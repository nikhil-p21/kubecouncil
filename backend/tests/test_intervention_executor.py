import asyncio
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
    AlertSignal,
    ApprovalDecision,
    AuditEvent,
    IncidentStore,
    Intervention,
    InterventionState,
)
from app.services.approval import ApprovalService
from app.services.council import BoundedIncidentCouncil, FakeIncidentCouncilModel
from app.services.evidence import DeterministicEvidenceRedactor, InitialEvidenceWindowCollector
from app.services.incident_store import FirestoreIncidentStore, InMemoryDocumentDatabase
from app.services.intervention_executor import (
    DeterministicInterventionExecutor,
    FakeExecutorKubernetesProvider,
    InterventionExecutionError,
)
from app.services.intervention_queue import (
    InMemoryInterventionQueue,
    intervention_request_hash,
)
from app.services.proposal_policy import DeterministicProposalPolicy
from app.services.workload_lease import FirestoreWorkloadLeaseStore, InMemoryLeaseDatabase


@pytest.mark.parametrize(
    "store",
    (
        InMemoryIncidentStore(),
        FirestoreIncidentStore(InMemoryDocumentDatabase()),
    ),
)
def test_approved_rollback_crosses_queue_and_executes_once_after_revalidation(
    store: IncidentStore,
) -> None:
    store, kubernetes, enrollment, approval = _awaiting_approval(store)
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())

    approved = approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )

    assert len(queue.pending()) == 1
    request = queue.pending()[0]
    assert request.payload_hash
    assert request.approval_id == approved.approvals[0].approval_id

    executor_kubernetes = FakeExecutorKubernetesProvider.from_policy_provider(kubernetes)
    executor = DeterministicInterventionExecutor(
        kubernetes=executor_kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )
    result = executor.consume(store, request)

    assert result.state.value == "succeeded"
    assert len(executor_kubernetes.applied_patches) == 1
    assert executor_kubernetes.applied_patches[0] == approved.policy_decision.patch
    completed = store.get(record.incident.incident_id)
    assert completed is not None
    assert completed.interventions == (result,)
    assert [
        event.event_type
        for event in completed.audit_events
        if event.event_type.startswith("intervention_")
    ] == [
        "intervention_requested",
        "intervention_received",
        "intervention_claimed",
        "intervention_validated",
        "intervention_dry_run_passed",
        "intervention_mutated",
        "intervention_converged",
    ]


def test_executor_rejects_workload_state_that_changed_after_approval() -> None:
    store, kubernetes, enrollment, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    request = queue.pending()[0]
    state = kubernetes.inspect_deployment(request.target)
    assert state is not None
    stale_kubernetes = FakeExecutorKubernetesProvider(
        (state.model_copy(update={"resource_version": "rv-external"}),)
    )
    executor = DeterministicInterventionExecutor(
        kubernetes=stale_kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )

    with pytest.raises(InterventionExecutionError, match="changed after Approval"):
        executor.consume(store, request)

    assert stale_kubernetes.applied_patches == ()
    rejected = store.get(request.incident_id)
    assert rejected is not None
    assert rejected.interventions[0].state.value == "safe_halted"
    assert rejected.incident.intervention_outcome.value == "safe_halted"


def test_unexpected_provider_failure_after_claim_enters_safe_halt() -> None:
    store, kubernetes, _, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    request = queue.pending()[0]
    executor_kubernetes = FakeExecutorKubernetesProvider.from_policy_provider(kubernetes)
    executor = DeterministicInterventionExecutor(
        kubernetes=executor_kubernetes,
        enrollment=ExplodingEnrollmentProvider(),
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )

    with pytest.raises(InterventionExecutionError, match="unexpected Executor failure"):
        executor.consume(store, request)

    failed = store.get(request.incident_id)
    assert failed is not None
    assert failed.interventions[0].state.value == "safe_halted"
    assert failed.incident.intervention_outcome.value == "safe_halted"
    assert failed.audit_events[-1].event_type == "intervention_safe_halted"


def test_rejected_human_decision_never_publishes_an_intervention() -> None:
    store, kubernetes, _, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())

    decided = approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.REJECTED,
        reviewed_binding=review.binding,
    )

    assert queue.pending() == ()
    assert all(
        event.event_type != "intervention_requested" for event in decided.audit_events
    )


def test_executor_rejects_tampered_authority_even_with_a_rehashed_message() -> None:
    store, kubernetes, enrollment, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    request = queue.pending()[0].model_copy(update={"approval_id": "approval-attacker"})
    request = request.model_copy(update={"payload_hash": intervention_request_hash(request)})
    executor_kubernetes = FakeExecutorKubernetesProvider.from_policy_provider(kubernetes)
    executor = DeterministicInterventionExecutor(
        kubernetes=executor_kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )

    with pytest.raises(InterventionExecutionError, match="approved authority"):
        executor.consume(store, request)

    assert executor_kubernetes.applied_patches == ()


def test_external_mutation_between_revalidation_and_apply_fails_optimistic_concurrency() -> None:
    store, kubernetes, enrollment, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    request = queue.pending()[0]
    executor_kubernetes = FakeExecutorKubernetesProvider.from_policy_provider(kubernetes)
    executor_kubernetes.mutate_before_next_apply(resource_version="rv-racing-writer")
    executor = DeterministicInterventionExecutor(
        kubernetes=executor_kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )

    with pytest.raises(InterventionExecutionError, match="resourceVersion mismatch"):
        executor.consume(store, request)

    assert executor_kubernetes.applied_patches == ()
    failed = store.get(request.incident_id)
    assert failed is not None
    assert failed.interventions[0].state.value == "safe_halted"
    assert failed.incident.intervention_outcome.value == "safe_halted"


def test_duplicate_delivery_returns_completed_intervention_without_a_second_write() -> None:
    store, kubernetes, enrollment, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    request = queue.pending()[0]
    executor_kubernetes = FakeExecutorKubernetesProvider.from_policy_provider(kubernetes)
    executor = DeterministicInterventionExecutor(
        kubernetes=executor_kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )

    first = executor.consume(store, request)
    duplicate = executor.consume(store, request)

    assert duplicate == first
    assert len(executor_kubernetes.applied_patches) == 1
    completed = store.get(request.incident_id)
    assert completed is not None
    assert completed.audit_events[-1].event_type == "intervention_duplicate_rejected"


def test_redelivery_resumes_running_intervention_before_any_mutation() -> None:
    store, kubernetes, enrollment, _ = _awaiting_approval()
    queue = InMemoryInterventionQueue()
    approval = ApprovalService(kubernetes, publisher=queue)
    record = store.list()[0]
    review = approval.review(store, record.incident.incident_id, _responder())
    approval.decide(
        store,
        incident_id=record.incident.incident_id,
        identity=_responder(),
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )
    request = queue.pending()[0]
    approved = store.get(request.incident_id)
    assert approved is not None
    running = Intervention(
        intervention_id=f"intervention-{request.idempotency_key[:24]}",
        incident_id=request.incident_id,
        proposal_id=request.proposal_id,
        approval_id=request.approval_id,
        target=request.target,
        state=InterventionState.RUNNING,
        requested_at=request.requested_at,
        idempotency_key=request.idempotency_key,
    )
    store.record_intervention(
        request.incident_id,
        approved.incident.version,
        running,
        AuditEvent(
            event_id="audit-interrupted-claim",
            incident_id=request.incident_id,
            event_type="intervention_claimed",
            occurred_at=datetime.now(UTC),
            actor="deterministic-executor",
        ),
    )
    executor_kubernetes = FakeExecutorKubernetesProvider.from_policy_provider(kubernetes)
    executor = DeterministicInterventionExecutor(
        kubernetes=executor_kubernetes,
        enrollment=enrollment,
        leases=FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase()),
        owner="executor-test-1",
    )

    resumed = executor.consume(store, request)

    assert resumed.state is InterventionState.SUCCEEDED
    assert len(executor_kubernetes.applied_patches) == 1
    completed = store.get(request.incident_id)
    assert completed is not None
    assert any(
        event.event_type == "intervention_resumed" for event in completed.audit_events
    )


class ExplodingEnrollmentProvider:
    def inspect(self, profile: object) -> object:
        raise RuntimeError("protected dependency read was forbidden")


def test_firestore_shaped_lease_serializes_and_renews_workload_ownership() -> None:
    leases = FirestoreWorkloadLeaseStore(InMemoryLeaseDatabase())
    target = fake_application_profile().workloads[0].reference
    now = datetime.now(UTC)

    first = leases.acquire(
        target, intervention_id="intervention-1", owner="executor-1", now=now
    )
    blocked = leases.acquire(
        target, intervention_id="intervention-2", owner="executor-2", now=now
    )

    assert first is not None
    assert blocked is None
    renewed = leases.renew(first, now=now)
    assert renewed.expires_at >= first.expires_at
    leases.release(renewed)
    assert (
        leases.acquire(
            target, intervention_id="intervention-2", owner="executor-2", now=now
        )
        is not None
    )


def _awaiting_approval(
    store: IncidentStore | None = None,
) -> tuple[
    IncidentStore,
    FakeExecutorKubernetesProvider,
    FakeEnrollmentProvider,
    ApprovalService,
]:
    store = store or InMemoryIncidentStore()
    profile = fake_application_profile()
    signal = AlertSignal(
        signal_id="manual-intervention-test",
        application_id=profile.application_id,
        namespace=profile.namespace,
        workload_name="recommendationservice",
        workload_namespace=profile.namespace,
        summary="recommendationservice is OOMKilled",
        observed_at=datetime.now(UTC),
    )
    record = store.create(profile, signal)
    InitialEvidenceWindowCollector(
        FakeEvidenceProvider(), DeterministicEvidenceRedactor()
    ).collect(
        store,
        incident_id=record.incident.incident_id,
        profile=profile,
        signal=signal,
        window=record.evidence_window,
    )
    asyncio.run(
        BoundedIncidentCouncil(FakeIncidentCouncilModel()).investigate(
            store, record.incident.incident_id
        )
    )
    kubernetes = FakeExecutorKubernetesProvider.ready()
    enrollment = FakeEnrollmentProvider.ready_for(profile)
    DeterministicProposalPolicy(kubernetes, enrollment).evaluate_and_record(
        store, record.incident.incident_id
    )
    return store, kubernetes, enrollment, ApprovalService(kubernetes)


def _responder() -> OperatorIdentity:
    return OperatorIdentity(
        principal="responder@example.com",
        subject="responder-1",
        role=OperatorRole.RESPONDER,
    )
