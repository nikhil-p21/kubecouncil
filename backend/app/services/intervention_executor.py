"""Deterministic, non-HTTP rollback Executor with no model or evidence-query access."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.approval import approval_matches_record
from app.domain.incidents import (
    ApprovalDecision,
    AuditEvent,
    DeploymentPatch,
    DeploymentPolicyState,
    EnrollmentProvider,
    ExecutorKubernetesProvider,
    IncidentStore,
    Intervention,
    InterventionRequest,
    InterventionState,
    PolicyKubernetesProvider,
    PolicyStatus,
    RollbackDeploymentAction,
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


class RollbackInterventionExecutor:
    """Consumes one approved rollback without exposing an API or adaptive tool surface."""

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
        """Revalidate, dry-run, mutate, and record one idempotent rollback request."""

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
            if not isinstance(proposal.action, RollbackDeploymentAction):
                raise InterventionExecutionError("this Executor accepts rollback actions only")
            if (
                proposal.proposal_id != request.proposal_id
                or proposal.action.target != request.target
                or policy.patch != request.patch
            ):
                raise InterventionExecutionError(
                    "Intervention request differs from reviewed policy"
                )

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

            store.record_intervention(
                request.incident_id,
                record.incident.version,
                intervention,
                self._event(request, "intervention_validated", now),
            )
            claimed = True
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
                ),
            )
            if (
                applied.target != request.target
                or applied.current_revision != proposal.action.revision
                or applied.generation <= state.generation
            ):
                raise InterventionExecutionError(
                    "rollback did not converge to the approved revision"
                )
            completed = intervention.model_copy(update={"state": InterventionState.SUCCEEDED})
            store.update_intervention(
                request.incident_id,
                completed,
                self._event(
                    request,
                    "intervention_converged",
                    self._clock(),
                    revision=str(applied.current_revision),
                ),
            )
            return completed
        except (ValueError, KeyError) as error:
            if claimed:
                self._record_failure(store, intervention, request, str(error))
            raise InterventionExecutionError(str(error)) from error
        except InterventionExecutionError:
            if claimed:
                self._record_failure(store, intervention, request, "safety revalidation failed")
            raise
        finally:
            self._leases.release(lease)

    def _record_failure(
        self,
        store: IncidentStore,
        intervention: Intervention,
        request: InterventionRequest,
        reason: str,
    ) -> None:
        failed = intervention.model_copy(update={"state": InterventionState.FAILED})
        store.update_intervention(
            request.incident_id,
            failed,
            self._event(
                request,
                "intervention_failed",
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
        template = spec.get("template") if isinstance(spec, dict) else None
        metadata = template.get("metadata") if isinstance(template, dict) else None
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        revision = labels.get("revision") if isinstance(labels, dict) else None
        if patch.action_type != "rollback_deployment" or not isinstance(revision, str):
            raise ValueError("Executor received a non-rollback Deployment patch")
        updated = state.model_copy(
            update={
                "resource_version": f"{state.resource_version}-rollback",
                "generation": state.generation + 1,
                "current_revision": int(revision),
            }
        )
        self._states[patch.target] = updated  # noqa: SLF001 - mutable Kubernetes fake
        self._applied_patches.append(patch)
        return updated


__all__ = [
    "FakeExecutorKubernetesProvider",
    "InterventionExecutionError",
    "RollbackInterventionExecutor",
]
