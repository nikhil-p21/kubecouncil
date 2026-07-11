"""Local incident-store fake for API and domain tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.incidents import (
    AlertSignal,
    ApplicationProfile,
    AuditEvent,
    CriticalJourney,
    EvidenceBudget,
    EvidenceObservation,
    EvidenceSource,
    EvidenceWindow,
    Incident,
    IncidentStore,
    InvestigationRecord,
    ManagedWorkload,
    RecoveryCriteria,
    ReplicaBounds,
    WorkloadCriticality,
    WorkloadReference,
)


def fake_application_profile() -> ApplicationProfile:
    return ApplicationProfile(
        application_id="online-boutique",
        display_name="Online Boutique",
        version="v1",
        namespace="online-boutique",
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


class InMemoryIncidentStore(IncidentStore):
    def __init__(self) -> None:
        self._records: dict[str, InvestigationRecord] = {}

    def create(self, profile: ApplicationProfile, signal: AlertSignal) -> InvestigationRecord:
        if signal.application_id != profile.application_id:
            raise ValueError("alert signal application is not enrolled by the supplied profile")
        workload = WorkloadReference(namespace=signal.namespace, name=signal.workload_name)
        if workload not in {managed.reference for managed in profile.workloads}:
            raise ValueError("alert signal workload is not enrolled by the supplied profile")
        incident_id = f"inc-{uuid4().hex}"
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
                incident_id=incident_id,
                started_at=signal.observed_at - timedelta(minutes=5),
                ended_at=signal.observed_at,
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
        enrolled_workloads = {
            workload.reference for workload in record.application_profile.workloads
        }
        if evidence.scope not in enrolled_workloads:
            raise ValueError("evidence scope is outside the enrolled application profile")
        if any(item.evidence_id == evidence.evidence_id for item in record.evidence):
            raise ValueError("evidence already exists and is append-only")
        return self._replace(record.model_copy(update={"evidence": (*record.evidence, evidence)}))

    def append_audit_event(self, incident_id: str, event: AuditEvent) -> InvestigationRecord:
        record = self._required_record(incident_id)
        if event.incident_id != incident_id:
            raise ValueError("audit event must belong to the requested incident")
        if any(item.event_id == event.event_id for item in record.audit_events):
            raise ValueError("audit event already exists and is append-only")
        updated = record.model_copy(update={"audit_events": (*record.audit_events, event)})
        return self._replace(updated)

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
        now = datetime.now(UTC)
        evidence = EvidenceObservation(
            evidence_id=f"evidence-{uuid4().hex}",
            incident_id=incident_id,
            source=EvidenceSource.KUBERNETES,
            observed_at=now,
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
        self.append_audit_event(incident_id, event)
        return event

    def _required_record(self, incident_id: str) -> InvestigationRecord:
        record = self.get(incident_id)
        if record is None:
            raise ValueError("incident does not exist")
        return record

    def _replace(self, record: InvestigationRecord) -> InvestigationRecord:
        self._records[record.incident.incident_id] = record
        return record
