"""Freshness-bound human Approval workflow."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.approval import build_approval_binding
from app.domain.identity import OperatorIdentity, OperatorRole
from app.domain.incidents import (
    Approval,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalReview,
    AuditEvent,
    IncidentStore,
    InterventionPublisher,
    InvestigationRecord,
    PolicyKubernetesProvider,
)
from app.services.intervention_queue import build_intervention_request


class ApprovalError(RuntimeError):
    """Raised when identity, completeness, freshness, or claim checks fail."""


class ApprovalService:
    def __init__(
        self,
        kubernetes: PolicyKubernetesProvider,
        *,
        review_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
        publisher: InterventionPublisher | None = None,
    ) -> None:
        self._kubernetes = kubernetes
        self._review_ttl = review_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._publisher = publisher

    def review(
        self,
        store: IncidentStore,
        incident_id: str,
        identity: OperatorIdentity,
    ) -> ApprovalReview:
        self._require_responder(identity)
        record = store.get(incident_id)
        if record is None:
            raise ApprovalError("incident does not exist")
        if record.approvals:
            raise ApprovalError("proposal is already decided")
        if record.proposal is None:
            raise ApprovalError("incident has no Remediation Proposal to review")
        state = self._kubernetes.inspect_deployment(record.proposal.action.target)
        if state is None:
            raise ApprovalError("current Deployment state is unavailable")
        try:
            binding = build_approval_binding(
                record,
                state,
                expires_at=self._clock() + self._review_ttl,
            )
        except ValueError as error:
            raise ApprovalError(str(error)) from error
        return ApprovalReview(
            incident_id=incident_id,
            proposal_id=record.proposal.proposal_id,
            responder_principal=identity.principal,
            binding=binding,
        )

    def decide(
        self,
        store: IncidentStore,
        *,
        incident_id: str,
        identity: OperatorIdentity,
        decision: ApprovalDecision,
        reviewed_binding: ApprovalBinding,
    ) -> InvestigationRecord:
        self._require_responder(identity)
        now = self._clock()
        if reviewed_binding.expires_at <= now:
            raise ApprovalError("approval review context expired")
        record = store.get(incident_id)
        if record is None:
            raise ApprovalError("incident does not exist")
        if record.approvals:
            raise ApprovalError("proposal is already decided")
        if record.proposal is None:
            raise ApprovalError("incident has no Remediation Proposal to decide")
        state = self._kubernetes.inspect_deployment(record.proposal.action.target)
        if state is None:
            raise ApprovalError("current Deployment state is unavailable")
        try:
            current = build_approval_binding(
                record,
                state,
                expires_at=reviewed_binding.expires_at,
            )
        except ValueError as error:
            raise ApprovalError(str(error)) from error
        if current != reviewed_binding:
            raise ApprovalError("approval review context is stale")
        approval = Approval(
            approval_id=f"approval-{uuid4().hex}",
            incident_id=incident_id,
            proposal_id=record.proposal.proposal_id,
            responder_principal=identity.principal,
            decision=decision,
            decided_at=now,
            expires_at=reviewed_binding.expires_at,
            proposal_hash=current.proposal_hash,
            evidence_hash=current.evidence_hash,
            workload_resource_version=current.workload_resource_version,
            workload_generation=current.workload_generation,
            workload_revision=current.workload_revision,
            policy_hash=current.policy_hash,
            dry_run_hash=current.dry_run_hash,
            recovery_criteria_hash=current.recovery_criteria_hash,
            failure_strategy_hash=current.failure_strategy_hash,
        )
        event = AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=incident_id,
            event_type=f"proposal_{decision.value}",
            occurred_at=now,
            actor=identity.principal,
            details={
                "approval_id": approval.approval_id,
                "proposal_id": approval.proposal_id,
                "decision": decision.value,
                "subject": identity.subject,
                "proposal_hash": approval.proposal_hash,
            },
        )
        try:
            recorded = store.record_approval_decision(
                incident_id,
                reviewed_binding.incident_version,
                approval,
                event,
            )
        except ValueError as error:
            raise ApprovalError(str(error)) from error
        if decision is ApprovalDecision.APPROVED and self._publisher is not None:
            request = build_intervention_request(recorded, approval, requested_at=now)
            self._publisher.publish(request)
            recorded = store.append_audit_event(
                incident_id,
                AuditEvent(
                    event_id=f"audit-{uuid4().hex}",
                    incident_id=incident_id,
                    event_type="intervention_requested",
                    occurred_at=now,
                    actor="investigator",
                    details={
                        "request_id": request.request_id,
                        "approval_id": request.approval_id,
                        "idempotency_key": request.idempotency_key,
                        "payload_hash": request.payload_hash,
                    },
                ),
            )
        return recorded

    @staticmethod
    def _require_responder(identity: OperatorIdentity) -> None:
        if identity.role is not OperatorRole.RESPONDER:
            raise ApprovalError("Responder role is required")


__all__ = ["ApprovalError", "ApprovalService"]
