import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.identity import get_current_identity, get_identity_provider
from app.api.incidents import (
    get_approval_service,
    get_incident_council,
    get_incident_store,
    get_proposal_policy,
)
from app.domain.identity import OperatorIdentity, OperatorRole
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import AlertSignal, ApprovalDecision, IncidentStore
from app.main import app
from app.services.approval import ApprovalError, ApprovalService
from app.services.council import BoundedIncidentCouncil, FakeIncidentCouncilModel
from app.services.evidence import DeterministicEvidenceRedactor, InitialEvidenceWindowCollector
from app.services.identity import IAPIdentityProvider, IdentityError, LocalIdentityProvider
from app.services.incident_store import FirestoreIncidentStore, InMemoryDocumentDatabase
from app.services.proposal_policy import (
    DeterministicProposalPolicy,
    FakePolicyKubernetesProvider,
)


class RecordingTokenVerifier:
    def __init__(self, claims: dict[str, object] | None = None) -> None:
        self.claims = claims or {
            "iss": "https://cloud.google.com/iap",
            "sub": "accounts.google.com:1234",
            "email": "responder@example.com",
        }
        self.calls: list[tuple[str, str]] = []

    def verify(self, assertion: str, audience: str) -> dict[str, object]:
        self.calls.append((assertion, audience))
        return self.claims


def test_iap_identity_verifies_assertion_and_maps_explicit_responder() -> None:
    verifier = RecordingTokenVerifier()
    provider = IAPIdentityProvider(
        audience="/projects/123/global/backendServices/456",
        responder_principals=frozenset({"responder@example.com"}),
        token_verifier=verifier,
    )

    identity = provider.authenticate("signed-assertion")

    assert identity == OperatorIdentity(
        principal="responder@example.com",
        subject="accounts.google.com:1234",
        role=OperatorRole.RESPONDER,
    )
    assert verifier.calls == [
        ("signed-assertion", "/projects/123/global/backendServices/456")
    ]


def test_iap_identity_maps_wildcard_responder() -> None:
    verifier = RecordingTokenVerifier()
    provider = IAPIdentityProvider(
        audience="/projects/123/global/backendServices/456",
        responder_principals=frozenset({"*"}),
        token_verifier=verifier,
    )

    identity = provider.authenticate("signed-assertion")

    assert identity == OperatorIdentity(
        principal="responder@example.com",
        subject="accounts.google.com:1234",
        role=OperatorRole.RESPONDER,
    )


def test_iap_identity_rejects_invalid_issuer_and_local_identity_is_development_only() -> None:
    provider = IAPIdentityProvider(
        audience="iap-audience",
        responder_principals=frozenset(),
        token_verifier=RecordingTokenVerifier(
            {
                "iss": "https://attacker.example",
                "sub": "subject",
                "email": "viewer@example.com",
            }
        ),
    )

    with pytest.raises(IdentityError, match="issuer"):
        provider.authenticate("forged")
    with pytest.raises(ValueError, match="development"):
        LocalIdentityProvider(
            runtime_mode="deployed",
            identity=OperatorIdentity(
                principal="local@example.com",
                subject="local",
                role=OperatorRole.RESPONDER,
            ),
        )


def test_identity_api_requires_and_verifies_the_signed_iap_header() -> None:
    provider = IAPIdentityProvider(
        audience="iap-audience",
        responder_principals=frozenset({"responder@example.com"}),
        token_verifier=RecordingTokenVerifier(),
    )
    app.dependency_overrides[get_identity_provider] = lambda: provider
    client = TestClient(app)
    try:
        missing = client.get("/api/identity/me")
        verified = client.get(
            "/api/identity/me",
            headers={"X-Goog-IAP-JWT-Assertion": "signed-assertion"},
        )
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 401
    assert verified.status_code == 200
    assert verified.json()["role"] == "responder"


def test_viewer_can_read_but_cannot_trigger_an_investigation() -> None:
    store, policy, approval = _awaiting_approval_store()
    incident_id = store.list()[0].incident.incident_id
    app.dependency_overrides[get_incident_store] = lambda: store
    app.dependency_overrides[get_incident_council] = lambda: BoundedIncidentCouncil(
        FakeIncidentCouncilModel()
    )
    app.dependency_overrides[get_proposal_policy] = lambda: policy
    app.dependency_overrides[get_approval_service] = lambda: approval
    app.dependency_overrides[get_current_identity] = lambda: OperatorIdentity(
        principal="viewer@example.com",
        subject="viewer-1",
        role=OperatorRole.VIEWER,
    )
    client = TestClient(app)
    try:
        read = client.get(f"/api/incidents/{incident_id}")
        investigate = client.post(f"/api/incidents/{incident_id}/investigate")
        close = client.post(
            f"/api/incidents/{incident_id}/close",
            json={"expected_version": store.list()[0].incident.version},
        )
    finally:
        app.dependency_overrides.clear()

    assert read.status_code == 200
    assert investigate.status_code == 403
    assert investigate.json()["detail"]["code"] == "responder_required"
    assert close.status_code == 403


def test_approval_binds_full_review_context_and_records_an_immutable_audit_event() -> None:
    store, _, service = _awaiting_approval_store()
    incident_id = store.list()[0].incident.incident_id
    identity = _responder()
    review = service.review(store, incident_id, identity)

    result = service.decide(
        store,
        incident_id=incident_id,
        identity=identity,
        decision=ApprovalDecision.APPROVED,
        reviewed_binding=review.binding,
    )

    recorded = result.approvals[0]
    assert recorded.responder_principal == identity.principal
    assert recorded.proposal_hash == review.binding.proposal_hash
    assert recorded.evidence_hash == review.binding.evidence_hash
    assert recorded.workload_resource_version == "rv-8"
    assert recorded.workload_generation == 8
    assert recorded.workload_revision == 8
    assert recorded.policy_hash == review.binding.policy_hash
    assert recorded.dry_run_hash == review.binding.dry_run_hash
    assert recorded.recovery_criteria_hash == review.binding.recovery_criteria_hash
    assert recorded.failure_strategy_hash == review.binding.failure_strategy_hash
    assert result.audit_events[-1].event_type == "proposal_approved"
    assert result.audit_events[-1].actor == identity.principal
    assert result.interventions == ()

    with pytest.raises(ApprovalError, match="already decided"):
        service.decide(
            store,
            incident_id=incident_id,
            identity=identity,
            decision=ApprovalDecision.APPROVED,
            reviewed_binding=review.binding,
        )


@pytest.mark.parametrize(
    "binding_update",
    [
        {"proposal_hash": "tampered-proposal-hash"},
        {"evidence_hash": "tampered-evidence-hash"},
        {"workload_resource_version": "rv-stale"},
        {"workload_generation": 7},
        {"workload_revision": 7},
        {"policy_hash": "tampered-policy-hash"},
        {"dry_run_hash": "tampered-dry-run-hash"},
        {"recovery_criteria_hash": "tampered-recovery-hash"},
        {"failure_strategy_hash": "tampered-failure-hash"},
    ],
)
def test_mismatched_or_expired_review_binding_rejects_without_intervention(
    binding_update: dict[str, object],
) -> None:
    store, _, service = _awaiting_approval_store()
    incident_id = store.list()[0].incident.incident_id
    identity = _responder()
    review = service.review(store, incident_id, identity)
    stale = review.binding.model_copy(update=binding_update)

    with pytest.raises(ApprovalError, match="review context is stale"):
        service.decide(
            store,
            incident_id=incident_id,
            identity=identity,
            decision=ApprovalDecision.APPROVED,
            reviewed_binding=stale,
        )

    record = store.get(incident_id)
    assert record is not None
    assert record.approvals == ()
    assert record.interventions == ()

    expired = review.binding.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    with pytest.raises(ApprovalError, match="expired"):
        service.decide(
            store,
            incident_id=incident_id,
            identity=identity,
            decision=ApprovalDecision.APPROVED,
            reviewed_binding=expired,
        )


def test_concurrent_approval_claims_allow_exactly_one_decision() -> None:
    store, _, service = _awaiting_approval_store(
        FirestoreIncidentStore(InMemoryDocumentDatabase())
    )
    incident_id = store.list()[0].incident.incident_id
    identity = _responder()
    review = service.review(store, incident_id, identity)

    def decide(decision: ApprovalDecision) -> str:
        try:
            service.decide(
                store,
                incident_id=incident_id,
                identity=identity,
                decision=decision,
                reviewed_binding=review.binding,
            )
        except ApprovalError:
            return "rejected"
        return "recorded"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(decide, ApprovalDecision))

    assert sorted(outcomes) == ["recorded", "rejected"]
    record = store.get(incident_id)
    assert record is not None
    assert len(record.approvals) == 1
    assert len(
        [event for event in record.audit_events if event.event_type.startswith("proposal_")]
    ) == 2  # policy plus exactly one human decision


def test_approval_api_returns_review_context_and_rejects_replay() -> None:
    store, _, service = _awaiting_approval_store()
    incident_id = store.list()[0].incident.incident_id
    app.dependency_overrides[get_incident_store] = lambda: store
    app.dependency_overrides[get_approval_service] = lambda: service
    client = TestClient(app)
    try:
        review = client.get(f"/api/incidents/{incident_id}/approval-review")
        first = client.post(
            f"/api/incidents/{incident_id}/approval-decisions",
            json={
                "decision": "approved",
                "reviewed_binding": review.json()["binding"],
            },
        )
        replay = client.post(
            f"/api/incidents/{incident_id}/approval-decisions",
            json={
                "decision": "approved",
                "reviewed_binding": review.json()["binding"],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert review.status_code == 200
    assert review.json()["binding"]["workload_generation"] == 8
    assert first.status_code == 200
    assert first.json()["approvals"][0]["responder_principal"] == (
        "test-responder@example.com"
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "approval_rejected"


def _responder() -> OperatorIdentity:
    return OperatorIdentity(
        principal="responder@example.com",
        subject="responder-1",
        role=OperatorRole.RESPONDER,
    )


def _awaiting_approval_store(
    store: IncidentStore | None = None,
) -> tuple[IncidentStore, DeterministicProposalPolicy, ApprovalService]:
    store = store or InMemoryIncidentStore()
    profile = fake_application_profile()
    observed_at = datetime.now(UTC)
    signal = AlertSignal(
        signal_id="manual-approval-test",
        application_id=profile.application_id,
        namespace=profile.namespace,
        workload_name="recommendationservice",
        workload_namespace=profile.namespace,
        summary="recommendationservice is OOMKilled",
        observed_at=observed_at,
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
    kubernetes = FakePolicyKubernetesProvider.ready()
    policy = DeterministicProposalPolicy(
        kubernetes, FakeEnrollmentProvider.ready_for(profile)
    )
    policy.evaluate_and_record(store, record.incident.incident_id)
    return store, policy, ApprovalService(kubernetes)
