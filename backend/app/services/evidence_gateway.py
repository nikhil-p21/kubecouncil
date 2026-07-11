"""Incident-scoped gateway for bounded, read-only evidence queries."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from typing import Protocol
from uuid import uuid4

from app.domain.incidents import (
    AuditEvent,
    EvidenceMapping,
    EvidenceObservation,
    EvidenceProviderRequest,
    EvidenceQuery,
    EvidenceQueryAdapter,
    EvidenceQueryKind,
    EvidenceRedactor,
    EvidenceRetrievalFailure,
    EvidenceSource,
    IncidentStore,
    InvestigationRecord,
    RawEvidenceObservation,
    SpecialistRole,
)
from app.services.evidence import MAXIMUM_EVIDENCE_EXCERPT_CHARACTERS

logger = logging.getLogger(__name__)


class EvidenceGatewayError(ValueError):
    """A query could not be executed within its deterministic safety boundary."""


class RetryableEvidenceProviderError(RuntimeError):
    """A read-only provider operation may be retried without changing external state."""


class KubernetesEvidenceReader(Protocol):
    """Purpose-built Kubernetes reads; broad resource discovery is intentionally absent."""

    def read_workload_state(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_items: int,
        deadline_seconds: float,
    ) -> str: ...

    def read_pod_events(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_items: int,
        deadline_seconds: float,
    ) -> str: ...

    def read_pod_logs(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_lines: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> str: ...

    def read_change_history(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_items: int,
        deadline_seconds: float,
    ) -> str: ...


class CloudLoggingReader(Protocol):
    """Approved Cloud Logging operation exposed to the gateway."""

    def query_workload_logs(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_lines: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> tuple[str, ...]: ...


class CloudMonitoringReader(Protocol):
    """Approved Cloud Monitoring operations exposed to the gateway."""

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
    ) -> tuple[str, ...]: ...

    def lookup_alert_policy(self, *, identifier: str, deadline_seconds: float) -> str: ...


class KubernetesEvidenceAdapter:
    """Translates allowlisted evidence kinds into narrow Kubernetes reader calls."""

    def __init__(self, reader: KubernetesEvidenceReader) -> None:
        self._reader = reader

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        if request.source is not EvidenceSource.KUBERNETES:
            raise EvidenceGatewayError("Kubernetes adapter received a non-Kubernetes request")
        namespace = request.scope.namespace
        workload = request.scope.name
        if request.kind is EvidenceQueryKind.WORKLOAD_STATE:
            content = self._reader.read_workload_state(
                namespace=namespace,
                workload=workload,
                maximum_items=request.maximum_items,
                deadline_seconds=request.deadline_seconds,
            )
        elif request.kind is EvidenceQueryKind.POD_EVENTS:
            content = self._reader.read_pod_events(
                namespace=namespace,
                workload=workload,
                maximum_items=request.maximum_items,
                deadline_seconds=request.deadline_seconds,
            )
        elif request.kind is EvidenceQueryKind.POD_LOGS:
            content = self._reader.read_pod_logs(
                namespace=namespace,
                workload=workload,
                maximum_lines=request.maximum_items,
                started_at=request.started_at,
                ended_at=request.ended_at,
                deadline_seconds=request.deadline_seconds,
            )
        elif request.kind is EvidenceQueryKind.CHANGE_HISTORY:
            content = self._reader.read_change_history(
                namespace=namespace,
                workload=workload,
                maximum_items=request.maximum_items,
                deadline_seconds=request.deadline_seconds,
            )
        else:
            raise EvidenceGatewayError(f"unsupported Kubernetes evidence operation: {request.kind}")
        return RawEvidenceObservation(
            source=EvidenceSource.KUBERNETES,
            kind=request.kind,
            scope=request.scope,
            content=content,
            provider_reference=(
                f"kubernetes://{namespace}/deployments/{workload}/{request.kind.value}"
            ),
            observed_at=request.ended_at,
        )


class CloudLoggingEvidenceAdapter:
    """Exposes only bounded, workload-scoped Cloud Logging retrieval."""

    def __init__(self, reader: CloudLoggingReader) -> None:
        self._reader = reader

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        if (
            request.source is not EvidenceSource.CLOUD_LOGGING
            or request.kind is not EvidenceQueryKind.POD_LOGS
        ):
            raise EvidenceGatewayError(
                f"unsupported Cloud Logging evidence operation: {request.kind}"
            )
        lines = self._reader.query_workload_logs(
            namespace=request.scope.namespace,
            workload=request.scope.name,
            maximum_lines=request.maximum_items,
            started_at=request.started_at,
            ended_at=request.ended_at,
            deadline_seconds=request.deadline_seconds,
        )[: request.maximum_items]
        return RawEvidenceObservation(
            source=EvidenceSource.CLOUD_LOGGING,
            kind=request.kind,
            scope=request.scope,
            content="\n".join(lines),
            provider_reference=(f"cloud-logging://{request.scope.namespace}/{request.scope.name}"),
            observed_at=request.ended_at,
            item_count=len(lines),
        )


class CloudMonitoringEvidenceAdapter:
    """Exposes only profile-owned time-series queries and alert-policy lookup."""

    def __init__(self, reader: CloudMonitoringReader) -> None:
        self._reader = reader

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        if request.source is not EvidenceSource.CLOUD_MONITORING:
            raise EvidenceGatewayError(
                f"unsupported Cloud Monitoring evidence operation: {request.kind}"
            )
        if request.kind is EvidenceQueryKind.METRICS:
            if request.query_template is None:
                raise EvidenceGatewayError("metric query is missing its profile-owned template")
            series = self._reader.query_time_series(
                query=request.query_template,
                namespace=request.scope.namespace,
                workload=request.scope.name,
                maximum_series=request.maximum_items,
                started_at=request.started_at,
                ended_at=request.ended_at,
                deadline_seconds=request.deadline_seconds,
            )[: request.maximum_items]
            return RawEvidenceObservation(
                source=EvidenceSource.CLOUD_MONITORING,
                kind=request.kind,
                scope=request.scope,
                provider_reference=(
                    f"cloud-monitoring://{request.scope.namespace}/{request.mapping_identifier}"
                ),
                observed_at=request.ended_at,
                item_count=len(series),
                metric_series=series,
            )
        if request.kind is EvidenceQueryKind.ALERT_POLICY:
            content = self._reader.lookup_alert_policy(
                identifier=request.mapping_identifier,
                deadline_seconds=request.deadline_seconds,
            )
            return RawEvidenceObservation(
                source=EvidenceSource.CLOUD_MONITORING,
                kind=request.kind,
                scope=request.scope,
                content=content,
                provider_reference=(
                    f"cloud-monitoring://alert-policies/{request.mapping_identifier}"
                ),
                observed_at=request.ended_at,
            )
        raise EvidenceGatewayError(
            f"unsupported Cloud Monitoring evidence operation: {request.kind}"
        )


class FakeEvidenceQueryAdapter:
    """Credential-free local adapter that obeys the same resolved request contract."""

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation:
        if request.kind is EvidenceQueryKind.METRICS:
            return RawEvidenceObservation(
                source=request.source,
                kind=request.kind,
                scope=request.scope,
                provider_reference=f"fake://{request.source}/{request.mapping_identifier}",
                observed_at=request.ended_at,
                item_count=2,
                metric_series=("request success rate 91%", "p95 latency 2400ms"),
            )
        return RawEvidenceObservation(
            source=request.source,
            kind=request.kind,
            scope=request.scope,
            content=f"Bounded follow-up evidence for {request.mapping_identifier}.",
            provider_reference=f"fake://{request.source}/{request.mapping_identifier}",
            observed_at=request.ended_at,
        )


class EvidenceQueryGateway:
    """Resolves model requests against durable Incident and profile authority."""

    def __init__(
        self,
        *,
        adapters: Mapping[EvidenceSource, EvidenceQueryAdapter],
        redactor: EvidenceRedactor,
        deadline_seconds: float = 5,
        maximum_attempts: int = 2,
    ) -> None:
        if not 0 < deadline_seconds <= 30:
            raise ValueError("evidence provider deadline must be between 0 and 30 seconds")
        if not 1 <= maximum_attempts <= 2:
            raise ValueError("evidence queries allow one attempt and at most one safe retry")
        self._adapters = adapters
        self._redactor = redactor
        self._deadline_seconds = deadline_seconds
        self._maximum_attempts = maximum_attempts

    def execute(
        self,
        store: IncidentStore,
        *,
        incident_id: str,
        specialist: SpecialistRole,
        mapping_identifier: str,
        query_round: int,
    ) -> InvestigationRecord:
        record = store.get(incident_id)
        if record is None:
            raise EvidenceGatewayError("incident does not exist")
        mapping = self._resolve_mapping(record, mapping_identifier)
        self._validate_budget(record, specialist, query_round)
        adapter = self._adapters.get(mapping.source)
        if adapter is None:
            raise EvidenceGatewayError("allowlisted evidence provider is unavailable")
        query_id = f"query-{uuid4().hex}"
        query = EvidenceQuery(
            query_id=query_id,
            incident_id=incident_id,
            specialist=specialist,
            kind=mapping.kind,
            target=mapping.scope,
            requested_at=datetime.now(UTC),
            query_round=query_round,
        )
        store.append_evidence_query(incident_id, query)
        store.append_audit_event(
            incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=incident_id,
                event_type="evidence_query_requested",
                occurred_at=datetime.now(UTC),
                actor=f"specialist:{specialist.value}",
                details={
                    "query_id": query_id,
                    "mapping_identifier": mapping.identifier,
                    "source": mapping.source.value,
                },
            ),
        )
        provider_request = self._provider_request(record, mapping, query_id)
        try:
            raw, attempts = self._query(adapter, provider_request)
            evidence = self._safe_observation(record, mapping, raw, query_id)
            store.append_evidence(incident_id, evidence)
        except Exception as error:
            logger.warning(
                "evidence query failed incident_id=%s query_id=%s provider=%s error_type=%s",
                incident_id,
                query_id,
                mapping.source,
                type(error).__name__,
            )
            self._record_failure(store, query, mapping)
            raise EvidenceGatewayError(
                "evidence query provider failed; no evidence was retained"
            ) from error
        return store.append_audit_event(
            incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=incident_id,
                event_type="evidence_query_completed",
                occurred_at=datetime.now(UTC),
                actor="evidence-gateway",
                details={
                    "query_id": query_id,
                    "evidence_id": evidence.evidence_id,
                    "attempts": str(attempts),
                },
            ),
        )

    @staticmethod
    def _resolve_mapping(record: InvestigationRecord, mapping_identifier: str) -> EvidenceMapping:
        mapping = next(
            (
                item
                for item in record.application_profile.evidence_mappings
                if item.identifier == mapping_identifier
            ),
            None,
        )
        if mapping is None:
            raise EvidenceGatewayError(
                "evidence query is not allowlisted by the Incident Application Profile"
            )
        return mapping

    @staticmethod
    def _validate_budget(
        record: InvestigationRecord, specialist: SpecialistRole, query_round: int
    ) -> None:
        maximum = record.application_profile.evidence_budget.maximum_queries_per_specialist
        prior = [query for query in record.evidence_queries if query.specialist is specialist]
        if query_round < 1 or query_round > maximum or len(prior) >= maximum:
            raise EvidenceGatewayError("specialist evidence query budget is exhausted")
        if prior and query_round < max(query.query_round for query in prior):
            raise EvidenceGatewayError("specialist evidence query rounds cannot move backwards")

    def _provider_request(
        self,
        record: InvestigationRecord,
        mapping: EvidenceMapping,
        query_id: str,
    ) -> EvidenceProviderRequest:
        budget = record.application_profile.evidence_budget
        if mapping.kind is EvidenceQueryKind.POD_LOGS:
            maximum_items = budget.maximum_log_lines
        elif mapping.kind is EvidenceQueryKind.METRICS:
            maximum_items = budget.maximum_metric_series
        else:
            maximum_items = min(100, budget.maximum_log_lines)
        return EvidenceProviderRequest(
            query_id=query_id,
            incident_id=record.incident.incident_id,
            source=mapping.source,
            kind=mapping.kind,
            scope=mapping.scope,
            mapping_identifier=mapping.identifier,
            query_template=mapping.query_template,
            started_at=record.evidence_window.started_at,
            ended_at=record.evidence_window.ended_at,
            maximum_items=maximum_items,
            deadline_seconds=self._deadline_seconds,
        )

    def _query(
        self, adapter: EvidenceQueryAdapter, request: EvidenceProviderRequest
    ) -> tuple[RawEvidenceObservation, int]:
        started = monotonic()
        for attempt in range(1, self._maximum_attempts + 1):
            try:
                raw = adapter.query(request)
                if monotonic() - started > request.deadline_seconds:
                    raise EvidenceGatewayError("evidence provider deadline exceeded")
                return raw, attempt
            except RetryableEvidenceProviderError:
                if attempt == self._maximum_attempts:
                    raise
        raise RuntimeError("evidence query exhausted without a provider result")

    def _safe_observation(
        self,
        record: InvestigationRecord,
        mapping: EvidenceMapping,
        raw: RawEvidenceObservation,
        query_id: str,
    ) -> EvidenceObservation:
        if (raw.source, raw.kind, raw.scope) != (
            mapping.source,
            mapping.kind,
            mapping.scope,
        ):
            raise EvidenceGatewayError("provider response escaped its resolved evidence scope")
        observed_at = raw.observed_at or record.evidence_window.ended_at
        if not record.evidence_window.started_at <= observed_at <= record.evidence_window.ended_at:
            raise EvidenceGatewayError("provider response falls outside the Evidence Window")
        content, truncated = self._bounded_content(record, raw)
        redacted = self._redactor.redact(content)
        provider_reference = self._redactor.redact(raw.provider_reference)
        return EvidenceObservation(
            evidence_id=f"evidence-{uuid4().hex}",
            incident_id=record.incident.incident_id,
            source=raw.source,
            query=raw.kind,
            query_reference=mapping.identifier,
            evidence_query_id=query_id,
            evidence_window_id=record.evidence_window.window_id,
            observed_at=observed_at,
            scope=raw.scope,
            redacted_excerpt=redacted,
            content_hash=sha256(content.encode()).hexdigest(),
            truncated=truncated,
            provider_reference=provider_reference,
        )

    @staticmethod
    def _bounded_content(
        record: InvestigationRecord, raw: RawEvidenceObservation
    ) -> tuple[str, bool]:
        budget = record.application_profile.evidence_budget
        if raw.kind is EvidenceQueryKind.METRICS:
            bounded = raw.metric_series[: budget.maximum_metric_series]
            content = "\n".join(bounded)
            truncated = len(raw.metric_series) > len(bounded)
        elif raw.kind is EvidenceQueryKind.POD_LOGS:
            lines = raw.content.splitlines()
            bounded_lines = lines[: budget.maximum_log_lines]
            content = "\n".join(bounded_lines)
            truncated = len(lines) > len(bounded_lines)
        else:
            content = raw.content
            truncated = raw.item_count > min(100, budget.maximum_log_lines)
            if truncated:
                raise EvidenceGatewayError("provider returned more resources than requested")
        if len(content) > MAXIMUM_EVIDENCE_EXCERPT_CHARACTERS:
            return content[:MAXIMUM_EVIDENCE_EXCERPT_CHARACTERS], True
        return content, truncated

    @staticmethod
    def _record_failure(
        store: IncidentStore, query: EvidenceQuery, mapping: EvidenceMapping
    ) -> None:
        store.append_evidence_retrieval_failure(
            query.incident_id,
            EvidenceRetrievalFailure(
                failure_id=f"evidence-failure-{uuid4().hex}",
                incident_id=query.incident_id,
                source=mapping.source,
                query=mapping.kind,
                scope=mapping.scope,
                occurred_at=datetime.now(UTC),
                message="evidence query provider failed; no evidence was retained",
            ),
        )
        store.append_audit_event(
            query.incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=query.incident_id,
                event_type="evidence_query_failed",
                occurred_at=datetime.now(UTC),
                actor="evidence-gateway",
                details={"query_id": query.query_id},
            ),
        )
