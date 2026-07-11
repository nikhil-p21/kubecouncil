"""Deterministic hashing and freshness rules for human Approval."""

import json
from datetime import datetime
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

from app.domain.incidents import (
    Approval,
    ApprovalBinding,
    DeploymentPolicyState,
    InvestigationRecord,
    PolicyStatus,
)


def canonical_hash(value: Any) -> str:
    """Hash provider-independent JSON with stable key and collection ordering."""

    material: object
    if isinstance(value, BaseModel):
        material = value.model_dump(mode="json")
    elif isinstance(value, tuple):
        material = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ]
    else:
        material = value
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).hexdigest()


def build_approval_binding(
    record: InvestigationRecord,
    state: DeploymentPolicyState,
    *,
    expires_at: datetime,
) -> ApprovalBinding:
    proposal = record.proposal
    policy = record.policy_decision
    if proposal is None or policy is None or policy.status is not PolicyStatus.PASSED:
        raise ValueError("Approval requires one policy-passed Remediation Proposal")
    if policy.dry_run_diff is None:
        raise ValueError("Approval requires a successful server-side dry-run")
    expected_state = (
        policy.workload_resource_version,
        policy.workload_generation,
        policy.workload_revision,
    )
    current_state = (state.resource_version, state.generation, state.current_revision)
    if expected_state != current_state:
        raise ValueError("live workload state changed after policy review")
    return ApprovalBinding(
        incident_version=record.incident.version,
        proposal_hash=canonical_hash(proposal),
        evidence_hash=canonical_hash((record.evidence_window, *record.evidence)),
        workload_resource_version=state.resource_version,
        workload_generation=state.generation,
        workload_revision=state.current_revision,
        policy_hash=canonical_hash(policy),
        dry_run_hash=canonical_hash(policy.dry_run_diff),
        recovery_criteria_hash=canonical_hash(proposal.recovery_criteria),
        failure_strategy_hash=canonical_hash(proposal.rollback_strategy),
        expires_at=expires_at,
    )


def approval_matches_record(record: InvestigationRecord, approval: Approval) -> bool:
    proposal = record.proposal
    policy = record.policy_decision
    if proposal is None or policy is None or policy.status is not PolicyStatus.PASSED:
        return False
    if policy.dry_run_diff is None:
        return False
    return (
        approval.proposal_id == proposal.proposal_id
        and approval.proposal_hash == canonical_hash(proposal)
        and approval.evidence_hash
        == canonical_hash((record.evidence_window, *record.evidence))
        and approval.workload_resource_version == policy.workload_resource_version
        and approval.workload_generation == policy.workload_generation
        and approval.workload_revision == policy.workload_revision
        and approval.policy_hash == canonical_hash(policy)
        and approval.dry_run_hash == canonical_hash(policy.dry_run_diff)
        and approval.recovery_criteria_hash == canonical_hash(proposal.recovery_criteria)
        and approval.failure_strategy_hash == canonical_hash(proposal.rollback_strategy)
    )


__all__ = ["approval_matches_record", "build_approval_binding", "canonical_hash"]
