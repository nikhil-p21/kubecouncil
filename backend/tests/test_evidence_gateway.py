from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.incidents import get_evidence_query_gateway, get_incident_store
from app.domain.incident_fakes import InMemoryIncidentStore
from app.domain.incidents import (
    EvidenceProviderRequest,
    EvidenceQueryKind,
    EvidenceSource,
    RawEvidenceObservation,
    SpecialistRole,
)
from app.main import app
from app.services.evidence import DeterministicEvidenceRedactor
from app.services.evidence_gateway import (
    CloudLoggingEvidenceAdapter,
    CloudMonitoringEvidenceAdapter,
    EvidenceGatewayError,
    EvidenceQueryGateway,
    KubernetesEvidenceAdapter,
    RetryableEvidenceProviderError,
)


def test_gateway_derives_scope_query_and_budgets_from_the_incident_profile() -> None:
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    adapter = RecordingAdapter()
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.CLOUD_MONITORING: adapter},
        redactor=DeterministicEvidenceRedactor(),
        deadline_seconds=4,
    )

    result = gateway.execute(
        store,
        incident_id=record.incident.incident_id,
        specialist=SpecialistRole.METRICS,
        mapping_identifier="checkout-success-rate",
        query_round=1,
    )

    request = adapter.requests[0]
    assert request.scope.namespace == "online-boutique"
    assert request.scope.name == "recommendationservice"
    assert request.query_template == "sum(rate(checkout_requests_total[5m]))"
    assert request.started_at == record.evidence_window.started_at
    assert request.ended_at == record.evidence_window.ended_at
    assert request.maximum_items == 20
    assert request.deadline_seconds == 4
    assert result.evidence_queries[0].target == request.scope
    assert result.evidence[0].query_reference == "checkout-success-rate"
    assert result.evidence[0].evidence_query_id == request.query_id
    assert result.evidence[0].redacted_excerpt == "series-1\nseries-2"
    assert result.audit_events[-1].event_type == "evidence_query_completed"
    assert result.audit_events[-1].details["query_id"] == request.query_id


def test_gateway_rejects_scope_escape_broad_discovery_and_secret_requests() -> None:
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.KUBERNETES: RecordingAdapter()},
        redactor=DeterministicEvidenceRedactor(),
    )

    for identifier in ("other-namespace-pods", "all-resources", "secrets"):
        with pytest.raises(EvidenceGatewayError, match="allowlisted"):
            gateway.execute(
                store,
                incident_id=record.incident.incident_id,
                specialist=SpecialistRole.HEALTH,
                mapping_identifier=identifier,
                query_round=1,
            )


def test_gateway_enforces_per_specialist_query_and_round_budgets() -> None:
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    adapter = RecordingAdapter(source=EvidenceSource.KUBERNETES)
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.KUBERNETES: adapter},
        redactor=DeterministicEvidenceRedactor(),
    )

    gateway.execute(
        store,
        incident_id=record.incident.incident_id,
        specialist=SpecialistRole.HEALTH,
        mapping_identifier="recommendationservice-rollout",
        query_round=1,
    )
    gateway.execute(
        store,
        incident_id=record.incident.incident_id,
        specialist=SpecialistRole.HEALTH,
        mapping_identifier="recommendationservice-events",
        query_round=2,
    )

    with pytest.raises(EvidenceGatewayError, match="query budget"):
        gateway.execute(
            store,
            incident_id=record.incident.incident_id,
            specialist=SpecialistRole.HEALTH,
            mapping_identifier="recommendationservice-revisions",
            query_round=2,
        )


def test_gateway_retries_only_retryable_failures_and_records_safe_failure() -> None:
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    adapter = FlakyAdapter()
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.CLOUD_MONITORING: adapter},
        redactor=DeterministicEvidenceRedactor(),
        maximum_attempts=2,
    )

    result = gateway.execute(
        store,
        incident_id=record.incident.incident_id,
        specialist=SpecialistRole.METRICS,
        mapping_identifier="checkout-success-rate",
        query_round=1,
    )

    assert adapter.attempts == 2
    assert result.audit_events[-1].details["attempts"] == "2"

    failed_store = InMemoryIncidentStore()
    failed_record = failed_store.open_fake_incident("recommendationservice is OOMKilled")
    failed_gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.CLOUD_MONITORING: AlwaysFailingAdapter()},
        redactor=DeterministicEvidenceRedactor(),
    )
    with pytest.raises(EvidenceGatewayError, match="provider failed"):
        failed_gateway.execute(
            failed_store,
            incident_id=failed_record.incident.incident_id,
            specialist=SpecialistRole.METRICS,
            mapping_identifier="checkout-success-rate",
            query_round=1,
        )
    persisted = failed_store.get(failed_record.incident.incident_id)
    assert persisted is not None
    assert persisted.evidence_retrieval_failures[-1].message == (
        "evidence query provider failed; no evidence was retained"
    )
    assert "provider-secret" not in persisted.model_dump_json()


def test_gateway_truncates_and_redacts_before_persistence() -> None:
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.CLOUD_MONITORING: OverBudgetAdapter()},
        redactor=DeterministicEvidenceRedactor(),
    )

    result = gateway.execute(
        store,
        incident_id=record.incident.incident_id,
        specialist=SpecialistRole.METRICS,
        mapping_identifier="checkout-success-rate",
        query_round=1,
    )

    evidence = result.evidence[0]
    assert evidence.truncated is True
    assert len(evidence.redacted_excerpt.splitlines()) == 20
    assert "result-secret" not in evidence.redacted_excerpt
    assert "provider-secret" not in evidence.provider_reference
    assert "<redacted>" in evidence.redacted_excerpt
    assert "<redacted>" in evidence.provider_reference


def test_gateway_fails_closed_when_a_provider_misses_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, 6.0))
    monkeypatch.setattr("app.services.evidence_gateway.monotonic", lambda: next(clock))
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.CLOUD_MONITORING: RecordingAdapter()},
        redactor=DeterministicEvidenceRedactor(),
        deadline_seconds=5,
    )

    with pytest.raises(EvidenceGatewayError, match="provider failed"):
        gateway.execute(
            store,
            incident_id=record.incident.incident_id,
            specialist=SpecialistRole.METRICS,
            mapping_identifier="checkout-success-rate",
            query_round=1,
        )

    persisted = store.get(record.incident.incident_id)
    assert persisted is not None
    assert persisted.audit_events[-1].event_type == "evidence_query_failed"


def test_kubernetes_adapter_exposes_only_purpose_built_read_operations() -> None:
    reader = RecordingKubernetesReader()
    adapter = KubernetesEvidenceAdapter(reader)

    observation = adapter.query(_request(EvidenceSource.KUBERNETES))

    assert observation.kind is EvidenceQueryKind.WORKLOAD_STATE
    assert reader.calls == [("workload_state", "online-boutique", "recommendationservice", 25)]
    unsupported = _request(EvidenceSource.KUBERNETES).model_copy(
        update={"kind": EvidenceQueryKind.ALERT_POLICY}
    )
    with pytest.raises(EvidenceGatewayError, match="unsupported Kubernetes"):
        adapter.query(unsupported)
    assert not hasattr(reader, "read_secrets")
    assert not hasattr(reader, "list_resources")


def test_cloud_adapters_allow_only_bounded_logging_and_monitoring_operations() -> None:
    logging_reader = RecordingLoggingReader()
    logging_adapter = CloudLoggingEvidenceAdapter(logging_reader)
    logs_request = _request(EvidenceSource.CLOUD_LOGGING).model_copy(
        update={"kind": EvidenceQueryKind.POD_LOGS, "maximum_items": 12}
    )

    logs = logging_adapter.query(logs_request)

    assert logs.item_count == 2
    assert logging_reader.calls[0][0:3] == (
        "online-boutique",
        "recommendationservice",
        12,
    )

    monitoring_reader = RecordingMonitoringReader()
    monitoring_adapter = CloudMonitoringEvidenceAdapter(monitoring_reader)
    metrics_request = _request(EvidenceSource.CLOUD_MONITORING)
    metrics = monitoring_adapter.query(metrics_request)
    assert metrics.metric_series == ("series-a", "series-b")
    assert monitoring_reader.queries == ["sum(rate(checkout_requests_total[5m]))"]

    with pytest.raises(EvidenceGatewayError, match="unsupported Cloud Logging"):
        logging_adapter.query(metrics_request)
    with pytest.raises(EvidenceGatewayError, match="unsupported Cloud Monitoring"):
        monitoring_adapter.query(logs_request)


def test_evidence_query_api_accepts_a_mapping_id_but_no_caller_supplied_scope() -> None:
    store = InMemoryIncidentStore()
    record = store.open_fake_incident("recommendationservice is OOMKilled")
    gateway = EvidenceQueryGateway(
        adapters={EvidenceSource.CLOUD_MONITORING: RecordingAdapter()},
        redactor=DeterministicEvidenceRedactor(),
    )
    app.dependency_overrides[get_incident_store] = lambda: store
    app.dependency_overrides[get_evidence_query_gateway] = lambda: gateway
    client = TestClient(app)
    try:
        response = client.post(
            f"/api/incidents/{record.incident.incident_id}/evidence-queries",
            json={
                "specialist": "metrics",
                "mapping_identifier": "checkout-success-rate",
                "query_round": 1,
            },
        )
        escaped = client.post(
            f"/api/incidents/{record.incident.incident_id}/evidence-queries",
            json={
                "specialist": "metrics",
                "mapping_identifier": "checkout-success-rate",
                "query_round": 1,
                "namespace": "other-namespace",
                "workload": "unmanaged",
                "metric_query": "up",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["evidence_queries"][0]["target"] == {
        "namespace": "online-boutique",
        "name": "recommendationservice",
        "kind": "Deployment",
    }
    assert escaped.status_code == 422


def _request(source: EvidenceSource) -> EvidenceProviderRequest:
    now = datetime.now(UTC)
    return EvidenceProviderRequest(
        query_id="query-1",
        incident_id="incident-1",
        source=source,
        kind=EvidenceQueryKind.METRICS
        if source is EvidenceSource.CLOUD_MONITORING
        else EvidenceQueryKind.WORKLOAD_STATE,
        scope={"namespace": "online-boutique", "name": "recommendationservice"},
        mapping_identifier="checkout-success-rate",
        query_template="sum(rate(checkout_requests_total[5m]))",
        started_at=now - timedelta(minutes=30),
        ended_at=now,
        maximum_items=25,
        deadline_seconds=5,
    )


class RecordingAdapter:
    def __init__(self, *, source: EvidenceSource = EvidenceSource.CLOUD_MONITORING) -> None:
        self.source = source
        self.requests: list[EvidenceProviderRequest] = []

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        self.requests.append(request)
        if self.source is EvidenceSource.CLOUD_MONITORING:
            return RawEvidenceObservation(
                source=self.source,
                kind=request.kind,
                scope=request.scope,
                provider_reference="fake://monitoring/query-1",
                observed_at=request.ended_at,
                item_count=2,
                metric_series=("series-1", "series-2"),
            )
        return RawEvidenceObservation(
            source=self.source,
            kind=request.kind,
            scope=request.scope,
            provider_reference="fake://kubernetes/query-1",
            observed_at=request.ended_at,
            content="bounded evidence",
        )


class FlakyAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        self.attempts += 1
        if self.attempts == 1:
            raise RetryableEvidenceProviderError("temporary provider failure")
        return super().query(request)


class AlwaysFailingAdapter:
    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        raise RuntimeError("provider-secret=do-not-persist")


class OverBudgetAdapter:
    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        return RawEvidenceObservation(
            source=request.source,
            kind=request.kind,
            scope=request.scope,
            provider_reference="fake://monitoring/query?token=provider-secret",
            observed_at=request.ended_at,
            item_count=25,
            metric_series=tuple(
                "token=result-secret" if number == 0 else f"series-{number}" for number in range(25)
            ),
        )


class RecordingKubernetesReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def read_workload_state(
        self, *, namespace: str, workload: str, maximum_items: int, deadline_seconds: float
    ) -> str:
        self.calls.append(("workload_state", namespace, workload, maximum_items))
        return "Deployment, pod, and rollout state"

    def read_pod_events(
        self, *, namespace: str, workload: str, maximum_items: int, deadline_seconds: float
    ) -> str:
        return "bounded pod events"

    def read_pod_logs(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_lines: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> str:
        return "bounded pod logs"

    def read_change_history(
        self, *, namespace: str, workload: str, maximum_items: int, deadline_seconds: float
    ) -> str:
        return "Deployment and ReplicaSet revisions"


class RecordingLoggingReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, datetime, datetime, float]] = []

    def query_workload_logs(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_lines: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> tuple[str, ...]:
        self.calls.append(
            (namespace, workload, maximum_lines, started_at, ended_at, deadline_seconds)
        )
        return ("line one", "line two")


class RecordingMonitoringReader:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query_time_series(
        self,
        *,
        query: str,
        namespace: str,
        workload: str,
        maximum_series: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> tuple[str, ...]:
        self.queries.append(query)
        return ("series-a", "series-b")

    def lookup_alert_policy(self, *, identifier: str, deadline_seconds: float) -> str:
        return "alert policy"
