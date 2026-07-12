"""Durable IncidentStore adapters with provider-independent domain documents."""

import importlib
from collections.abc import Callable
from threading import RLock
from typing import Protocol, cast

from app.domain.incident_fakes import InMemoryIncidentStore
from app.domain.incidents import (
    AlertSignal,
    AlertSignalEvidence,
    ApplicationProfile,
    Approval,
    AuditEvent,
    CoordinatorOutput,
    EvidenceObservation,
    EvidenceQuery,
    EvidenceRetrievalFailure,
    Incident,
    Intervention,
    InvestigationRecord,
    ModelInvocation,
    PolicyDecision,
    RecoveryAssessment,
    SpecialistFinding,
)


class DocumentDatabase(Protocol):
    """Minimal transactional document boundary implemented by a Firestore SDK adapter."""

    def create(self, document_id: str, value: dict[str, object]) -> None: ...

    def read(self, document_id: str) -> dict[str, object] | None: ...

    def list(self) -> tuple[dict[str, object], ...]: ...

    def transaction(
        self,
        document_id: str,
        update: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]: ...


class _FirestoreSnapshot(Protocol):
    @property
    def exists(self) -> bool: ...

    def to_dict(self) -> dict[str, object] | None: ...


class _FirestoreTransaction(Protocol):
    def set(self, reference: "_FirestoreDocument", value: dict[str, object]) -> None: ...


class _FirestoreDocument(Protocol):
    def create(self, value: dict[str, object]) -> object: ...

    def get(self, *, transaction: _FirestoreTransaction | None = None) -> _FirestoreSnapshot: ...


class _FirestoreCollection(Protocol):
    def document(self, document_id: str) -> _FirestoreDocument: ...

    def stream(self) -> tuple[_FirestoreSnapshot, ...]: ...


class _FirestoreClient(Protocol):
    def collection(self, path: str) -> _FirestoreCollection: ...

    def transaction(self) -> _FirestoreTransaction: ...


class _FirestoreModule(Protocol):
    def transactional(
        self,
        callback: Callable[[_FirestoreTransaction], dict[str, object]],
    ) -> Callable[[_FirestoreTransaction], dict[str, object]]: ...


class GoogleFirestoreDocumentDatabase:
    """Small Google Cloud Firestore SDK adapter; live verification is integration-only."""

    def __init__(self, client: _FirestoreClient, *, collection: str = "incidents") -> None:
        self._client = client
        self._collection = client.collection(collection)

    def create(self, document_id: str, value: dict[str, object]) -> None:
        self._collection.document(document_id).create(value)

    def read(self, document_id: str) -> dict[str, object] | None:
        snapshot = self._collection.document(document_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def list(self) -> tuple[dict[str, object], ...]:
        documents: list[dict[str, object]] = []
        for snapshot in self._collection.stream():
            value = snapshot.to_dict()
            if value is not None:
                documents.append(value)
        return tuple(documents)

    def transaction(
        self,
        document_id: str,
        update: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        try:
            module = cast(_FirestoreModule, importlib.import_module("google.cloud.firestore"))
        except ModuleNotFoundError as error:
            raise RuntimeError("google-cloud-firestore is required for the live store") from error
        reference = self._collection.document(document_id)

        def perform(transaction: _FirestoreTransaction) -> dict[str, object]:
            snapshot = reference.get(transaction=transaction)
            current = snapshot.to_dict() if snapshot.exists else None
            if current is None:
                raise ValueError("incident does not exist")
            replacement = update(current)
            transaction.set(reference, replacement)
            return replacement

        return module.transactional(perform)(self._client.transaction())


class InMemoryDocumentDatabase:
    """Firestore-shaped transactional fake used by local parity tests."""

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, object]] = {}
        self._lock = RLock()

    def create(self, document_id: str, value: dict[str, object]) -> None:
        with self._lock:
            if document_id in self._documents:
                raise ValueError("incident already exists")
            self._documents[document_id] = value.copy()

    def read(self, document_id: str) -> dict[str, object] | None:
        with self._lock:
            document = self._documents.get(document_id)
            return document.copy() if document is not None else None

    def list(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(document.copy() for document in self._documents.values())

    def transaction(
        self,
        document_id: str,
        update: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        with self._lock:
            current = self._documents.get(document_id)
            if current is None:
                raise ValueError("incident does not exist")
            replacement = update(current.copy())
            self._documents[document_id] = replacement.copy()
            return replacement.copy()


class FirestoreIncidentStore:
    """Firestore-ready store using atomic document transactions and domain JSON only.

    A production adapter supplies ``DocumentDatabase`` using Firestore transactions. Keeping
    that SDK-specific translation outside this class prevents Firestore values leaking into
    domain contracts and lets the same behavior run against the local transactional fake.
    """

    def __init__(self, database: DocumentDatabase) -> None:
        self._database = database

    def create(self, profile: ApplicationProfile, signal: AlertSignal) -> InvestigationRecord:
        delegate = InMemoryIncidentStore()
        record = delegate.create(profile, signal)
        self._database.create(record.incident.incident_id, record.model_dump(mode="json"))
        return record

    def get(self, incident_id: str) -> InvestigationRecord | None:
        document = self._database.read(incident_id)
        return InvestigationRecord.model_validate(document) if document is not None else None

    def list(self) -> tuple[InvestigationRecord, ...]:
        records = [
            InvestigationRecord.model_validate(document) for document in self._database.list()
        ]
        return tuple(sorted(records, key=lambda item: item.incident.opened_at, reverse=True))

    def append_evidence(
        self, incident_id: str, evidence: EvidenceObservation
    ) -> InvestigationRecord:
        return self._mutate(incident_id, lambda store: store.append_evidence(incident_id, evidence))

    def append_alert_signal(
        self, incident_id: str, signal: AlertSignalEvidence
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id, lambda store: store.append_alert_signal(incident_id, signal)
        )

    def append_evidence_retrieval_failure(
        self, incident_id: str, failure: EvidenceRetrievalFailure
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id,
            lambda store: store.append_evidence_retrieval_failure(incident_id, failure),
        )

    def append_evidence_query(self, incident_id: str, query: EvidenceQuery) -> InvestigationRecord:
        return self._mutate(
            incident_id, lambda store: store.append_evidence_query(incident_id, query)
        )

    def append_finding(
        self, incident_id: str, finding: SpecialistFinding
    ) -> InvestigationRecord:
        return self._mutate(incident_id, lambda store: store.append_finding(incident_id, finding))

    def append_model_invocation(
        self, incident_id: str, invocation: ModelInvocation
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id, lambda store: store.append_model_invocation(incident_id, invocation)
        )

    def complete_investigation(
        self, incident_id: str, output: CoordinatorOutput
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id, lambda store: store.complete_investigation(incident_id, output)
        )

    def record_policy_decision(
        self, incident_id: str, decision: PolicyDecision
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id, lambda store: store.record_policy_decision(incident_id, decision)
        )

    def record_approval_decision(
        self,
        incident_id: str,
        expected_version: int,
        approval: Approval,
        event: AuditEvent,
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id,
            lambda store: store.record_approval_decision(
                incident_id, expected_version, approval, event
            ),
        )

    def record_intervention(
        self,
        incident_id: str,
        expected_version: int,
        intervention: Intervention,
        event: AuditEvent,
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id,
            lambda store: store.record_intervention(
                incident_id, expected_version, intervention, event
            ),
        )

    def update_intervention(
        self,
        incident_id: str,
        intervention: Intervention,
        event: AuditEvent,
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id,
            lambda store: store.update_intervention(incident_id, intervention, event),
        )

    def record_recovery_assessment(
        self,
        incident_id: str,
        expected_version: int,
        assessment: RecoveryAssessment,
        event: AuditEvent,
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id,
            lambda store: store.record_recovery_assessment(
                incident_id,
                expected_version,
                assessment,
                event,
            ),
        )

    def append_audit_event(self, incident_id: str, event: AuditEvent) -> InvestigationRecord:
        return self._mutate(incident_id, lambda store: store.append_audit_event(incident_id, event))

    def compare_and_set(
        self, incident_id: str, expected_version: int, replacement: Incident
    ) -> InvestigationRecord:
        return self._mutate(
            incident_id,
            lambda store: store.compare_and_set(incident_id, expected_version, replacement),
        )

    def timeline(self, incident_id: str, *, after: int = 0) -> tuple[AuditEvent, ...]:
        record = self.get(incident_id)
        if record is None:
            raise ValueError("incident does not exist")
        return tuple(event for event in record.audit_events if event.cursor > after)

    def _mutate(
        self,
        incident_id: str,
        operation: Callable[[InMemoryIncidentStore], InvestigationRecord],
    ) -> InvestigationRecord:
        result: InvestigationRecord | None = None

        def update(document: dict[str, object]) -> dict[str, object]:
            nonlocal result
            current = InvestigationRecord.model_validate(document)
            delegate = InMemoryIncidentStore()
            delegate._records[incident_id] = current  # noqa: SLF001 - adapter validation seam
            result = operation(delegate)
            return result.model_dump(mode="json")

        self._database.transaction(incident_id, update)
        if result is None:
            raise RuntimeError("incident transaction completed without a result")
        return result
