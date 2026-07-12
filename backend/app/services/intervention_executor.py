"""Deterministic, non-HTTP Intervention Executor with action-specific failure semantics."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.approval import approval_matches_record
from app.domain.incidents import (
    ActionConvergenceStatus,
    ApprovalDecision,
    AuditEvent,
    DeploymentConvergenceResult,
    DeploymentPatch,
    DeploymentPolicyState,
    DeploymentRevision,
    EnrollmentProvider,
    ExecutorKubernetesProvider,
    IncidentStore,
    Intervention,
    InterventionRequest,
    InterventionState,
    InvestigationRecord,
    PolicyKubernetesProvider,
    PolicyStatus,
    RemediationAction,
    RemediationProposal,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    ScaleDeploymentAction,
    WorkloadLease,
    WorkloadLeaseStore,
    WorkloadReference,
)
from app.services.intervention_queue import intervention_request_hash
from app.services.proposal_policy import (
    DeterministicProposalPolicy,
    FakePolicyKubernetesProvider,
)


class InterventionExecutionError(RuntimeError):
    """Raised when the Executor cannot prove that the approved mutation remains safe."""


class DeterministicInterventionExecutor:
    """Consumes one approved action without exposing an API or adaptive tool surface."""

    def __init__(
        self,
        *,
        kubernetes: ExecutorKubernetesProvider,
        enrollment: EnrollmentProvider,
        leases: WorkloadLeaseStore,
        owner: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._kubernetes = kubernetes
        self._enrollment = enrollment
        self._leases = leases
        self._owner = owner
        self._clock = clock or (lambda: datetime.now(UTC))

    def consume(
        self,
        store: IncidentStore,
        request: InterventionRequest,
    ) -> Intervention:
        """Revalidate, dry-run, mutate, verify, and record one idempotent request."""

        now = self._clock()
        record = store.get(request.incident_id)
        if record is None:
            raise InterventionExecutionError("Intervention Incident does not exist")
        duplicate = next(
            (
                item
                for item in record.interventions
                if item.idempotency_key == request.idempotency_key
            ),
            None,
        )
        if duplicate is not None:
            store.append_audit_event(
                request.incident_id,
                self._event(request, "intervention_duplicate_rejected", now),
            )
            return duplicate
        store.append_audit_event(
            request.incident_id,
            self._event(
                request,
                "intervention_received",
                now,
                payload_hash=request.payload_hash,
            ),
        )

        intervention = Intervention(
            intervention_id=f"intervention-{request.idempotency_key[:24]}",
            incident_id=request.incident_id,
            proposal_id=request.proposal_id,
            approval_id=request.approval_id,
            target=request.target,
            state=InterventionState.RUNNING,
            requested_at=request.requested_at,
            idempotency_key=request.idempotency_key,
        )
        lease = self._leases.acquire(
            request.target,
            intervention_id=intervention.intervention_id,
            owner=self._owner,
            now=now,
        )
        if lease is None:
            store.append_audit_event(
                request.incident_id,
                self._event(
                    request,
                    "intervention_refused",
                    now,
                    reason="another Intervention holds the Managed Workload",
                ),
            )
            raise InterventionExecutionError("another Intervention holds the Managed Workload")

        claimed = False
        try:
            record = store.get(request.incident_id)
            if record is None:
                raise InterventionExecutionError("Intervention Incident disappeared")
            approval = next(
                (item for item in record.approvals if item.approval_id == request.approval_id),
                None,
            )
            proposal = record.proposal
            policy = record.policy_decision
            if intervention_request_hash(request) != request.payload_hash:
                raise InterventionExecutionError("Intervention request payload hash is invalid")
            if approval is None or approval.decision is not ApprovalDecision.APPROVED:
                raise InterventionExecutionError("Intervention lacks current approved authority")
            if approval.expires_at <= now or not approval_matches_record(record, approval):
                raise InterventionExecutionError("Intervention Approval is stale")
            if proposal is None or policy is None or policy.status is not PolicyStatus.PASSED:
                raise InterventionExecutionError("Intervention policy authority is invalid")
            if (
                proposal.proposal_id != request.proposal_id
                or proposal.action.target != request.target
                or policy.patch != request.patch
            ):
                raise InterventionExecutionError(
                    "Intervention request differs from reviewed policy"
                )

            store.record_intervention(
                request.incident_id,
                record.incident.version,
                intervention,
                self._event(
                    request,
                    "intervention_claimed",
                    now,
                    action_type=proposal.action.action_type,
                ),
            )
            claimed = True

            state = self._kubernetes.inspect_deployment(request.target)
            if state is None:
                raise InterventionExecutionError("current Deployment state is unavailable")
            expected_state = (
                approval.workload_resource_version,
                approval.workload_generation,
                approval.workload_revision,
            )
            current_state = (
                state.resource_version,
                state.generation,
                state.current_revision,
            )
            if current_state != expected_state:
                raise InterventionExecutionError("Deployment changed after Approval")

            revalidated = DeterministicProposalPolicy(
                self._kubernetes, self._enrollment
            ).evaluate(record, proposal)
            if (
                revalidated.status is not PolicyStatus.PASSED
                or revalidated.patch != request.patch
            ):
                raise InterventionExecutionError(
                    "Executor policy revalidation rejected the request"
                )
            store.append_audit_event(
                request.incident_id,
                self._event(
                    request,
                    "intervention_validated",
                    self._clock(),
                    action_type=proposal.action.action_type,
                ),
            )
            store.append_audit_event(
                request.incident_id,
                self._event(
                    request,
                    "intervention_dry_run_passed",
                    now,
                    diff=revalidated.dry_run_diff or "",
                ),
            )
            lease = self._leases.renew(lease, now=self._clock())
            applied = self._kubernetes.apply_deployment_patch(request.patch)
            store.append_audit_event(
                request.incident_id,
                self._event(
                    request,
                    "intervention_mutated",
                    self._clock(),
                    resource_version=applied.resource_version,
                    generation=str(applied.generation),
                    action_type=proposal.action.action_type,
                ),
            )
            convergence = self._kubernetes.verify_deployment_convergence(
                request.patch,
                applied,
            )
            if convergence.status is ActionConvergenceStatus.AMBIGUOUS:
                raise InterventionExecutionError(
                    f"Deployment convergence is ambiguous: {convergence.reason}"
                )
            if convergence.status is ActionConvergenceStatus.FAILED:
                return self._handle_action_failure(
                    store,
                    intervention,
                    request,
                    record,
                    proposal,
                    state,
                    convergence,
                    lease,
                )
            if convergence.observed_state is None or not self._action_converged(
                proposal.action,
                state,
                convergence.observed_state,
            ):
                raise InterventionExecutionError(
                    "Deployment convergence is ambiguous: observed state does not match the "
                    "approved action"
                )
            completed = intervention.model_copy(update={"state": InterventionState.SUCCEEDED})
            store.update_intervention(
                request.incident_id,
                completed,
                self._event(
                    request,
                    "intervention_converged",
                    self._clock(),
                    action_type=proposal.action.action_type,
                    revision=str(convergence.observed_state.current_revision),
                    replicas=str(convergence.observed_state.replicas),
                ),
            )
            return completed
        except (ValueError, KeyError) as error:
            if claimed:
                self._record_safe_halt(store, intervention, request, str(error))
            else:
                self._record_refusal(store, request, str(error))
            raise InterventionExecutionError(str(error)) from error
        except InterventionExecutionError as error:
            if claimed:
                self._record_safe_halt(store, intervention, request, str(error))
            else:
                self._record_refusal(store, request, str(error))
            raise
        finally:
            try:
                self._leases.release(lease)
            except ValueError as error:
                store.append_audit_event(
                    request.incident_id,
                    self._event(
                        request,
                        "intervention_lease_release_skipped",
                        self._clock(),
                        reason=str(error),
                    ),
                )

    def _handle_action_failure(
        self,
        store: IncidentStore,
        intervention: Intervention,
        request: InterventionRequest,
        record: InvestigationRecord,
        proposal: RemediationProposal,
        previous_state: DeploymentPolicyState,
        convergence: DeploymentConvergenceResult,
        lease: WorkloadLease,
    ) -> Intervention:
        store.append_audit_event(
            request.incident_id,
            self._event(
                request,
                "intervention_action_failed",
                self._clock(),
                action_type=proposal.action.action_type,
                reason=convergence.reason,
            ),
        )
        if isinstance(proposal.action, ScaleDeploymentAction):
            return self._restore_failed_scale(
                store,
                intervention,
                request,
                record,
                proposal,
                previous_state,
                convergence,
                lease,
            )

        restoration = (
            "forbidden: the prior revision is implicated in this Incident"
            if isinstance(proposal.action, RollbackDeploymentAction)
            else "unavailable: controlled restart has no inverse mutation"
        )
        failed = intervention.model_copy(update={"state": InterventionState.FAILED})
        store.update_intervention(
            request.incident_id,
            failed,
            self._event(
                request,
                "intervention_escalated",
                self._clock(),
                action_type=proposal.action.action_type,
                reason=convergence.reason,
                restoration=restoration,
            ),
        )
        return failed

    def _restore_failed_scale(
        self,
        store: IncidentStore,
        intervention: Intervention,
        request: InterventionRequest,
        record: InvestigationRecord,
        proposal: RemediationProposal,
        previous_state: DeploymentPolicyState,
        convergence: DeploymentConvergenceResult,
        lease: WorkloadLease,
    ) -> Intervention:
        observed = convergence.observed_state
        current = self._kubernetes.inspect_deployment(request.target)
        if observed is None or current != observed:
            raise InterventionExecutionError(
                "scale restoration stopped because current Deployment state is ambiguous"
            )
        restoration_proposal = proposal.model_copy(
            update={
                "proposal_id": f"{proposal.proposal_id}-restore-{intervention.intervention_id}",
                "action": ScaleDeploymentAction(
                    target=proposal.action.target,
                    replicas=previous_state.replicas,
                ),
            }
        )
        decision = DeterministicProposalPolicy(
            self._kubernetes,
            self._enrollment,
        ).evaluate(record, restoration_proposal)
        if decision.status is not PolicyStatus.PASSED or decision.patch is None:
            raise InterventionExecutionError("scale restoration policy rejected the inverse patch")
        store.append_audit_event(
            request.incident_id,
            self._event(
                request,
                "intervention_restoration_validated",
                self._clock(),
                replicas=str(previous_state.replicas),
                dry_run_diff=decision.dry_run_diff or "",
            ),
        )
        self._leases.renew(lease, now=self._clock())
        restored = self._kubernetes.apply_deployment_patch(decision.patch)
        store.append_audit_event(
            request.incident_id,
            self._event(
                request,
                "intervention_restoration_mutated",
                self._clock(),
                replicas=str(previous_state.replicas),
                resource_version=restored.resource_version,
            ),
        )
        restoration_convergence = self._kubernetes.verify_deployment_convergence(
            decision.patch,
            restored,
        )
        restoration_action = restoration_proposal.action
        if (
            restoration_convergence.status is not ActionConvergenceStatus.SUCCEEDED
            or restoration_convergence.observed_state is None
            or not self._action_converged(
                restoration_action,
                current,
                restoration_convergence.observed_state,
            )
        ):
            raise InterventionExecutionError(
                "scale restoration convergence is ambiguous; no further writes are allowed"
            )
        rolled_back = intervention.model_copy(
            update={"state": InterventionState.ROLLED_BACK}
        )
        store.update_intervention(
            request.incident_id,
            rolled_back,
            self._event(
                request,
                "intervention_restored",
                self._clock(),
                replicas=str(previous_state.replicas),
                reason=convergence.reason,
            ),
        )
        return rolled_back

    @staticmethod
    def _action_converged(
        action: RemediationAction,
        previous: DeploymentPolicyState,
        observed: DeploymentPolicyState,
    ) -> bool:
        if (
            observed.target != action.target
            or observed.resource_version == previous.resource_version
            or observed.generation <= previous.generation
        ):
            return False
        if isinstance(action, RollbackDeploymentAction):
            return observed.current_revision == action.revision
        if isinstance(action, ScaleDeploymentAction):
            return (
                observed.replicas == action.replicas
                and observed.current_revision == previous.current_revision
            )
        if isinstance(action, RestartDeploymentAction):
            return observed.current_revision > previous.current_revision
        return False

    def _record_safe_halt(
        self,
        store: IncidentStore,
        intervention: Intervention,
        request: InterventionRequest,
        reason: str,
    ) -> None:
        current = store.get(request.incident_id)
        if current is None:
            return
        stored = next(
            (
                item
                for item in current.interventions
                if item.intervention_id == intervention.intervention_id
            ),
            None,
        )
        if stored is None or stored.state is not InterventionState.RUNNING:
            return
        safe_halted = intervention.model_copy(update={"state": InterventionState.SAFE_HALTED})
        store.update_intervention(
            request.incident_id,
            safe_halted,
            self._event(
                request,
                "intervention_safe_halted",
                self._clock(),
                reason=reason,
            ),
        )

    def _record_refusal(
        self,
        store: IncidentStore,
        request: InterventionRequest,
        reason: str,
    ) -> None:
        store.append_audit_event(
            request.incident_id,
            self._event(
                request,
                "intervention_refused",
                self._clock(),
                reason=reason,
            ),
        )

    @staticmethod
    def _event(
        request: InterventionRequest,
        event_type: str,
        occurred_at: datetime,
        **details: str,
    ) -> AuditEvent:
        return AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=request.incident_id,
            event_type=event_type,
            occurred_at=occurred_at,
            actor="deterministic-executor",
            details={
                "request_id": request.request_id,
                "idempotency_key": request.idempotency_key,
                **details,
            },
        )


class FakeExecutorKubernetesProvider(FakePolicyKubernetesProvider):
    """Mutable fake enforcing optimistic concurrency and exact Deployment patches."""

    def __init__(
        self,
        states: tuple[DeploymentPolicyState, ...],
        *,
        dry_run_error: str | None = None,
    ) -> None:
        super().__init__(states, dry_run_error=dry_run_error)
        self._applied_patches: list[DeploymentPatch] = []
        self._external_resource_version: str | None = None
        self._convergence_results: list[ActionConvergenceStatus] = []
        self._quota_headroom_after_next_apply: int | None = None

    @classmethod
    def ready(
        cls,
        *,
        active_intervention: bool = False,
        available_revisions: tuple[int, ...] = (7, 8),
        implicated_revisions: tuple[int, ...] = (8,),
        replica_quota_headroom: int = 3,
        dry_run_error: str | None = None,
    ) -> "FakeExecutorKubernetesProvider":
        policy = FakePolicyKubernetesProvider.ready(
            active_intervention=active_intervention,
            available_revisions=available_revisions,
            implicated_revisions=implicated_revisions,
            replica_quota_headroom=replica_quota_headroom,
            dry_run_error=dry_run_error,
        )
        return cls.from_policy_provider(policy)

    @classmethod
    def from_policy_provider(
        cls, provider: PolicyKubernetesProvider
    ) -> "FakeExecutorKubernetesProvider":
        target = WorkloadReference(
            namespace="online-boutique", name="recommendationservice"
        )
        state = provider.inspect_deployment(target)
        if state is None:
            raise ValueError("policy provider has no recommendationservice state")
        return cls((state,))

    @property
    def applied_patches(self) -> tuple[DeploymentPatch, ...]:
        return tuple(self._applied_patches)

    def mutate_before_next_apply(self, *, resource_version: str) -> None:
        """Schedule a concurrent writer after Executor validation and before mutation."""

        self._external_resource_version = resource_version

    def set_convergence_results(self, *statuses: ActionConvergenceStatus) -> None:
        """Set deterministic post-apply outcomes for action and restoration tests."""

        self._convergence_results = list(statuses)

    def set_quota_headroom_after_next_apply(self, headroom: int) -> None:
        """Change current quota facts after one write to exercise restoration policy."""

        self._quota_headroom_after_next_apply = headroom

    def reject_future_dry_runs(self, reason: str) -> None:
        """Make subsequent Executor-side policy dry-runs fail closed."""

        self._dry_run_error = reason  # noqa: SLF001 - explicit mutable provider fake control

    def apply_deployment_patch(self, patch: DeploymentPatch) -> DeploymentPolicyState:
        state = self.inspect_deployment(patch.target)
        if state is None:
            raise ValueError("Deployment disappeared before mutation")
        if self._external_resource_version is not None:
            state = state.model_copy(
                update={"resource_version": self._external_resource_version}
            )
            self._states[patch.target] = state  # noqa: SLF001 - mutable Kubernetes fake
            self._external_resource_version = None
        if patch.resource_version != state.resource_version:
            raise ValueError("optimistic concurrency resourceVersion mismatch")
        dry_run = self.dry_run_deployment_patch(patch)
        if not dry_run.accepted:
            raise ValueError(dry_run.error or "server-side dry-run rejected the patch")
        spec = patch.body.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("Executor received a malformed Deployment patch")
        updates: dict[str, object] = {
            "resource_version": f"{state.resource_version}-{patch.action_type}",
            "generation": state.generation + 1,
        }
        if patch.action_type == "rollback_deployment":
            template = spec.get("template")
            metadata = template.get("metadata") if isinstance(template, dict) else None
            labels = metadata.get("labels") if isinstance(metadata, dict) else None
            revision = labels.get("revision") if isinstance(labels, dict) else None
            if not isinstance(revision, str):
                raise ValueError("rollback patch has no exact revision template")
            updates["current_revision"] = int(revision)
        elif patch.action_type == "scale_deployment":
            replicas = spec.get("replicas")
            if not isinstance(replicas, int):
                raise ValueError("scale patch has no exact replica count")
            updates["replicas"] = replicas
        elif patch.action_type == "restart_deployment":
            template = spec.get("template")
            metadata = template.get("metadata") if isinstance(template, dict) else None
            annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
            token = (
                annotations.get("kubecouncil.io/restart-token")
                if isinstance(annotations, dict)
                else None
            )
            if not isinstance(token, str):
                raise ValueError("restart patch has no exact restart token")
            next_revision = state.current_revision + 1
            updates["current_revision"] = next_revision
            updates["available_revisions"] = (
                *state.available_revisions,
                DeploymentRevision(
                    revision=next_revision,
                    pod_template={"metadata": {"annotations": {"restart-token": token}}},
                ),
            )
        if self._quota_headroom_after_next_apply is not None:
            updates["replica_quota_headroom"] = self._quota_headroom_after_next_apply
            self._quota_headroom_after_next_apply = None
        updated = state.model_copy(update=updates)
        self._states[patch.target] = updated  # noqa: SLF001 - mutable Kubernetes fake
        self._applied_patches.append(patch)
        return updated

    def verify_deployment_convergence(
        self,
        patch: DeploymentPatch,
        applied_state: DeploymentPolicyState,
    ) -> DeploymentConvergenceResult:
        current = self.inspect_deployment(patch.target)
        status = (
            self._convergence_results.pop(0)
            if self._convergence_results
            else ActionConvergenceStatus.SUCCEEDED
        )
        reasons = {
            ActionConvergenceStatus.SUCCEEDED: "Deployment reached the action-specific state.",
            ActionConvergenceStatus.FAILED: "Deployment reported a definitive action failure.",
            ActionConvergenceStatus.AMBIGUOUS: (
                "Deployment convergence could not be established deterministically."
            ),
        }
        return DeploymentConvergenceResult(
            status=status,
            reason=reasons[status],
            observed_state=current,
        )


__all__ = [
    "DeterministicInterventionExecutor",
    "FakeExecutorKubernetesProvider",
    "InterventionExecutionError",
]
