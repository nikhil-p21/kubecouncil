from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.applications import router as applications_router
from app.api.health import router as health_router
from app.api.incidents import router as incidents_router
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryApplicationProfileProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import EvidenceSource
from app.observability import RequestIdMiddleware, configure_logging
from app.services.evidence import DeterministicEvidenceRedactor
from app.services.evidence_gateway import EvidenceQueryGateway, FakeEvidenceQueryAdapter


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
    yield


configure_logging()
app = FastAPI(title="KubeCouncil", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.include_router(health_router)
app.include_router(applications_router)
app.include_router(incidents_router)
