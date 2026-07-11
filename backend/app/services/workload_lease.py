"""Firestore-shaped workload leases for deterministic Executor serialization."""

import importlib
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol, cast
from uuid import uuid4

from app.domain.incidents import WorkloadLease, WorkloadReference


class LeaseDatabase(Protocol):
    """Atomic document operation supporting absent and deleted lease documents."""

    def transaction(
        self,
        document_id: str,
        update: Callable[[dict[str, object] | None], dict[str, object] | None],
    ) -> dict[str, object] | None: ...


class InMemoryLeaseDatabase:
    """Firestore-shaped atomic fake used by Executor tests."""

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, object]] = {}
        self._lock = RLock()

    def transaction(
        self,
        document_id: str,
        update: Callable[[dict[str, object] | None], dict[str, object] | None],
    ) -> dict[str, object] | None:
        with self._lock:
            current = self._documents.get(document_id)
            replacement = update(current.copy() if current is not None else None)
            if replacement is None:
                self._documents.pop(document_id, None)
                return None
            self._documents[document_id] = replacement.copy()
            return replacement.copy()


class _FirestoreSnapshot(Protocol):
    @property
    def exists(self) -> bool: ...

    def to_dict(self) -> dict[str, object] | None: ...


class _FirestoreDocument(Protocol):
    def get(self, *, transaction: "_FirestoreTransaction") -> _FirestoreSnapshot: ...


class _FirestoreCollection(Protocol):
    def document(self, document_id: str) -> _FirestoreDocument: ...


class _FirestoreTransaction(Protocol):
    def set(self, reference: _FirestoreDocument, value: dict[str, object]) -> None: ...

    def delete(self, reference: _FirestoreDocument) -> None: ...


class _FirestoreClient(Protocol):
    def collection(self, path: str) -> _FirestoreCollection: ...

    def transaction(self) -> _FirestoreTransaction: ...


class _FirestoreModule(Protocol):
    def transactional(
        self,
        callback: Callable[[_FirestoreTransaction], dict[str, object] | None],
    ) -> Callable[[_FirestoreTransaction], dict[str, object] | None]: ...


class GoogleFirestoreLeaseDatabase:
    """Live Firestore adapter; one transaction owns create, renew, and release races."""

    def __init__(self, client: _FirestoreClient, *, collection: str = "workload-leases") -> None:
        self._client = client
        self._collection = client.collection(collection)

    def transaction(
        self,
        document_id: str,
        update: Callable[[dict[str, object] | None], dict[str, object] | None],
    ) -> dict[str, object] | None:
        try:
            module = cast(_FirestoreModule, importlib.import_module("google.cloud.firestore"))
        except ModuleNotFoundError as error:
            raise RuntimeError("google-cloud-firestore is required for live leases") from error
        reference = self._collection.document(document_id)

        def perform(transaction: _FirestoreTransaction) -> dict[str, object] | None:
            snapshot = reference.get(transaction=transaction)
            current = snapshot.to_dict() if snapshot.exists else None
            replacement = update(current)
            if replacement is None:
                transaction.delete(reference)
            else:
                transaction.set(reference, replacement)
            return replacement

        return module.transactional(perform)(self._client.transaction())


class FirestoreWorkloadLeaseStore:
    """Provider-independent lease semantics persisted as Firestore domain JSON."""

    def __init__(
        self,
        database: LeaseDatabase,
        *,
        ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self._database = database
        self._ttl = ttl

    def acquire(
        self,
        target: WorkloadReference,
        *,
        intervention_id: str,
        owner: str,
        now: datetime,
    ) -> WorkloadLease | None:
        lease_key = self._key(target)
        acquired = False

        def update(current: dict[str, object] | None) -> dict[str, object]:
            nonlocal acquired
            existing = WorkloadLease.model_validate(current) if current is not None else None
            if existing is not None and existing.expires_at > now:
                return existing.model_dump(mode="json")
            acquired = True
            lease = WorkloadLease(
                lease_key=lease_key,
                target=target,
                intervention_id=intervention_id,
                owner=owner,
                token=uuid4().hex,
                acquired_at=now,
                expires_at=now + self._ttl,
            )
            return lease.model_dump(mode="json")

        document = self._database.transaction(lease_key, update)
        if not acquired or document is None:
            return None
        return WorkloadLease.model_validate(document)

    def renew(self, lease: WorkloadLease, *, now: datetime) -> WorkloadLease:
        renewed: WorkloadLease | None = None

        def update(current: dict[str, object] | None) -> dict[str, object] | None:
            nonlocal renewed
            if current is None:
                raise ValueError("workload lease was lost before renewal")
            existing = WorkloadLease.model_validate(current)
            if existing.token != lease.token or existing.owner != lease.owner:
                raise ValueError("workload lease ownership changed before renewal")
            if existing.expires_at <= now:
                raise ValueError("workload lease expired before renewal")
            renewed = existing.model_copy(update={"expires_at": now + self._ttl})
            return renewed.model_dump(mode="json")

        self._database.transaction(lease.lease_key, update)
        if renewed is None:
            raise RuntimeError("workload lease renewal completed without a result")
        return renewed

    def release(self, lease: WorkloadLease) -> None:
        def update(current: dict[str, object] | None) -> None:
            if current is None:
                return None
            existing = WorkloadLease.model_validate(current)
            if existing.token != lease.token or existing.owner != lease.owner:
                raise ValueError("cannot release a workload lease owned by another Executor")
            return None

        self._database.transaction(lease.lease_key, update)

    @staticmethod
    def _key(target: WorkloadReference) -> str:
        return f"{target.namespace}--{target.kind.lower()}--{target.name}"


__all__ = [
    "FirestoreWorkloadLeaseStore",
    "GoogleFirestoreLeaseDatabase",
    "InMemoryLeaseDatabase",
    "LeaseDatabase",
]
