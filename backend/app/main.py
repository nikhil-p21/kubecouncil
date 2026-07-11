import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.applications import router as applications_router
from app.api.health import router as health_router
from app.api.identity import router as identity_router
from app.api.incidents import router as incidents_router
from app.domain.identity import OperatorIdentity, OperatorRole
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryApplicationProfileProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import EvidenceSource
from app.observability import RequestIdMiddleware, configure_logging
from app.services.approval import ApprovalService
from app.services.council import BoundedIncidentCouncil, FakeIncidentCouncilModel
from app.services.evidence import DeterministicEvidenceRedactor
from app.services.evidence_gateway import EvidenceQueryGateway, FakeEvidenceQueryAdapter
from app.services.identity import (
    IAPIdentityProvider,
    LocalIdentityProvider,
    UnavailableIdentityProvider,
)
from app.services.intervention_queue import InMemoryInterventionQueue
from app.services.proposal_policy import (
    DeterministicProposalPolicy,
    FakePolicyKubernetesProvider,
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.incident_store = InMemoryIncidentStore()
    profile = fake_application_profile()
    application.state.application_profile_provider = InMemoryApplicationProfileProvider((profile,))
    application.state.enrollment_provider = FakeEnrollmentProvider.ready_for(profile)
    application.state.evidence_provider = FakeEvidenceProvider()
    application.state.evidence_redactor = DeterministicEvidenceRedactor()
    fake_query_adapter = FakeEvidenceQueryAdapter()
    application.state.evidence_query_gateway = EvidenceQueryGateway(
        adapters={
            EvidenceSource.KUBERNETES: fake_query_adapter,
            EvidenceSource.CLOUD_LOGGING: fake_query_adapter,
            EvidenceSource.CLOUD_MONITORING: fake_query_adapter,
        },
        redactor=application.state.evidence_redactor,
    )
    application.state.incident_council = BoundedIncidentCouncil(
        FakeIncidentCouncilModel(),
        evidence_gateway=application.state.evidence_query_gateway,
    )
    policy_kubernetes = FakePolicyKubernetesProvider.ready()
    application.state.proposal_policy = DeterministicProposalPolicy(
        policy_kubernetes,
        application.state.enrollment_provider,
    )
    application.state.intervention_publisher = InMemoryInterventionQueue()
    application.state.approval_service = ApprovalService(
        policy_kubernetes,
        publisher=application.state.intervention_publisher,
    )
    runtime_mode = os.getenv("KUBECOUNCIL_RUNTIME_MODE", "deployed")
    if runtime_mode == "development":
        principal = os.getenv("KUBECOUNCIL_LOCAL_PRINCIPAL", "developer@example.com")
        application.state.identity_provider = LocalIdentityProvider(
            runtime_mode=runtime_mode,
            identity=OperatorIdentity(
                principal=principal,
                subject=f"local:{principal}",
                role=OperatorRole.RESPONDER,
            ),
        )
    else:
        audience = os.getenv("KUBECOUNCIL_IAP_AUDIENCE")
        responders = frozenset(
            item.strip()
            for item in os.getenv("KUBECOUNCIL_RESPONDER_PRINCIPALS", "").split(",")
            if item.strip()
        )
        application.state.identity_provider = (
            IAPIdentityProvider(
                audience=audience,
                responder_principals=responders,
            )
            if audience
            else UnavailableIdentityProvider()
        )
    yield


configure_logging()
app = FastAPI(title="KubeCouncil", version="0.1.0", lifespan=lifespan)
app.state.identity_provider = UnavailableIdentityProvider()
app.add_middleware(RequestIdMiddleware)
app.include_router(health_router)
app.include_router(identity_router)
app.include_router(applications_router)
app.include_router(incidents_router)
