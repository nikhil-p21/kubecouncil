"""Deterministic Remediation Proposal policy and server-side dry-run workflow."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from app.domain.incidents import (
    AuditEvent,
    DeploymentPatch,
    DeploymentPolicyState,
    DeploymentRevision,
    DryRunResult,
    EnrollmentProvider,
    EvidenceObservation,
    IncidentStore,
    InvestigationRecord,
    PolicyCheck,
    PolicyCheckCode,
    PolicyDecision,
    PolicyKubernetesProvider,
    PolicyStatus,
    RemediationProposal,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    ScaleDeploymentAction,
    WorkloadReference,
)
from app.services.enrollment import EnrollmentChecker


class ProposalPolicyError(RuntimeError):
    """Raised when there is no structurally valid proposal to evaluate."""


def evidence_hash(evidence: tuple[EvidenceObservation, ...]) -> str:
    """Return the stable evidence binding used by Council and policy."""

    material = "\n".join(sorted(item.content_hash for item in evidence))
    return sha256(material.encode()).hexdigest()


class DeterministicProposalPolicy:
    """Owns proposal authority, current-state validation, patch shape, and dry-run."""

    def __init__(
        self,
        kubernetes: PolicyKubernetesProvider,
        enrollment: EnrollmentProvider,
    ) -> None:
        self._kubernetes = kubernetes
        self._enrollment = enrollment

    def evaluate(
        self, record: InvestigationRecord, proposal: RemediationProposal
    ) -> PolicyDecision:
        checks: list[PolicyCheck] = []

        self._check(
            checks,
            PolicyCheckCode.PROPOSAL_SCOPED,
            proposal.incident_id == record.incident.incident_id,
            "Proposal belongs to this Incident.",
            "Proposal belongs to another Incident.",
        )
        self._check(
            checks,
            PolicyCheckCode.EVIDENCE_CURRENT,
            proposal.evidence_hash == evidence_hash(record.evidence),
            "Proposal is bound to the current recorded evidence.",
            "Proposal evidence binding is stale.",
        )

        readiness = EnrollmentChecker().check(
            record.application_profile,
            self._enrollment.inspect(record.application_profile),
        )
        self._check(
            checks,
            PolicyCheckCode.ENROLLMENT_READY,
            readiness.ready,
            "Application Enrollment prerequisites are current.",
            "Application Enrollment prerequisites are not ready.",
        )

        action = proposal.action
        workload = next(
            (
                candidate
                for candidate in record.application_profile.workloads
                if candidate.reference == action.target
            ),
            None,
        )
        self._check(
            checks,
            PolicyCheckCode.TARGET_ENROLLED,
            workload is not None,
            "Target is declared by the enrolled Application Profile.",
            "Target is outside the enrolled Application Profile.",
        )
        self._check(
            checks,
            PolicyCheckCode.TARGET_EXECUTABLE,
            workload is not None and workload.executable and not workload.protected_dependency,
            "Target is an executable Managed Workload.",
            "Target is protected or otherwise non-executable.",
        )
        self._check(
            checks,
            PolicyCheckCode.ACTION_ALLOWED,
            workload is not None and action.action_type in workload.allowed_actions,
            "Action is explicitly allowlisted for the target.",
            "Action is not allowlisted for the target; present it only as Manual Guidance.",
        )

        state = self._kubernetes.inspect_deployment(action.target)
        self._check(
            checks,
            PolicyCheckCode.LIVE_STATE_AVAILABLE,
            state is not None,
            "Current Deployment state is available.",
            "Current Deployment state is unavailable or the target is unmanaged.",
        )
        self._check(
            checks,
            PolicyCheckCode.NO_ACTIVE_INTERVENTION,
            state is not None and not state.active_intervention,
            "No active Intervention holds the target.",
            "Another Intervention currently holds the target.",
        )

        if isinstance(action, RollbackDeploymentAction):
            revision = self._revision(state, action.revision)
            self._check(
                checks,
                PolicyCheckCode.REVISION_AVAILABLE,
                revision is not None,
                "Requested prior revision exists in current Deployment history.",
                "Requested revision is missing from current Deployment history.",
            )
            self._check(
                checks,
                PolicyCheckCode.RESTORATION_SAFE,
                revision is not None and revision.restorable and not revision.implicated,
                "Requested revision has complete, non-implicated restoration evidence.",
                "Requested revision is implicated or lacks safe restoration evidence.",
            )
        elif isinstance(action, ScaleDeploymentAction):
            within_bounds = (
                workload is not None
                and workload.replica_bounds.minimum
                <= action.replicas
                <= workload.replica_bounds.maximum
            )
            self._check(
                checks,
                PolicyCheckCode.REPLICA_BOUNDS,
                within_bounds,
                "Requested replicas are within declared bounds.",
                "Requested replicas exceed declared workload bounds.",
            )
            required_headroom = max(0, action.replicas - state.replicas) if state else 0
            self._check(
                checks,
                PolicyCheckCode.REPLICA_QUOTA,
                state is not None and required_headroom <= state.replica_quota_headroom,
                "Requested scale remains within current quota headroom.",
                "Requested scale exceeds current quota headroom.",
            )

        preflight_failure = next((check.message for check in checks if not check.passed), None)
        if preflight_failure is not None or state is None:
            return self._decision(
                record,
                proposal,
                checks,
                status=PolicyStatus.REJECTED,
                state=state,
                rejection_reason=preflight_failure or "Policy preflight failed.",
            )

        patch = self._build_patch(action, state)
        patch_valid = self._patch_shape_is_exact(patch)
        self._check(
            checks,
            PolicyCheckCode.PATCH_SHAPE,
            patch_valid,
            "Action produced the exact allowlisted minimal patch shape.",
            "Action produced fields outside its allowlisted patch shape.",
        )
        if not patch_valid:
            return self._decision(
                record,
                proposal,
                checks,
                status=PolicyStatus.REJECTED,
                state=state,
                patch=patch,
                rejection_reason="Action patch shape is not allowlisted.",
            )

        dry_run = self._kubernetes.dry_run_deployment_patch(patch)
        self._check(
            checks,
            PolicyCheckCode.SERVER_DRY_RUN,
            dry_run.accepted,
            "Kubernetes server-side dry-run accepted the exact patch.",
            dry_run.error or "Kubernetes server-side dry-run rejected the patch.",
        )
        if not dry_run.accepted:
            return self._decision(
                record,
                proposal,
                checks,
                status=PolicyStatus.DRY_RUN_FAILED,
                state=state,
                patch=patch,
                rejection_reason=dry_run.error or "Kubernetes server-side dry-run failed.",
            )
        return self._decision(
            record,
            proposal,
            checks,
            status=PolicyStatus.PASSED,
            state=state,
            patch=patch,
            dry_run_diff=dry_run.diff,
        )

    def evaluate_and_record(
        self, store: IncidentStore, incident_id: str
    ) -> InvestigationRecord:
        record = store.get(incident_id)
        if record is None:
            raise ProposalPolicyError("incident does not exist")
        if record.proposal is None:
            return record
        decision = self.evaluate(record, record.proposal)
        store.record_policy_decision(incident_id, decision)
        event_type = (
            "proposal_policy_passed"
            if decision.status is PolicyStatus.PASSED
            else "proposal_policy_rejected"
        )
        return store.append_audit_event(
            incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=incident_id,
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                actor="deterministic-policy-engine",
                details={
                    "proposal_id": decision.proposal_id,
                    "status": decision.status.value,
                    "failed_checks": ",".join(
                        check.code.value for check in decision.checks if not check.passed
                    ),
                },
            ),
        )

    @staticmethod
    def _check(
        checks: list[PolicyCheck],
        code: PolicyCheckCode,
        passed: bool,
        passed_message: str,
        failed_message: str,
    ) -> None:
        checks.append(
            PolicyCheck(
                code=code,
                passed=passed,
                message=passed_message if passed else failed_message,
            )
        )

    @staticmethod
    def _revision(
        state: DeploymentPolicyState | None, revision: int
    ) -> DeploymentRevision | None:
        if state is None:
            return None
        return next(
            (
                candidate
                for candidate in state.available_revisions
                if candidate.revision == revision
            ),
            None,
        )

    @classmethod
    def _build_patch(
        cls,
        action: RollbackDeploymentAction | ScaleDeploymentAction | RestartDeploymentAction,
        state: DeploymentPolicyState,
    ) -> DeploymentPatch:
        metadata: dict[str, object] = {
            "name": action.target.name,
            "namespace": action.target.namespace,
            "resourceVersion": state.resource_version,
        }
        if isinstance(action, RollbackDeploymentAction):
            revision = cls._revision(state, action.revision)
            if revision is None:
                raise ProposalPolicyError("validated rollback revision disappeared")
            spec: dict[str, object] = {"template": revision.pod_template}
        elif isinstance(action, ScaleDeploymentAction):
            spec = {"replicas": action.replicas}
        else:
            spec = {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubecouncil.io/restart-token": action.restart_token,
                        }
                    }
                }
            }
        return DeploymentPatch(
            action_type=action.action_type,
            target=action.target,
            resource_version=state.resource_version,
            body={
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": metadata,
                "spec": spec,
            },
        )

    @staticmethod
    def _patch_shape_is_exact(patch: DeploymentPatch) -> bool:
        body = patch.body
        if set(body) != {"apiVersion", "kind", "metadata", "spec"}:
            return False
        metadata = body.get("metadata")
        spec = body.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            return False
        if set(metadata) != {"name", "namespace", "resourceVersion"}:
            return False
        if patch.action_type == "scale_deployment":
            return set(spec) == {"replicas"} and isinstance(spec.get("replicas"), int)
        if set(spec) != {"template"} or not isinstance(spec.get("template"), dict):
            return False
        if patch.action_type == "rollback_deployment":
            return bool(spec["template"])
        template = spec["template"]
        if not isinstance(template, dict) or set(template) != {"metadata"}:
            return False
        template_metadata = template.get("metadata")
        if not isinstance(template_metadata, dict) or set(template_metadata) != {"annotations"}:
            return False
        annotations = template_metadata.get("annotations")
        return (
            isinstance(annotations, dict)
            and set(annotations) == {"kubecouncil.io/restart-token"}
            and isinstance(annotations.get("kubecouncil.io/restart-token"), str)
        )

    @staticmethod
    def _decision(
        record: InvestigationRecord,
        proposal: RemediationProposal,
        checks: list[PolicyCheck],
        *,
        status: PolicyStatus,
        state: DeploymentPolicyState | None,
        patch: DeploymentPatch | None = None,
        dry_run_diff: str | None = None,
        rejection_reason: str | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            incident_id=record.incident.incident_id,
            proposal_id=proposal.proposal_id,
            status=status,
            checks=tuple(checks),
            evaluated_at=datetime.now(UTC),
            workload_resource_version=state.resource_version if state else None,
            patch=patch,
            dry_run_diff=dry_run_diff,
            rejection_reason=rejection_reason,
        )


class FakePolicyKubernetesProvider:
    """Current-state and dry-run fake for the local vertical slice."""

    def __init__(
        self,
        states: tuple[DeploymentPolicyState, ...],
        *,
        dry_run_error: str | None = None,
    ) -> None:
        self._states = {state.target: state for state in states}
        self._dry_run_error = dry_run_error
        self._dry_run_patches: list[DeploymentPatch] = []

    @classmethod
    def ready(
        cls,
        *,
        active_intervention: bool = False,
        available_revisions: tuple[int, ...] = (7, 8),
        implicated_revisions: tuple[int, ...] = (8,),
        replica_quota_headroom: int = 3,
        dry_run_error: str | None = None,
    ) -> "FakePolicyKubernetesProvider":
        target = WorkloadReference(
            namespace="online-boutique", name="recommendationservice"
        )
        revisions = tuple(
            DeploymentRevision(
                revision=revision,
                pod_template={"metadata": {"labels": {"revision": str(revision)}}},
                restorable=True,
                implicated=revision in implicated_revisions,
            )
            for revision in available_revisions
        )
        return cls(
            (
                DeploymentPolicyState(
                    target=target,
                    resource_version="rv-8",
                    generation=8,
                    replicas=3,
                    current_revision=8,
                    available_revisions=revisions,
                    active_intervention=active_intervention,
                    replica_quota_headroom=replica_quota_headroom,
                ),
            ),
            dry_run_error=dry_run_error,
        )

    @property
    def dry_run_patches(self) -> tuple[DeploymentPatch, ...]:
        return tuple(self._dry_run_patches)

    def inspect_deployment(self, target: WorkloadReference) -> DeploymentPolicyState | None:
        return self._states.get(target)

    def dry_run_deployment_patch(self, patch: DeploymentPatch) -> DryRunResult:
        self._dry_run_patches.append(patch)
        if self._dry_run_error is not None:
            return DryRunResult(accepted=False, error=self._dry_run_error)
        state = self._states[patch.target]
        spec = patch.body.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("fake dry-run received a malformed Deployment spec")
        if patch.action_type == "rollback_deployment":
            template = spec.get("template")
            metadata = template.get("metadata") if isinstance(template, dict) else None
            labels = metadata.get("labels") if isinstance(metadata, dict) else None
            revision = labels.get("revision") if isinstance(labels, dict) else None
            diff = (
                f"Deployment/{patch.target.name}: revision "
                f"{state.current_revision} -> {revision}"
            )
        elif patch.action_type == "scale_deployment":
            replicas = spec.get("replicas")
            diff = f"Deployment/{patch.target.name}: replicas {state.replicas} -> {replicas}"
        else:
            diff = f"Deployment/{patch.target.name}: controlled restart token added"
        return DryRunResult(accepted=True, diff=diff)


__all__ = [
    "DeterministicProposalPolicy",
    "FakePolicyKubernetesProvider",
    "PolicyCheckCode",
    "PolicyStatus",
    "ProposalPolicyError",
    "evidence_hash",
]
