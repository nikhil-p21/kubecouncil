import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.identity import OperatorIdentity, OperatorRole
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import (
    AlertSignal,
    AlertSignalEvidence,
    ApprovalDecision,
    CriticalJourneyObservation,
    DeploymentRecoveryObservation,
    IncidentLifecycle,
    IncidentStore,
    InterventionOutcome,
    RecoveryObservation,
    SyntheticProbeObservation,
)
from app.services.approval import ApprovalService
from app.services.council import BoundedIncidentCouncil, FakeIncidentCouncilModel
from app.services.evidence import DeterministicEvidenceRedactor, InitialEvidenceWindowCollector
from app.services.incident_store import FirestoreIncidentStore, InMemoryDocumentDatabase
from app.services.intervention_executor import (
    FakeExecutorKubernetesProvider,
    RollbackInterventionExecutor,
)
from app.services.intervention_queue import InMemoryInterventionQueue
from app.services.proposal_policy import DeterministicProposalPolicy
from app.services.recovery_verifier import (
    DeterministicRecoveryVerifier,
    FakeRecoveryEvidenceProvider,
    FakeSyntheticProbeRunner,
)
from app.services.workload_lease import FirestoreWorkloadLeaseStore, InMemoryLeaseDatabase


@pytest.mark.parametrize(
    "store",
    (
        InMemoryIncidentStore(),
        FirestoreIncidentStore(InMemoryDocumentDatabase()),
    ),
)
def test_two_consecutive_healthy_windows_resolve_incident(
    store: IncidentStore,
) -> None:
    store, incident_id, clock = _monitoring_incident(store)
    provider = FakeRecoveryEvidenceProvider((_healthy_observation(), _healthy_observation()))
    verifier = DeterministicRecoveryVerifier(provider, FakeSyntheticProbeRunner(), clock=clock)

    first = verifier.verify_next_window(store, incident_id)

    assert first.criteria_satisfied is True
    assert first.stable_windows == 1
    still_monitoring = store.get(incident_id)
    assert still_monitoring is not None
    assert still_monitoring.incident.lifecycle is IncidentLifecycle.MONITORING
    assert still_monitoring.incident.intervention_outcome is InterventionOutcome.MONITORING

    clock.advance(seconds=60)
    second = verifier.verify_next_window(store, incident_id)

    assert second.criteria_satisfied is True
    assert second.stable_windows == 2
    resolved = store.get(incident_id)
    assert resolved is not None
    assert resolved.incident.lifecycle is IncidentLifecycle.RESOLVED
    assert resolved.incident.intervention_outcome is InterventionOutcome.SUCCEEDED
    assert [item.stable_windows for item in resolved.recovery_assessments] == [1, 2]
    assert [
        event.event_type
        for event in resolved.audit_events
        if event.event_type.startswith("recovery_")
    ] == ["recovery_window_satisfied", "recovery_stabilized"]


def test_regression_resets_stabilization_progress() -> None:
    store, incident_id, clock = _monitoring_incident()
    provider = FakeRecoveryEvidenceProvider(
        (
            _healthy_observation(),
            _healthy_observation(oom_terminations=1),
            _healthy_observation(),
            _healthy_observation(),
        )
    )
    verifier = DeterministicRecoveryVerifier(provider, FakeSyntheticProbeRunner(), clock=clock)

    first = verifier.verify_next_window(store, incident_id)
    clock.advance(seconds=60)
    regressed = verifier.verify_next_window(store, incident_id)
    clock.advance(seconds=60)
    restarted = verifier.verify_next_window(store, incident_id)
    clock.advance(seconds=60)
    stabilized = verifier.verify_next_window(store, incident_id)

    assert first.stable_windows == 1
    assert regressed.criteria_satisfied is False
    assert regressed.symptoms_cleared is False
    assert regressed.stable_windows == 0
    assert restarted.stable_windows == 1
    assert stabilized.stable_windows == 2


def test_sparse_traffic_uses_probe_for_availability_but_latency_is_insufficient() -> None:
    store, incident_id, clock = _monitoring_incident()
    sparse = _healthy_observation(request_count=24, p95_latency_ms=None)
    probes = FakeSyntheticProbeRunner(
        (SyntheticProbeObservation(probe_name="checkout-probe", attempts=3, successes=3),)
    )
    verifier = DeterministicRecoveryVerifier(
        FakeRecoveryEvidenceProvider((sparse,)), probes, clock=clock
    )

    assessment = verifier.verify_next_window(store, incident_id)

    assert assessment.traffic_sufficient is False
    assert assessment.synthetic_probe_used is True
    assert assessment.availability_satisfied is True
    assert assessment.latency_satisfied is False
    assert assessment.sufficient_evidence is False
    assert assessment.criteria_satisfied is False
    assert "latency" in assessment.explanation.lower()
    record = store.get(incident_id)
    assert record is not None
    assert record.incident.lifecycle is IncidentLifecycle.MONITORING


@pytest.mark.parametrize(
    ("changes", "failed_check"),
    (
        ({"observed_generation": 9}, "kubernetes_converged"),
        ({"active_revision": 8}, "kubernetes_converged"),
        ({"updated_replicas": 1}, "kubernetes_converged"),
        ({"unavailable_replicas": 1}, "kubernetes_converged"),
        ({"restart_delta": 1}, "symptoms_cleared"),
    ),
)
def test_recovery_rejects_non_converged_or_regressing_workload(
    changes: dict[str, int],
    failed_check: str,
) -> None:
    store, incident_id, clock = _monitoring_incident()
    verifier = DeterministicRecoveryVerifier(
        FakeRecoveryEvidenceProvider((_healthy_observation(**changes),)),
        FakeSyntheticProbeRunner(),
        clock=clock,
    )

    assessment = verifier.verify_next_window(store, incident_id)

    assert getattr(assessment, failed_check) is False
    assert assessment.criteria_satisfied is False


def test_application_metrics_must_meet_profile_and_healthy_baseline() -> None:
    store, incident_id, clock = _monitoring_incident()
    regressed = _healthy_observation(success_rate=0.98, p95_latency_ms=1200)
    verifier = DeterministicRecoveryVerifier(
        FakeRecoveryEvidenceProvider((regressed,)),
        FakeSyntheticProbeRunner(),
        clock=clock,
    )

    assessment = verifier.verify_next_window(store, incident_id)

    assert assessment.traffic_sufficient is True
    assert assessment.availability_satisfied is False
    assert assessment.latency_satisfied is False
    assert assessment.criteria_satisfied is False


def test_provider_alert_closure_cannot_resolve_a_monitoring_incident() -> None:
    store, incident_id, clock = _monitoring_incident()
    record = store.get(incident_id)
    assert record is not None
    closure = AlertSignal(
        signal_id="provider-closure",
        application_id=record.incident.application_id,
        namespace=record.application_profile.namespace,
        workload_name="recommendationservice",
        workload_namespace=record.application_profile.namespace,
        summary="provider considers the alert closed",
        observed_at=clock(),
    )

    updated = store.append_alert_signal(
        incident_id,
        AlertSignalEvidence(
            notification_id="provider-closure",
            incident_id=incident_id,
            signal=closure,
            provider_state="closed",
            received_at=clock(),
        ),
    )

    assert updated.incident.lifecycle is IncidentLifecycle.MONITORING
    assert updated.incident.intervention_outcome is InterventionOutcome.MONITORING
    assert updated.recovery_assessments == ()


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _monitoring_incident(
    store: IncidentStore | None = None,
) -> tuple[IncidentStore, str, MutableClock]:
    store = store or InMemoryIncidentStore()
    now = datetime.now(UTC)
    profile = fake_application_profile()
    signal = AlertSignal(
        signal_id="manual-recovery-test",
        application_id=profile.application_id,
        namespace=profile.namespace,
        workload_name="recommendationservice",
        workload_namespace=profile.namespace,
        summary="recommendationservice is OOMKilled",
        observed_at=now - timedelta(minutes=10),
    )
    record = store.create(profile, signal)
    InitialEvidenceWindowCollector(
        FakeEvidenceProvider(), DeterministicEvidenceRedactor()
    ).collect(
        store,
        incident_id=record.incident.incident_id,
        profile=profile,
        signal=signal,
        window=record.evidence_window,
    )
    asyncio.run(
        BoundedIncidentCouncil(FakeIncidentCouncilModel()).investigate(
            store, record.incident.incident_id
        )
    )
    kubernetes = FakeExecutorKubernetesProvider.ready()
    enrollment = FakeEnrollmentProvider.ready_for(profile)
    DeterministicProposalPolicy(kubernetes, enrollment).evaluate_and_record(
        store, record.incident.incident_id
    )
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue, clock=lambda: now)
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    executor = RollbackInterventionExecutor(
        kubernetes=kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-recovery-test",
        clock=lambda: now,
    )
    executor.consume(store, queue.pending()[0])
    return store, record.incident.incident_id, MutableClock(now + timedelta(seconds=60))


def _healthy_observation(
    *,
    observed_generation: int = 10,
    active_revision: int = 7,
    updated_replicas: int = 2,
    unavailable_replicas: int = 0,
    oom_terminations: int = 0,
    restart_delta: int = 0,
    request_count: int = 120,
    success_rate: float = 0.995,
    p95_latency_ms: int | None = 800,
) -> RecoveryObservation:
    return RecoveryObservation(
        deployment=DeploymentRecoveryObservation(
            target=fake_application_profile().workloads[0].reference,
            generation=10,
            observed_generation=observed_generation,
            active_revision=active_revision,
            desired_replicas=2,
            updated_replicas=updated_replicas,
            available_replicas=2,
            unavailable_replicas=unavailable_replicas,
            oom_terminations=oom_terminations,
            restart_delta=restart_delta,
        ),
        journey=CriticalJourneyObservation(
            journey_name="checkout",
            request_count=request_count,
            success_rate=success_rate,
            p95_latency_ms=p95_latency_ms,
            healthy_baseline_success_rate=0.995,
            healthy_baseline_p95_latency_ms=900,
        ),
    )


def _responder() -> OperatorIdentity:
    return OperatorIdentity(
        principal="responder@example.com",
        subject="responder-1",
        role=OperatorRole.RESPONDER,
    )
