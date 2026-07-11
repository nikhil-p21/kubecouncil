"""Hash-bound Intervention messages published by the unprivileged Investigator."""

import json
from datetime import datetime
from threading import RLock
from typing import Protocol

from app.domain.approval import approval_matches_record, canonical_hash
from app.domain.incidents import (
    Approval,
    ApprovalDecision,
    InterventionRequest,
    InvestigationRecord,
    PolicyStatus,
)


class InterventionPublishError(RuntimeError):
    """Raised when an approved record cannot produce a safe Executor request."""


def build_intervention_request(
    record: InvestigationRecord,
    approval: Approval,
    *,
    requested_at: datetime,
) -> InterventionRequest:
    """Build the one immutable queue message authorized by a current Approval."""

    proposal = record.proposal
    policy = record.policy_decision
    if approval.decision is not ApprovalDecision.APPROVED:
        raise InterventionPublishError("only an approved proposal may publish an Intervention")
    if proposal is None or policy is None or policy.status is not PolicyStatus.PASSED:
        raise InterventionPublishError("Intervention publishing requires passed policy")
    if policy.patch is None:
        raise InterventionPublishError("Intervention publishing requires the reviewed patch")
    if approval.proposal_id != proposal.proposal_id:
        raise InterventionPublishError("Approval does not authorize the recorded proposal")
    if approval.expires_at <= requested_at or not approval_matches_record(record, approval):
        raise InterventionPublishError("Approval is stale and cannot publish an Intervention")
    idempotency_key = canonical_hash(
        {
            "incident_id": record.incident.incident_id,
            "proposal_id": proposal.proposal_id,
            "approval_id": approval.approval_id,
            "target": proposal.action.target.model_dump(mode="json"),
        }
    )
    request_without_hash: dict[str, object] = {
        "request_id": f"request-{idempotency_key[:24]}",
        "incident_id": record.incident.incident_id,
        "proposal_id": proposal.proposal_id,
        "approval_id": approval.approval_id,
        "target": proposal.action.target,
        "patch": policy.patch,
        "requested_at": requested_at,
        "idempotency_key": idempotency_key,
    }
    draft = InterventionRequest.model_validate(
        {**request_without_hash, "payload_hash": "00000000"}
    )
    return draft.model_copy(update={"payload_hash": intervention_request_hash(draft)})


def intervention_request_hash(request: InterventionRequest) -> str:
    """Recompute the payload hash without trusting the message's supplied hash."""

    payload = request.model_dump(mode="json", exclude={"payload_hash"})
    return canonical_hash(payload)


class InMemoryInterventionQueue:
    """Idempotent local publisher fake; duplicate keys never enqueue twice."""

    def __init__(self) -> None:
        self._requests: dict[str, InterventionRequest] = {}
        self._lock = RLock()

    def publish(self, request: InterventionRequest) -> None:
        with self._lock:
            existing = self._requests.get(request.idempotency_key)
            if existing is not None and existing != request:
                raise InterventionPublishError("idempotency key was reused for another payload")
            self._requests[request.idempotency_key] = request

    def pending(self) -> tuple[InterventionRequest, ...]:
        with self._lock:
            return tuple(self._requests.values())


class _PublishFuture(Protocol):
    def result(self) -> str: ...


class _PubSubPublisher(Protocol):
    def publish(self, topic: str, data: bytes, **attrs: str) -> _PublishFuture: ...


class PubSubInterventionPublisher:
    """Synchronous durable publisher adapter used at the privilege boundary."""

    def __init__(self, publisher: _PubSubPublisher, *, topic: str) -> None:
        self._publisher = publisher
        self._topic = topic

    def publish(self, request: InterventionRequest) -> None:
        data = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        self._publisher.publish(
            self._topic,
            data,
            idempotency_key=request.idempotency_key,
            payload_hash=request.payload_hash,
        ).result()


__all__ = [
    "InMemoryInterventionQueue",
    "InterventionPublishError",
    "PubSubInterventionPublisher",
    "build_intervention_request",
    "intervention_request_hash",
]
