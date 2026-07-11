import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.incidents import (
    get_incident_council,
    get_incident_store,
    get_proposal_policy,
)
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import (
    AlertSignal,
    Approval,
    ApprovalDecision,
    InvestigationRecord,
    PolicyDecision,
    RemediationProposal,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    ScaleDeploymentAction,
    WorkloadReference,
)
from app.main import app
from app.services.council import BoundedIncidentCouncil, FakeIncidentCouncilModel
from app.services.evidence import DeterministicEvidenceRedactor, InitialEvidenceWindowCollector
from app.services.proposal_policy import (
    DeterministicProposalPolicy,
    FakePolicyKubernetesProvider,
    PolicyCheckCode,
    PolicyStatus,
    evidence_hash,
)


def test_rollback_policy_persists_exact_patch_and_server_dry_run() -> None:
    store, incident_id = _investigated_incident()
    provider = FakePolicyKubernetesProvider.ready()
    policy = DeterministicProposalPolicy(
        provider, FakeEnrollmentProvider.ready_for(fake_application_profile())
    )

    result = policy.evaluate_and_record(store, incident_id)

    assert result.policy_decision is not None
    assert result.policy_decision.status is PolicyStatus.PASSED
    assert all(check.passed for check in result.policy_decision.checks)
    assert result.policy_decision.patch is not None
    assert result.policy_decision.patch.body == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "recommendationservice",
            "namespace": "online-boutique",
            "resourceVersion": "rv-8",
        },
        "spec": {"template": {"metadata": {"labels": {"revision": "7"}}}},
    }
    assert result.policy_decision.dry_run_diff == (
        "Deployment/recommendationservice: revision 8 -> 7"
    )
    assert result.incident.lifecycle.value == "awaiting_approval"
    assert provider.dry_run_patches == (result.policy_decision.patch,)
    assert result.audit_events[-1].event_type == "proposal_policy_passed"


@pytest.mark.parametrize(
    ("action", "expected_spec"),
    [
        (
            ScaleDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                replicas=4,
            ),
            {"replicas": 4},
        ),
        (
            RestartDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                restart_token="incident-inc-policy-1",
            ),
            {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubecouncil.io/restart-token": "incident-inc-policy-1"
                        }
                    }
                }
            },
        ),
    ],
)
def test_scale_and_restart_use_action_specific_minimal_patches(
    action: ScaleDeploymentAction | RestartDeploymentAction,
    expected_spec: dict[str, object],
) -> None:
    record, proposal = _record_and_proposal(action)
    policy = DeterministicProposalPolicy(
        FakePolicyKubernetesProvider.ready(),
        FakeEnrollmentProvider.ready_for(record.application_profile),
    )

    decision = policy.evaluate(record, proposal)

    assert decision.status is PolicyStatus.PASSED
    assert decision.patch is not None
    assert decision.patch.body["spec"] == expected_spec


@pytest.mark.parametrize(
    ("provider", "action", "failed_check"),
    [
        (
            FakePolicyKubernetesProvider.ready(active_intervention=True),
            RollbackDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                revision=7,
            ),
            PolicyCheckCode.NO_ACTIVE_INTERVENTION,
        ),
        (
            FakePolicyKubernetesProvider.ready(available_revisions=(8,)),
            RollbackDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                revision=7,
            ),
            PolicyCheckCode.REVISION_AVAILABLE,
        ),
        (
            FakePolicyKubernetesProvider.ready(implicated_revisions=(7,)),
            RollbackDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                revision=7,
            ),
            PolicyCheckCode.RESTORATION_SAFE,
        ),
        (
            FakePolicyKubernetesProvider.ready(),
            ScaleDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                replicas=6,
            ),
            PolicyCheckCode.REPLICA_BOUNDS,
        ),
        (
            FakePolicyKubernetesProvider.ready(replica_quota_headroom=1),
            ScaleDeploymentAction(
                target=WorkloadReference(
                    namespace="online-boutique", name="recommendationservice"
                ),
                replicas=5,
            ),
            PolicyCheckCode.REPLICA_QUOTA,
        ),
    ],
)
def test_policy_rejects_live_state_bounds_quota_and_restoration_violations(
    provider: FakePolicyKubernetesProvider,
    action: RollbackDeploymentAction | ScaleDeploymentAction,
    failed_check: PolicyCheckCode,
) -> None:
    record, proposal = _record_and_proposal(action)
    policy = DeterministicProposalPolicy(
        provider, FakeEnrollmentProvider.ready_for(record.application_profile)
    )

    decision = policy.evaluate(record, proposal)

    assert decision.status is PolicyStatus.REJECTED
    assert any(check.code is failed_check and not check.passed for check in decision.checks)
    assert provider.dry_run_patches == ()


def test_policy_rejects_protected_unmanaged_disallowed_and_stale_targets() -> None:
    record, rollback = _record_and_proposal(
        RollbackDeploymentAction(
            target=WorkloadReference(
                namespace="online-boutique", name="recommendationservice"
            ),
            revision=7,
        )
    )
    policy = DeterministicProposalPolicy(
        FakePolicyKubernetesProvider.ready(),
        FakeEnrollmentProvider.ready_for(record.application_profile),
    )
    protected = rollback.model_copy(
        update={
            "action": RollbackDeploymentAction(
                target=WorkloadReference(namespace="online-boutique", name="redis-cart"),
                revision=1,
            )
        }
    )
    unmanaged = rollback.model_copy(
        update={
            "action": RestartDeploymentAction(
                target=WorkloadReference(namespace="online-boutique", name="unknown"),
                restart_token="manual-only",
            )
        }
    )
    disallowed_profile = record.application_profile.model_copy(
        update={
            "workloads": (
                record.application_profile.workloads[0].model_copy(
                    update={"allowed_actions": ("scale_deployment",)}
                ),
                record.application_profile.workloads[1],
            )
        }
    )

    protected_decision = policy.evaluate(record, protected)
    unmanaged_decision = policy.evaluate(record, unmanaged)
    disallowed_decision = DeterministicProposalPolicy(
        FakePolicyKubernetesProvider.ready(),
        FakeEnrollmentProvider.ready_for(disallowed_profile),
    ).evaluate(record.model_copy(update={"application_profile": disallowed_profile}), rollback)
    stale_decision = policy.evaluate(
        record,
        rollback.model_copy(update={"evidence_hash": "stale-evidence-hash"}),
    )

    assert _failed(protected_decision, PolicyCheckCode.TARGET_EXECUTABLE)
    assert _failed(unmanaged_decision, PolicyCheckCode.TARGET_ENROLLED)
    assert _failed(disallowed_decision, PolicyCheckCode.ACTION_ALLOWED)
    assert _failed(stale_decision, PolicyCheckCode.EVIDENCE_CURRENT)


def test_dry_run_failure_is_a_terminal_policy_rejection() -> None:
    store, incident_id = _investigated_incident()
    provider = FakePolicyKubernetesProvider.ready(dry_run_error="admission denied")
    policy = DeterministicProposalPolicy(
        provider, FakeEnrollmentProvider.ready_for(fake_application_profile())
    )

    result = policy.evaluate_and_record(store, incident_id)

    assert result.policy_decision is not None
    assert result.policy_decision.status is PolicyStatus.DRY_RUN_FAILED
    assert result.policy_decision.rejection_reason == "admission denied"
    with pytest.raises(ValueError, match="already recorded"):
        policy.evaluate_and_record(store, incident_id)


def test_proposal_cardinality_and_action_shape_are_strict() -> None:
    record, proposal = _record_and_proposal(
        RollbackDeploymentAction(
            target=WorkloadReference(
                namespace="online-boutique", name="recommendationservice"
            ),
            revision=7,
        )
    )
    payload = proposal.model_dump(mode="json")
    action_payload = payload.pop("action")
    payload["actions"] = [action_payload, action_payload]

    with pytest.raises(ValidationError):
        RemediationProposal.model_validate(payload)
    unsupported = proposal.model_dump(mode="json")
    unsupported["action"]["action_type"] = "delete_deployment"
    with pytest.raises(ValidationError):
        RemediationProposal.model_validate(unsupported)
    assert record.proposal is None


def test_rejected_policy_cannot_be_overridden_by_later_approval() -> None:
    record, proposal = _record_and_proposal(
        ScaleDeploymentAction(
            target=WorkloadReference(
                namespace="online-boutique", name="recommendationservice"
            ),
            replicas=99,
        )
    )
    decision = DeterministicProposalPolicy(
        FakePolicyKubernetesProvider.ready(),
        FakeEnrollmentProvider.ready_for(record.application_profile),
    ).evaluate(record, proposal)
    now = datetime.now(UTC)
    approval = Approval(
        approval_id="approval-policy-override",
        incident_id=record.incident.incident_id,
        proposal_id=proposal.proposal_id,
        responder_principal="responder@example.com",
        decision=ApprovalDecision.APPROVED,
        decided_at=now,
        expires_at=now + timedelta(minutes=5),
        proposal_hash="proposal-hash",
        evidence_hash=proposal.evidence_hash,
        workload_version="rv-8",
        policy_hash="rejected-policy-hash",
        dry_run_hash="no-dry-run-hash",
        recovery_criteria_hash="recovery-hash",
        failure_strategy_hash="failure-hash",
    )
    invalid = record.model_copy(
        update={
            "proposal": proposal,
            "policy_decision": decision,
            "approvals": (approval,),
        }
    )

    with pytest.raises(ValidationError, match="cannot override"):
        InvestigationRecord.model_validate(invalid.model_dump())


def test_investigate_api_returns_a_policy_checked_proposal() -> None:
    store = _incident_store()
    incident_id = store.list()[0].incident.incident_id
    council = BoundedIncidentCouncil(FakeIncidentCouncilModel())
    policy = DeterministicProposalPolicy(
        FakePolicyKubernetesProvider.ready(),
        FakeEnrollmentProvider.ready_for(fake_application_profile()),
    )
    app.dependency_overrides[get_incident_store] = lambda: store
    app.dependency_overrides[get_incident_council] = lambda: council
    app.dependency_overrides[get_proposal_policy] = lambda: policy
    client = TestClient(app)
    try:
        response = client.post(f"/api/incidents/{incident_id}/investigate")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["policy_decision"]["status"] == "passed"
    assert body["policy_decision"]["dry_run_diff"]
    assert body["audit_events"][-1]["event_type"] == "proposal_policy_passed"


def _failed(decision: PolicyDecision, code: PolicyCheckCode) -> bool:
    return any(check.code is code and not check.passed for check in decision.checks)


def _record_and_proposal(
    action: RollbackDeploymentAction | ScaleDeploymentAction | RestartDeploymentAction,
) -> tuple[InvestigationRecord, RemediationProposal]:
    store = _incident_store()
    record = store.list()[0]
    proposal = RemediationProposal(
        proposal_id="proposal-policy-test",
        incident_id=record.incident.incident_id,
        action=action,
        expected_impact="Restore checkout health.",
        recovery_criteria=record.application_profile.recovery_criteria,
        rollback_strategy="Enter Safe Halt if restoration cannot be proven safe.",
        evidence_hash=evidence_hash(record.evidence),
        known_risks=("A rollout may temporarily reduce available capacity.",),
    )
    return record, proposal


def _incident_store() -> InMemoryIncidentStore:
    store = InMemoryIncidentStore()
    profile = fake_application_profile()
    signal = AlertSignal(
        signal_id="manual-policy-test",
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
    return store


def _investigated_incident() -> tuple[InMemoryIncidentStore, str]:
    store = _incident_store()
    incident_id = store.list()[0].incident.incident_id
    asyncio.run(BoundedIncidentCouncil(FakeIncidentCouncilModel()).investigate(store, incident_id))
    return store, incident_id
