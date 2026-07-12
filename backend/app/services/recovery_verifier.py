"""Deterministic recovery verification and stabilization after an Intervention."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.incidents import (
    AuditEvent,
    CriticalJourney,
    IncidentLifecycle,
    IncidentStore,
    InterventionState,
    RecoveryAssessment,
    RecoveryEvidenceProvider,
    RecoveryObservation,
    RollbackDeploymentAction,
    SyntheticProbe,
    SyntheticProbeObservation,
    SyntheticProbeRunner,
    WorkloadReference,
)


class RecoveryVerificationError(RuntimeError):
    """Raised when the verifier cannot establish a safe, current recovery context."""


class DeterministicRecoveryVerifier:
    """Evaluates one bounded window and resolves only after stable, sufficient evidence."""

    def __init__(
        self,
        evidence: RecoveryEvidenceProvider,
        probes: SyntheticProbeRunner,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._evidence = evidence
        self._probes = probes
        self._clock = clock or (lambda: datetime.now(UTC))

    def verify_next_window(
        self,
        store: IncidentStore,
        incident_id: str,
    ) -> RecoveryAssessment:
        record = store.get(incident_id)
        if record is None:
            raise RecoveryVerificationError("recovery Incident does not exist")
        if record.incident.lifecycle is not IncidentLifecycle.MONITORING:
            raise RecoveryVerificationError("recovery verification requires a monitoring Incident")
        intervention = next(
            (
                item
                for item in reversed(record.interventions)
                if item.state is InterventionState.SUCCEEDED
            ),
            None,
        )
        if intervention is None:
            raise RecoveryVerificationError(
                "recovery verification requires a successful Intervention"
            )
        proposal = record.proposal
        if proposal is None or not isinstance(proposal.action, RollbackDeploymentAction):
            raise RecoveryVerificationError("rollback recovery requires its recorded proposal")
        approval = next(
            (item for item in record.approvals if item.approval_id == intervention.approval_id),
            None,
        )
        if approval is None:
            raise RecoveryVerificationError("recovery Intervention has no recorded Approval")
        criteria = record.application_profile.recovery_criteria
        journey = next(
            (
                item
                for item in record.application_profile.critical_journeys
                if item.name == criteria.critical_journey_name
            ),
            None,
        )
        if journey is None:
            raise RecoveryVerificationError("recovery Critical Journey is not declared")

        window_ended_at = self._clock()
        window_started_at = window_ended_at - timedelta(
            seconds=criteria.stabilization_window_seconds
        )
        previous = tuple(
            item
            for item in record.recovery_assessments
            if item.intervention_id == intervention.intervention_id
        )
        if previous and window_started_at < previous[-1].window_ended_at:
            raise RecoveryVerificationError("the next recovery window overlaps prior evidence")
        observation = self._evidence.observe(
            intervention.target,
            journey,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
        )
        if observation.deployment.target != intervention.target:
            raise RecoveryVerificationError("recovery evidence escaped the approved workload")
        if observation.journey.journey_name != journey.name:
            raise RecoveryVerificationError("recovery evidence escaped the declared journey")

        deployment = observation.deployment
        metrics = observation.journey
        kubernetes_converged = (
            deployment.generation > approval.workload_generation
            and deployment.observed_generation == deployment.generation
            and deployment.active_revision == proposal.action.revision
            and deployment.updated_replicas == deployment.desired_replicas
            and deployment.available_replicas == deployment.desired_replicas
            and deployment.unavailable_replicas == 0
        )
        symptoms_cleared = deployment.oom_terminations == 0 and deployment.restart_delta == 0
        traffic_sufficient = metrics.request_count >= journey.minimum_request_count
        synthetic_probe_used = False
        synthetic_probe_successes: int | None = None
        if traffic_sufficient:
            availability_satisfied = (
                metrics.success_rate is not None
                and metrics.success_rate >= journey.success_rate_minimum
                and metrics.success_rate >= metrics.healthy_baseline_success_rate
            )
            latency_satisfied = (
                metrics.p95_latency_ms is not None
                and metrics.p95_latency_ms <= journey.p95_latency_ms_maximum
                and metrics.p95_latency_ms <= metrics.healthy_baseline_p95_latency_ms
            )
            sufficient_evidence = (
                metrics.success_rate is not None and metrics.p95_latency_ms is not None
            )
        else:
            availability_satisfied = False
            latency_satisfied = False
            sufficient_evidence = False
            if criteria.allow_synthetic_availability_fallback and journey.synthetic_probe:
                probe_result = self._probes.run(journey.synthetic_probe)
                self._validate_probe_result(journey.synthetic_probe, probe_result)
                synthetic_probe_used = True
                synthetic_probe_successes = probe_result.successes
                availability_satisfied = probe_result.successes == probe_result.attempts

        criteria_satisfied = all(
            (
                kubernetes_converged,
                symptoms_cleared,
                traffic_sufficient,
                availability_satisfied,
                latency_satisfied,
                sufficient_evidence,
            )
        )
        stable_windows = 0
        if criteria_satisfied:
            stable_windows = 1
            if (
                previous
                and previous[-1].criteria_satisfied
                and previous[-1].window_ended_at == window_started_at
            ):
                stable_windows = previous[-1].stable_windows + 1
        assessment = RecoveryAssessment(
            incident_id=incident_id,
            intervention_id=intervention.intervention_id,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            observed_at=window_ended_at,
            generation=deployment.generation,
            observed_generation=deployment.observed_generation,
            active_revision=deployment.active_revision,
            desired_replicas=deployment.desired_replicas,
            updated_replicas=deployment.updated_replicas,
            available_replicas=deployment.available_replicas,
            unavailable_replicas=deployment.unavailable_replicas,
            oom_terminations=deployment.oom_terminations,
            restart_delta=deployment.restart_delta,
            kubernetes_converged=kubernetes_converged,
            symptoms_cleared=symptoms_cleared,
            journey_name=journey.name,
            criteria_satisfied=criteria_satisfied,
            request_count=metrics.request_count,
            success_rate=metrics.success_rate,
            p95_latency_ms=metrics.p95_latency_ms,
            traffic_sufficient=traffic_sufficient,
            availability_satisfied=availability_satisfied,
            latency_satisfied=latency_satisfied,
            synthetic_probe_used=synthetic_probe_used,
            synthetic_probe_successes=synthetic_probe_successes,
            sufficient_evidence=sufficient_evidence,
            stable_windows=stable_windows,
            required_stable_windows=criteria.required_stable_windows,
            explanation=self._explain(
                kubernetes_converged=kubernetes_converged,
                symptoms_cleared=symptoms_cleared,
                traffic_sufficient=traffic_sufficient,
                availability_satisfied=availability_satisfied,
                latency_satisfied=latency_satisfied,
                sufficient_evidence=sufficient_evidence,
                synthetic_probe_used=synthetic_probe_used,
            ),
        )
        stabilized = stable_windows >= criteria.required_stable_windows
        event_type = "recovery_window_failed"
        if criteria_satisfied:
            event_type = "recovery_stabilized" if stabilized else "recovery_window_satisfied"
        event = AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=incident_id,
            event_type=event_type,
            occurred_at=window_ended_at,
            actor="deterministic-recovery-verifier",
            details={
                "intervention_id": intervention.intervention_id,
                "stable_windows": str(stable_windows),
                "required_stable_windows": str(criteria.required_stable_windows),
                "traffic_sufficient": str(traffic_sufficient).lower(),
                "synthetic_probe_used": str(synthetic_probe_used).lower(),
                "kubernetes_converged": str(kubernetes_converged).lower(),
                "symptoms_cleared": str(symptoms_cleared).lower(),
            },
        )
        try:
            store.record_recovery_assessment(
                incident_id,
                record.incident.version,
                assessment,
                event,
            )
        except ValueError as error:
            raise RecoveryVerificationError(str(error)) from error
        return assessment

    @staticmethod
    def _validate_probe_result(
        probe: SyntheticProbe,
        result: SyntheticProbeObservation,
    ) -> None:
        if result.probe_name != probe.name or result.attempts != probe.repetitions:
            raise RecoveryVerificationError("synthetic probe result changed the declared probe")

    @staticmethod
    def _explain(
        *,
        kubernetes_converged: bool,
        symptoms_cleared: bool,
        traffic_sufficient: bool,
        availability_satisfied: bool,
        latency_satisfied: bool,
        sufficient_evidence: bool,
        synthetic_probe_used: bool,
    ) -> str:
        failures: list[str] = []
        if not kubernetes_converged:
            failures.append("Kubernetes generation, revision, or replicas have not converged")
        if not symptoms_cleared:
            failures.append("OOM termination or restart growth is still present")
        if not traffic_sufficient:
            failures.append("application traffic is below the 100-request minimum")
        if not availability_satisfied:
            failures.append("Critical Journey availability is below its recovery threshold")
        if not latency_satisfied:
            failures.append("Critical Journey latency evidence is insufficient or regressed")
        if not sufficient_evidence:
            failures.append("application-metric evidence is insufficient for recovery")
        if failures:
            suffix = (
                "; synthetic probes established availability only"
                if synthetic_probe_used
                else ""
            )
            return "; ".join(failures) + suffix
        return "Kubernetes, workload symptoms, and Critical Journey metrics satisfy recovery"


class FakeRecoveryEvidenceProvider:
    """Deterministic sequence fake for local recovery verification."""

    def __init__(self, observations: tuple[RecoveryObservation, ...]) -> None:
        self._observations = observations
        self._index = 0
        self.requests: list[tuple[WorkloadReference, str, datetime, datetime]] = []

    def observe(
        self,
        target: WorkloadReference,
        journey: CriticalJourney,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> RecoveryObservation:
        self.requests.append((target, journey.name, window_started_at, window_ended_at))
        if self._index >= len(self._observations):
            raise RecoveryVerificationError("fake recovery evidence sequence is exhausted")
        observation = self._observations[self._index]
        self._index += 1
        return observation


class FakeSyntheticProbeRunner:
    """Deterministic repeated-probe fake; unused probes fail closed by default."""

    def __init__(self, observations: tuple[SyntheticProbeObservation, ...] = ()) -> None:
        self._observations = observations
        self._index = 0
        self.probes: list[SyntheticProbe] = []

    def run(self, probe: SyntheticProbe) -> SyntheticProbeObservation:
        self.probes.append(probe)
        if self._index >= len(self._observations):
            raise RecoveryVerificationError("synthetic probe result is unavailable")
        observation = self._observations[self._index]
        self._index += 1
        return observation


__all__ = [
    "DeterministicRecoveryVerifier",
    "FakeRecoveryEvidenceProvider",
    "FakeSyntheticProbeRunner",
    "RecoveryVerificationError",
]
