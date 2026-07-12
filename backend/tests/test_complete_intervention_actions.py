from datetime import UTC, datetime

import pytest

from app.domain.identity import OperatorIdentity, OperatorRole
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import (
    ActionConvergenceStatus,
    AlertSignal,
    ApprovalDecision,
    CoordinatorOutput,
    EvidenceCitation,
    IncidentStore,
    InterventionOutcome,
    InterventionRequest,
    InterventionState,
    InvestigationOutcome,
    RemediationAction,
    RemediationProposal,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    RootCauseHypothesis,
    ScaleDeploymentAction,
    WorkloadLease,
    WorkloadReference,
)
from app.services.approval import ApprovalService
from app.services.evidence import DeterministicEvidenceRedactor, InitialEvidenceWindowCollector
from app.services.incident_store import FirestoreIncidentStore, InMemoryDocumentDatabase
from app.services.intervention_executor import (
    DeterministicInterventionExecutor,
    FakeExecutorKubernetesProvider,
    InterventionExecutionError,
)
from app.services.intervention_queue import InMemoryInterventionQueue
from app.services.proposal_policy import DeterministicProposalPolicy, evidence_hash
from app.services.workload_lease import FirestoreWorkloadLeaseStore, InMemoryLeaseDatabase

TARGET = WorkloadReference(namespace="online-boutique", name="recommendationservice")


def test_bounded_scale_executes_through_the_approved_intervention_path() -> None:
    action = ScaleDeploymentAction(target=_target(), replicas=4)
    store, request, kubernetes, enrollment = _approved_request(action)
    executor = _executor(kubernetes, enrollment)

    intervention = executor.consume(store, request)

    assert intervention.state is InterventionState.SUCCEEDED
    assert [patch.action_type for patch in kubernetes.applied_patches] == ["scale_deployment"]
    assert kubernetes.inspect_deployment(_target()).replicas == 4  # type: ignore[union-attr]
    record = store.get(request.incident_id)
    assert record is not None
    assert record.incident.intervention_outcome is InterventionOutcome.MONITORING


def test_controlled_restart_executes_without_an_inverse_patch() -> None:
    action = RestartDeploymentAction(target=_target(), restart_token="incident-restart-1")
    store, request, kubernetes, enrollment = _approved_request(action)
    executor = _executor(kubernetes, enrollment)

    intervention = executor.consume(store, request)

    assert intervention.state is InterventionState.SUCCEEDED
    assert [patch.action_type for patch in kubernetes.applied_patches] == [
        "restart_deployment"
    ]
    record = store.get(request.incident_id)
    assert record is not None
    assert record.audit_events[-1].event_type == "intervention_converged"


@pytest.mark.parametrize(
    "store",
    (
        InMemoryIncidentStore(),
        FirestoreIncidentStore(InMemoryDocumentDatabase()),
    ),
)
def test_failed_scale_restores_previous_replicas_only_after_policy_revalidation(
    store: IncidentStore,
) -> None:
    action = ScaleDeploymentAction(target=_target(), replicas=4)
    store, request, kubernetes, enrollment = _approved_request(action, store=store)
    kubernetes.set_convergence_results(
        ActionConvergenceStatus.FAILED,
        ActionConvergenceStatus.SUCCEEDED,
    )

    intervention = _executor(kubernetes, enrollment).consume(store, request)

    assert intervention.state is InterventionState.ROLLED_BACK
    assert [patch.action_type for patch in kubernetes.applied_patches] == [
        "scale_deployment",
        "scale_deployment",
    ]
    assert [patch.body["spec"] for patch in kubernetes.applied_patches] == [
        {"replicas": 4},
        {"replicas": 3},
    ]
    record = store.get(request.incident_id)
    assert record is not None
    assert record.incident.intervention_outcome is InterventionOutcome.ROLLED_BACK
    event_types = [event.event_type for event in record.audit_events]
    assert "intervention_action_failed" in event_types
    assert "intervention_restoration_validated" in event_types
    assert "intervention_restored" in event_types


def test_unsafe_scale_restoration_enters_safe_halt_without_a_second_write() -> None:
    action = ScaleDeploymentAction(target=_target(), replicas=2)
    store, request, kubernetes, enrollment = _approved_request(action)
    kubernetes.set_convergence_results(ActionConvergenceStatus.FAILED)
    kubernetes.set_quota_headroom_after_next_apply(0)

    with pytest.raises(InterventionExecutionError, match="restoration policy"):
        _executor(kubernetes, enrollment).consume(store, request)

    assert len(kubernetes.applied_patches) == 1
    record = store.get(request.incident_id)
    assert record is not None
    assert record.incident.intervention_outcome is InterventionOutcome.SAFE_HALTED
    assert record.interventions[0].state is InterventionState.SAFE_HALTED
    assert record.audit_events[-1].event_type == "intervention_safe_halted"


@pytest.mark.parametrize(
    "action",
    (
        RollbackDeploymentAction(target=TARGET, revision=7),
        RestartDeploymentAction(target=TARGET, restart_token="incident-restart-failed"),
    ),
)
def test_non_invertible_action_failure_escalates_without_an_invented_patch(
    action: RollbackDeploymentAction | RestartDeploymentAction,
) -> None:
    store, request, kubernetes, enrollment = _approved_request(action)
    kubernetes.set_convergence_results(ActionConvergenceStatus.FAILED)

    intervention = _executor(kubernetes, enrollment).consume(store, request)

    assert intervention.state is InterventionState.FAILED
    assert len(kubernetes.applied_patches) == 1
    record = store.get(request.incident_id)
    assert record is not None
    assert record.incident.intervention_outcome is InterventionOutcome.FAILED
    assert record.audit_events[-1].event_type == "intervention_escalated"
    assert all("restor" not in event.event_type for event in record.audit_events)


def test_ambiguous_convergence_enters_safe_halt_without_restoration() -> None:
    action = ScaleDeploymentAction(target=_target(), replicas=4)
    store, request, kubernetes, enrollment = _approved_request(action)
    kubernetes.set_convergence_results(ActionConvergenceStatus.AMBIGUOUS)

    with pytest.raises(InterventionExecutionError, match="ambiguous"):
        _executor(kubernetes, enrollment).consume(store, request)

    assert len(kubernetes.applied_patches) == 1
    record = store.get(request.incident_id)
    assert record is not None
    assert record.interventions[0].state is InterventionState.SAFE_HALTED


def test_failed_executor_dry_run_enters_safe_halt_before_mutation() -> None:
    action = ScaleDeploymentAction(target=_target(), replicas=4)
    store, request, kubernetes, enrollment = _approved_request(action)
    kubernetes.reject_future_dry_runs("admission denied the current patch")

    with pytest.raises(InterventionExecutionError, match="revalidation rejected"):
        _executor(kubernetes, enrollment).consume(store, request)

    assert kubernetes.applied_patches == ()
    record = store.get(request.incident_id)
    assert record is not None
    assert record.incident.intervention_outcome is InterventionOutcome.SAFE_HALTED


def test_lease_loss_enters_safe_halt_before_mutation() -> None:
    action = ScaleDeploymentAction(target=_target(), replicas=4)
    store, request, kubernetes, enrollment = _approved_request(action)
    executor = DeterministicInterventionExecutor(
        kubernetes=kubernetes,
        enrollment=enrollment,
        leases=_LeaseLosingStore(InMemoryLeaseDatabase()),
        owner="executor-lease-loss-test",
    )

    with pytest.raises(InterventionExecutionError, match="lease was lost"):
        executor.consume(store, request)

    assert kubernetes.applied_patches == ()
    record = store.get(request.incident_id)
    assert record is not None
    assert record.interventions[0].state is InterventionState.SAFE_HALTED


class _LeaseLosingStore(FirestoreWorkloadLeaseStore):
    def renew(self, lease: WorkloadLease, *, now: datetime) -> WorkloadLease:
        raise ValueError("workload lease was lost before mutation")


def _approved_request(
    action: RemediationAction,
    *,
    store: IncidentStore | None = None,
) -> tuple[
    IncidentStore,
    InterventionRequest,
    FakeExecutorKubernetesProvider,
    FakeEnrollmentProvider,
]:
    store = store or InMemoryIncidentStore()
    profile = fake_application_profile()
    now = datetime.now(UTC)
    signal = AlertSignal(
        signal_id=f"manual-{action.action_type}",
        application_id=profile.application_id,
        namespace=profile.namespace,
        workload_name=action.target.name,
        workload_namespace=action.target.namespace,
        summary=f"{action.target.name} requires a bounded intervention",
        observed_at=now,
    )
    created = store.create(profile, signal)
    InitialEvidenceWindowCollector(
        FakeEvidenceProvider(), DeterministicEvidenceRedactor()
    ).collect(
        store,
        incident_id=created.incident.incident_id,
        profile=profile,
        signal=signal,
        window=created.evidence_window,
    )
    record = store.get(created.incident.incident_id)
    assert record is not None
    proposal = RemediationProposal(
        proposal_id=f"proposal-{action.action_type}",
        incident_id=record.incident.incident_id,
        action=action,
        expected_impact="Restore the checkout Critical Journey.",
        recovery_criteria=profile.recovery_criteria,
        rollback_strategy="Use only the action-specific deterministic failure strategy.",
        evidence_hash=evidence_hash(record.evidence),
    )
    citation = EvidenceCitation(
        evidence_id=record.evidence[0].evidence_id,
        observation="The bounded evidence supports testing this remediation.",
    )
    store.complete_investigation(
        record.incident.incident_id,
        CoordinatorOutput(
            outcome=InvestigationOutcome.PROPOSAL_READY,
            hypotheses=(
                RootCauseHypothesis(
                    hypothesis_id=f"hypothesis-{action.action_type}",
                    incident_id=record.incident.incident_id,
                    rank=1,
                    statement="The managed workload needs a bounded remediation.",
                    falsification_test="Apply once and verify deterministic convergence.",
                    confidence=0.8,
                    citations=(citation,),
                ),
            ),
            proposal=proposal,
        ),
    )
    kubernetes = FakeExecutorKubernetesProvider.ready()
    enrollment = FakeEnrollmentProvider.ready_for(profile)
    DeterministicProposalPolicy(kubernetes, enrollment).evaluate_and_record(
        store, record.incident.incident_id
    )
    queue = InMemoryInterventionQueue()
    approvals = ApprovalService(kubernetes, publisher=queue, clock=lambda: now)
    review = approvals.review(store, record.incident.incident_id, _responder())
    approvals.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    return store, queue.pending()[0], kubernetes, enrollment


def _executor(
    kubernetes: FakeExecutorKubernetesProvider,
    enrollment: FakeEnrollmentProvider,
) -> DeterministicInterventionExecutor:
    return DeterministicInterventionExecutor(
        kubernetes=kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-complete-actions-test",
    )


def _target():
    return TARGET


def _responder() -> OperatorIdentity:
    return OperatorIdentity(
        principal="responder@example.com",
        subject="responder-1",
        role=OperatorRole.RESPONDER,
    )
