"""Local incident-store fake for API and domain tests."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.domain.incidents import (
    AlertSignal,
    AlertSignalEvidence,
    ApplicationProfile,
    ApplicationProfileLoadResult,
    AuditEvent,
    CoordinatorOutput,
    CriticalJourney,
    EnrollmentProvider,
    EnrollmentSnapshot,
    EvidenceBudget,
    EvidenceMapping,
    EvidenceObservation,
    EvidenceProvider,
    EvidenceQuery,
    EvidenceQueryKind,
    EvidenceRetrievalFailure,
    EvidenceSource,
    EvidenceWindow,
    Incident,
    IncidentLifecycle,
    IncidentStore,
    InvestigationRecord,
    ManagedWorkload,
    ModelInvocation,
    NamespaceEnrollmentState,
    ObservabilityLink,
    ProfileValidationIssue,
    RawEvidenceObservation,
    RecoveryCriteria,
    ReplicaBounds,
    RoleBindingEnrollmentState,
    SpecialistFinding,
    WorkloadCriticality,
    WorkloadEnrollmentState,
    WorkloadReference,
    transition_incident,
)


def fake_application_profile() -> ApplicationProfile:
    return ApplicationProfile(
        application_id="online-boutique",
        display_name="Online Boutique",
        version="v1",
        namespace="online-boutique",
        namespaces=("online-boutique",),
        investigator_identity="serviceaccount:kubecouncil:investigator",
        investigator_role="investigator-read",
        executor_identity="serviceaccount:kubecouncil:executor",
        executor_role="executor-write",
        workloads=(
            ManagedWorkload(
                reference=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                criticality=WorkloadCriticality.IMPORTANT,
                replica_bounds=ReplicaBounds(minimum=1, maximum=5),
                executable=True,
                allowed_actions=("rollback_deployment", "scale_deployment", "restart_deployment"),
            ),
            ManagedWorkload(
                reference=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                criticality=WorkloadCriticality.CRITICAL,
                replica_bounds=ReplicaBounds(minimum=1, maximum=1),
                executable=False,
                protected_dependency=True,
            ),
        ),
        critical_journeys=(
            CriticalJourney(
                name="checkout",
                success_rate_minimum=0.99,
                p95_latency_ms_maximum=1500,
                minimum_request_count=100,
                synthetic_probe={
                    "name": "checkout-probe",
                    "target": "http://frontend/checkout",
                    "repetitions": 3,
                },
            ),
        ),
        evidence_mappings=(
            EvidenceMapping(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.WORKLOAD_STATE,
                scope=WorkloadReference(namespace="online-boutique", name="recommendationservice"),
                identifier="recommendationservice-rollout",
            ),
            EvidenceMapping(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.POD_EVENTS,
                scope=WorkloadReference(namespace="online-boutique", name="recommendationservice"),
                identifier="recommendationservice-events",
            ),
            EvidenceMapping(
                source=EvidenceSource.CLOUD_LOGGING,
                kind=EvidenceQueryKind.POD_LOGS,
                scope=WorkloadReference(namespace="online-boutique", name="recommendationservice"),
                identifier="recommendationservice-logs",
            ),
            EvidenceMapping(
                source=EvidenceSource.CLOUD_MONITORING,
                kind=EvidenceQueryKind.METRICS,
                scope=WorkloadReference(namespace="online-boutique", name="recommendationservice"),
                identifier="checkout-success-rate",
                query_template="sum(rate(checkout_requests_total[5m]))",
            ),
            EvidenceMapping(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.CHANGE_HISTORY,
                scope=WorkloadReference(namespace="online-boutique", name="recommendationservice"),
                identifier="recommendationservice-revisions",
            ),
            EvidenceMapping(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.WORKLOAD_STATE,
                scope=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                identifier="redis-cart-workload-state",
            ),
            EvidenceMapping(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.POD_EVENTS,
                scope=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                identifier="redis-cart-events",
            ),
            EvidenceMapping(
                source=EvidenceSource.CLOUD_LOGGING,
                kind=EvidenceQueryKind.POD_LOGS,
                scope=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                identifier="redis-cart-logs",
            ),
            EvidenceMapping(
                source=EvidenceSource.CLOUD_MONITORING,
                kind=EvidenceQueryKind.METRICS,
                scope=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                identifier="redis-cart-availability",
                query_template=(
                    'kube_deployment_status_replicas_available{namespace="online-boutique",'
                    'deployment="redis-cart"}'
                ),
            ),
            EvidenceMapping(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.CHANGE_HISTORY,
                scope=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                identifier="redis-cart-revisions",
            ),
        ),
        observability_links=(
            ObservabilityLink(
                label="Online Boutique logs",
                source=EvidenceSource.CLOUD_LOGGING,
                url=(
                    "https://console.cloud.google.com/logs/query;query="
                    "resource.type%3D%22k8s_container%22"
                ),
            ),
            ObservabilityLink(
                label="Online Boutique metrics",
                source=EvidenceSource.CLOUD_MONITORING,
                url="https://console.cloud.google.com/monitoring/metrics-explorer",
            ),
        ),
        evidence_budget=EvidenceBudget(
            maximum_queries_per_specialist=2,
            maximum_log_lines=100,
            maximum_metric_series=20,
            maximum_window_minutes=30,
        ),
        recovery_criteria=RecoveryCriteria(
            critical_journey_name="checkout",
            required_stable_windows=2,
            stabilization_window_seconds=60,
        ),
    )


class InMemoryApplicationProfileProvider:
    """Fake ConfigMap-backed profile source that preserves validation failures for the UI."""

    def __init__(self, profiles: tuple[ApplicationProfile | dict[str, Any], ...]) -> None:
        self._profiles = profiles

    def list_profiles(self) -> tuple[ApplicationProfileLoadResult, ...]:
        return tuple(self._load(profile, index) for index, profile in enumerate(self._profiles))

    def reload(
        self, profiles: tuple[ApplicationProfile | dict[str, Any], ...]
    ) -> tuple[ApplicationProfileLoadResult, ...]:
        self._profiles = profiles
        return self.list_profiles()

    @staticmethod
    def _load(
        profile: ApplicationProfile | dict[str, Any], index: int
    ) -> ApplicationProfileLoadResult:
        source = f"fake://application-profiles/{index}"
        try:
            parsed = ApplicationProfile.model_validate(profile)
        except ValidationError as error:
            raw_application_id = (
                profile.get("application_id") if isinstance(profile, dict) else None
            )
            application_id = raw_application_id if isinstance(raw_application_id, str) else None
            return ApplicationProfileLoadResult(
                source=source,
                application_id=application_id,
                errors=tuple(
                    ProfileValidationIssue(
                        location=".".join(str(part) for part in issue["loc"]),
                        message=issue["msg"],
                    )
                    for issue in error.errors()
                ),
            )
        return ApplicationProfileLoadResult(
            source=source,
            application_id=parsed.application_id,
            profile=parsed,
        )


class FakeEnrollmentProvider(EnrollmentProvider):
    """Read-only Kubernetes Enrollment facts for deterministic local verification."""

    def __init__(self, snapshot: EnrollmentSnapshot) -> None:
        self._snapshot = snapshot

    @classmethod
    def ready_for(cls, profile: ApplicationProfile) -> "FakeEnrollmentProvider":
        return cls(
            EnrollmentSnapshot(
                namespaces=tuple(
                    NamespaceEnrollmentState(
                        namespace=namespace,
                        exists=True,
                        labels={"kubecouncil.io/enrolled": "true"},
                    )
                    for namespace in profile.namespaces
                ),
                workloads=tuple(
                    WorkloadEnrollmentState(
                        reference=workload.reference,
                        exists=True,
                        labels=({"kubecouncil.io/managed": "true"} if workload.executable else {}),
                    )
                    for workload in profile.workloads
                ),
                role_bindings=tuple(
                    binding
                    for namespace in profile.namespaces
                    for binding in (
                        RoleBindingEnrollmentState(
                            namespace=namespace,
                            subject=profile.investigator_identity,
                            role=profile.investigator_role,
                            exists=True,
                        ),
                        RoleBindingEnrollmentState(
                            namespace=namespace,
                            subject=profile.executor_identity,
                            role=profile.executor_role,
                            exists=True,
                        ),
                    )
                ),
                admission_policy_binding=True,
            )
        )

    @classmethod
    def unready_for(cls, profile: ApplicationProfile) -> "FakeEnrollmentProvider":
        return cls(
            EnrollmentSnapshot(
                namespaces=tuple(
                    NamespaceEnrollmentState(namespace=namespace, exists=True)
                    for namespace in profile.namespaces
                ),
                workloads=tuple(
                    WorkloadEnrollmentState(reference=workload.reference, exists=True)
                    for workload in profile.workloads
                ),
            )
        )

    @staticmethod
    def empty() -> "FakeEnrollmentProvider":
        return FakeEnrollmentProvider(EnrollmentSnapshot())

    def inspect(self, profile: ApplicationProfile) -> EnrollmentSnapshot:
        return self._snapshot


class FakeEvidenceProvider(EvidenceProvider):
    """Bounded deterministic observations used by the local investigation path."""

    def __init__(self, observations: tuple[RawEvidenceObservation, ...] | None = None) -> None:
        self._observations = observations

    def collect_initial(
        self,
        profile: ApplicationProfile,
        signal: AlertSignal,
        window: EvidenceWindow,
    ) -> tuple[RawEvidenceObservation, ...]:
        if self._observations is not None:
            return self._observations
        target = WorkloadReference(namespace=signal.namespace, name=signal.workload_name)
        return (
            RawEvidenceObservation(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.WORKLOAD_STATE,
                scope=target,
                content="Deployment generation 8 is available; rollout revision 8 is active.",
                provider_reference="fake://kubernetes/deployments/recommendationservice",
                observed_at=window.ended_at,
            ),
            RawEvidenceObservation(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.POD_EVENTS,
                scope=target,
                content="Pod recommendationservice-7d9f restarted after an OOMKilled termination.",
                provider_reference="fake://kubernetes/events/recommendationservice",
                observed_at=window.ended_at,
            ),
            RawEvidenceObservation(
                source=EvidenceSource.CLOUD_LOGGING,
                kind=EvidenceQueryKind.POD_LOGS,
                scope=target,
                content="recommendationservice process terminated: OOMKilled",
                provider_reference="fake://logging/recommendationservice",
                observed_at=window.ended_at,
            ),
            RawEvidenceObservation(
                source=EvidenceSource.CLOUD_MONITORING,
                kind=EvidenceQueryKind.METRICS,
                scope=target,
                provider_reference="fake://monitoring/checkout-success-rate",
                observed_at=window.ended_at,
                item_count=2,
                metric_series=(
                    "checkout success rate fell to 91%.",
                    "p95 latency rose to 2400ms.",
                ),
            ),
            RawEvidenceObservation(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceQueryKind.CHANGE_HISTORY,
                scope=target,
                content="ReplicaSet revision 8 introduced a lower memory limit than revision 7.",
                provider_reference="fake://kubernetes/replicasets/recommendationservice",
                observed_at=window.ended_at,
            ),
        )


class InMemoryIncidentStore(IncidentStore):
    def __init__(self) -> None:
        self._records: dict[str, InvestigationRecord] = {}

    def create(self, profile: ApplicationProfile, signal: AlertSignal) -> InvestigationRecord:
        if signal.application_id != profile.application_id:
            raise ValueError("alert signal application is not enrolled by the supplied profile")
        if signal.namespace not in profile.namespaces:
            raise ValueError("alert signal namespace is not enrolled by the supplied profile")
        workload = WorkloadReference(namespace=signal.namespace, name=signal.workload_name)
        if workload not in {managed.reference for managed in profile.workloads}:
            raise ValueError("alert signal workload is not enrolled by the supplied profile")
        incident_id = f"inc-{uuid4().hex}"
        started_at = signal.window_start or (
            signal.observed_at - timedelta(minutes=profile.evidence_budget.maximum_window_minutes)
        )
        ended_at = signal.window_end or signal.observed_at
        maximum_window = timedelta(minutes=profile.evidence_budget.maximum_window_minutes)
        if ended_at - started_at > maximum_window:
            raise ValueError("alert window exceeds the application profile evidence budget")
        record = InvestigationRecord(
            incident=Incident(
                incident_id=incident_id,
                application_id=profile.application_id,
                profile_version=profile.version,
                opened_at=signal.observed_at,
                summary=signal.summary,
            ),
            application_profile=profile,
            evidence_window=EvidenceWindow(
                window_id=f"window-{uuid4().hex}",
                incident_id=incident_id,
                started_at=started_at,
                ended_at=ended_at,
                captured_at=signal.observed_at,
            ),
        )
        self._records[incident_id] = record
        return record

    def get(self, incident_id: str) -> InvestigationRecord | None:
        return self._records.get(incident_id)

    def list(self) -> tuple[InvestigationRecord, ...]:
        records = sorted(
            self._records.values(), key=lambda record: record.incident.opened_at, reverse=True
        )
        return tuple(records)

    def append_evidence(
        self, incident_id: str, evidence: EvidenceObservation
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if evidence.incident_id != incident_id:
            raise ValueError("evidence must belong to the requested incident")
        if evidence.evidence_window_id != record.evidence_window.window_id:
            raise ValueError("evidence must belong to the immutable initial evidence window")
        if not (
            record.evidence_window.started_at
            <= evidence.observed_at
            <= record.evidence_window.ended_at
        ):
            raise ValueError("evidence observation falls outside the immutable evidence window")
        enrolled_workloads = {
            workload.reference for workload in record.application_profile.workloads
        }
        if evidence.scope not in enrolled_workloads:
            raise ValueError("evidence scope is outside the enrolled application profile")
        if evidence.evidence_query_id is not None and evidence.evidence_query_id not in {
            query.query_id for query in record.evidence_queries
        }:
            raise ValueError("follow-up evidence must reference a recorded evidence query")
        if any(item.evidence_id == evidence.evidence_id for item in record.evidence):
            raise ValueError("evidence already exists and is append-only")
        return self._replace(record.model_copy(update={"evidence": (*record.evidence, evidence)}))

    def append_alert_signal(
        self, incident_id: str, signal: AlertSignalEvidence
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if signal.incident_id != incident_id:
            raise ValueError("alert signal evidence must belong to the requested incident")
        if signal.signal.application_id != record.incident.application_id:
            raise ValueError("alert signal evidence must belong to the enrolled application")
        if any(item.notification_id == signal.notification_id for item in record.alert_signals):
            raise ValueError("alert signal evidence already exists and is append-only")
        return self._replace(
            record.model_copy(update={"alert_signals": (*record.alert_signals, signal)})
        )

    def append_evidence_retrieval_failure(
        self, incident_id: str, failure: EvidenceRetrievalFailure
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if failure.incident_id != incident_id:
            raise ValueError("evidence retrieval failure must belong to the requested incident")
        if any(
            item.failure_id == failure.failure_id for item in record.evidence_retrieval_failures
        ):
            raise ValueError("evidence retrieval failure already exists and is append-only")
        return self._replace(
            record.model_copy(
                update={
                    "evidence_retrieval_failures": (
                        *record.evidence_retrieval_failures,
                        failure,
                    )
                }
            )
        )

    def append_evidence_query(self, incident_id: str, query: EvidenceQuery) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if query.incident_id != incident_id:
            raise ValueError("evidence query must belong to the requested incident")
        if query.target not in {
            workload.reference for workload in record.application_profile.workloads
        }:
            raise ValueError("evidence query target is outside the enrolled application profile")
        if any(item.query_id == query.query_id for item in record.evidence_queries):
            raise ValueError("evidence query already exists and is append-only")
        return self._replace(
            record.model_copy(update={"evidence_queries": (*record.evidence_queries, query)})
        )

    def append_finding(
        self, incident_id: str, finding: SpecialistFinding
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if finding.incident_id != incident_id:
            raise ValueError("Specialist Finding must belong to the requested incident")
        if any(item.finding_id == finding.finding_id for item in record.findings):
            raise ValueError("Specialist Finding already exists and is append-only")
        if any(item.specialist is finding.specialist for item in record.findings):
            raise ValueError("a Specialist may record only one final finding")
        evidence_ids = {item.evidence_id for item in record.evidence}
        if any(citation.evidence_id not in evidence_ids for citation in finding.citations):
            raise ValueError("Specialist Finding must cite recorded evidence")
        return self._replace(
            InvestigationRecord.model_validate(
                record.model_copy(update={"findings": (*record.findings, finding)}).model_dump()
            )
        )

    def append_model_invocation(
        self, incident_id: str, invocation: ModelInvocation
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if invocation.incident_id != incident_id:
            raise ValueError("model invocation must belong to the requested incident")
        if any(item.invocation_id == invocation.invocation_id for item in record.model_invocations):
            raise ValueError("model invocation already exists and is append-only")
        return self._replace(
            InvestigationRecord.model_validate(
                record.model_copy(
                    update={"model_invocations": (*record.model_invocations, invocation)}
                ).model_dump()
            )
        )

    def complete_investigation(
        self, incident_id: str, output: CoordinatorOutput
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if record.incident.investigation_outcome.value != "not_started":
            raise ValueError("investigation already has a terminal outcome")
        if any(hypothesis.incident_id != incident_id for hypothesis in output.hypotheses):
            raise ValueError("Root Cause Hypotheses must belong to the requested incident")
        updated_incident = transition_incident(
            record.incident,
            lifecycle=IncidentLifecycle.AWAITING_APPROVAL
            if output.proposal is not None
            else IncidentLifecycle.INVESTIGATING,
            investigation_outcome=output.outcome,
        ).model_copy(update={"version": record.incident.version + 1})
        updated = record.model_copy(
            update={
                "incident": updated_incident,
                "hypotheses": output.hypotheses,
                "proposal": output.proposal,
                "manual_guidance": output.manual_guidance,
            }
        )
        return self._replace(InvestigationRecord.model_validate(updated.model_dump()))

    def append_audit_event(self, incident_id: str, event: AuditEvent) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if event.incident_id != incident_id:
            raise ValueError("audit event must belong to the requested incident")
        if any(item.event_id == event.event_id for item in record.audit_events):
            raise ValueError("audit event already exists and is append-only")
        next_cursor = record.audit_events[-1].cursor + 1 if record.audit_events else 1
        stored_event = event.model_copy(update={"cursor": next_cursor})
        updated = record.model_copy(update={"audit_events": (*record.audit_events, stored_event)})
        return self._replace(updated)

    def timeline(self, incident_id: str, *, after: int = 0) -> tuple[AuditEvent, ...]:
        record = self._required_record(incident_id)
        return tuple(event for event in record.audit_events if event.cursor > after)

    def compare_and_set(
        self,
        incident_id: str,
        expected_version: int,
        replacement: Incident,
    ) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if record.incident.version != expected_version:
            raise ValueError("stale incident version")
        if replacement.incident_id != incident_id:
            raise ValueError("replacement incident does not match the stored incident")
        if replacement.version != expected_version:
            raise ValueError("replacement incident must use the expected version")
        if replacement.application_id != record.incident.application_id:
            raise ValueError("replacement incident must use the enrolled application")
        if replacement.profile_version != record.incident.profile_version:
            raise ValueError("replacement incident must use the enrolled profile version")
        updated_incident = replacement.model_copy(update={"version": expected_version + 1})
        updated_record = InvestigationRecord.model_validate(
            record.model_copy(update={"incident": updated_incident}).model_dump()
        )
        return self._replace(updated_record)

    def open_fake_incident(self, summary: str) -> InvestigationRecord:
        now = datetime.now(UTC)
        return self.create(
            fake_application_profile(),
            AlertSignal(
                signal_id=f"manual-{uuid4().hex}",
                application_id="online-boutique",
                namespace="online-boutique",
                workload_name="recommendationservice",
                workload_namespace="online-boutique",
                summary=summary,
                observed_at=now,
            ),
        )

    def append_fake_evidence(self, incident_id: str) -> EvidenceObservation:
        record = self._required_record(incident_id)
        evidence = EvidenceObservation(
            evidence_id=f"evidence-{uuid4().hex}",
            incident_id=incident_id,
            source=EvidenceSource.KUBERNETES,
            query=EvidenceQueryKind.WORKLOAD_STATE,
            query_reference="recommendationservice-rollout",
            evidence_window_id=record.evidence_window.window_id,
            observed_at=record.evidence_window.ended_at,
            scope=WorkloadReference(namespace="online-boutique", name="recommendationservice"),
            redacted_excerpt="Container terminated with reason OOMKilled.",
            content_hash="fake-evidence-hash",
            provider_reference="fake://kubernetes/pod/recommendationservice-1",
        )
        self.append_evidence(incident_id, evidence)
        return evidence

    def append_fake_audit_event(self, incident_id: str, event_type: str) -> AuditEvent:
        event = AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=incident_id,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            actor="local-operator",
        )
        updated = self.append_audit_event(incident_id, event)
        return updated.audit_events[-1]

    def _required_record(self, incident_id: str) -> InvestigationRecord:
        record = self.get(incident_id)
        if record is None:
            raise ValueError("incident does not exist")
        return record

    def _replace(self, record: InvestigationRecord) -> InvestigationRecord:
        self._records[record.incident.incident_id] = record
        return record
