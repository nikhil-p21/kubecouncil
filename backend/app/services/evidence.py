"""Deterministic collection of the immutable initial Evidence Window."""

import logging
import re
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from app.domain.incidents import (
    AlertSignal,
    ApplicationProfile,
    EvidenceMapping,
    EvidenceObservation,
    EvidenceProvider,
    EvidenceQueryKind,
    EvidenceRedactor,
    EvidenceRetrievalFailure,
    EvidenceWindow,
    IncidentStore,
    RawEvidenceObservation,
    WorkloadReference,
)

logger = logging.getLogger(__name__)
MAXIMUM_EVIDENCE_EXCERPT_CHARACTERS = 10_000


class EvidenceRedactionError(ValueError):
    """A deterministic redactor could not safely process provider content."""


class DeterministicEvidenceRedactor:
    """Small local redactor used before evidence reaches storage or the UI."""

    _SENSITIVE_VALUE = re.compile(
        r"""(?ix)
        (?:["']?(?:token|password|secret|api[_-]?key)["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|[^\s,;}\]]+)
        |
        (?:["']?authorization["']?\s*:\s*)
        (?:"[^"]*"|'[^']*'|[^,;}\]]+)
        """
    )

    def __init__(self, *, fail_for: str | None = None) -> None:
        self._fail_for = fail_for

    def redact(self, content: str) -> str:
        if self._fail_for is not None and self._fail_for in content:
            raise EvidenceRedactionError("redaction could not safely process provider content")
        return self._SENSITIVE_VALUE.sub("<redacted>", content)


class InitialEvidenceWindowCollector:
    """Captures bounded, profile-owned observations and records failures without raw content."""

    def __init__(self, provider: EvidenceProvider, redactor: EvidenceRedactor) -> None:
        self._provider = provider
        self._redactor = redactor

    def collect(
        self,
        store: IncidentStore,
        *,
        incident_id: str,
        profile: ApplicationProfile,
        signal: AlertSignal,
        window: EvidenceWindow,
    ) -> None:
        try:
            observations = self._provider.collect_initial(profile, signal, window)
        except Exception as error:
            logger.warning(
                "initial evidence provider failed incident_id=%s provider=%s error_type=%s",
                incident_id,
                type(self._provider).__name__,
                type(error).__name__,
            )
            self._append_provider_failure(store, incident_id)
            return
        maximum_observations = len(profile.evidence_mappings)
        for raw in observations[:maximum_observations]:
            self._collect_one(
                store,
                incident_id,
                profile,
                window,
                WorkloadReference(namespace=signal.namespace, name=signal.workload_name),
                raw,
            )
        if len(observations) > maximum_observations:
            self._append_provider_failure(
                store,
                incident_id,
                message="evidence budget exceeded; additional observations were not retained",
            )

    def _collect_one(
        self,
        store: IncidentStore,
        incident_id: str,
        profile: ApplicationProfile,
        window: EvidenceWindow,
        expected_scope: WorkloadReference,
        raw: RawEvidenceObservation,
    ) -> None:
        try:
            mapping = self._validate_scope_and_mapping(profile, window, expected_scope, raw)
            bounded_content, truncated = self._bound_content(profile, raw)
        except ValueError as error:
            self._append_failure(store, incident_id, raw, str(error))
            return
        try:
            redacted_excerpt = self._redactor.redact(bounded_content)
            provider_reference = self._redactor.redact(raw.provider_reference)
        except Exception as error:
            logger.warning(
                "initial evidence redaction failed incident_id=%s redactor=%s source=%s "
                "query=%s error_type=%s",
                incident_id,
                type(self._redactor).__name__,
                raw.source,
                raw.kind,
                type(error).__name__,
            )
            self._append_failure(
                store,
                incident_id,
                raw,
                "redaction failed; evidence was not retained",
            )
            return
        try:
            store.append_evidence(
                incident_id,
                EvidenceObservation(
                    evidence_id=f"evidence-{uuid4().hex}",
                    incident_id=incident_id,
                    source=raw.source,
                    query=raw.kind,
                    query_reference=mapping.identifier,
                    evidence_window_id=window.window_id,
                    observed_at=raw.observed_at or window.ended_at,
                    scope=raw.scope,
                    redacted_excerpt=redacted_excerpt,
                    content_hash=sha256(bounded_content.encode()).hexdigest(),
                    truncated=truncated,
                    provider_reference=provider_reference,
                ),
            )
        except ValueError as error:
            logger.warning(
                "initial evidence persistence failed incident_id=%s source=%s query=%s "
                "error_type=%s",
                incident_id,
                raw.source,
                raw.kind,
                type(error).__name__,
            )
            self._append_failure(
                store,
                incident_id,
                raw,
                "evidence validation failed; evidence was not retained",
            )

    @staticmethod
    def _validate_scope_and_mapping(
        profile: ApplicationProfile,
        window: EvidenceWindow,
        expected_scope: WorkloadReference,
        raw: RawEvidenceObservation,
    ) -> EvidenceMapping:
        if raw.scope != expected_scope:
            raise ValueError("evidence scope does not match the Incident target")
        observed_at = raw.observed_at or window.ended_at
        if not window.started_at <= observed_at <= window.ended_at:
            raise ValueError("evidence observation falls outside the immutable evidence window")
        mapping = next(
            (
                mapping
                for mapping in profile.evidence_mappings
                if (
                    mapping.source == raw.source
                    and mapping.kind == raw.kind
                    and mapping.scope == expected_scope
                )
            ),
            None,
        )
        if mapping is None:
            raise ValueError("evidence query is not allowlisted by the application profile")
        return mapping

    @staticmethod
    def _bound_content(
        profile: ApplicationProfile, raw: RawEvidenceObservation
    ) -> tuple[str, bool]:
        if raw.kind == EvidenceQueryKind.POD_LOGS:
            lines = raw.content.splitlines()
            bounded_lines = lines[: profile.evidence_budget.maximum_log_lines]
            bounded_content = "\n".join(bounded_lines)
            truncated = len(lines) > len(bounded_lines)
        elif raw.kind == EvidenceQueryKind.METRICS:
            bounded_series = raw.metric_series[: profile.evidence_budget.maximum_metric_series]
            bounded_content = "\n".join(bounded_series)
            truncated = len(raw.metric_series) > len(bounded_series)
        else:
            bounded_content = raw.content
            truncated = False
        if len(bounded_content) > MAXIMUM_EVIDENCE_EXCERPT_CHARACTERS:
            return bounded_content[:MAXIMUM_EVIDENCE_EXCERPT_CHARACTERS], True
        return bounded_content, truncated

    @staticmethod
    def _append_provider_failure(
        store: IncidentStore,
        incident_id: str,
        *,
        message: str = "evidence provider failed; no evidence was retained",
    ) -> None:
        store.append_evidence_retrieval_failure(
            incident_id,
            EvidenceRetrievalFailure(
                failure_id=f"evidence-failure-{uuid4().hex}",
                incident_id=incident_id,
                occurred_at=datetime.now(UTC),
                message=message,
            ),
        )

    @staticmethod
    def _append_failure(
        store: IncidentStore,
        incident_id: str,
        raw: RawEvidenceObservation,
        message: str,
    ) -> None:
        store.append_evidence_retrieval_failure(
            incident_id,
            EvidenceRetrievalFailure(
                failure_id=f"evidence-failure-{uuid4().hex}",
                incident_id=incident_id,
                source=raw.source,
                query=raw.kind,
                scope=raw.scope,
                occurred_at=datetime.now(UTC),
                message=message,
            ),
        )
