from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.applications import router as applications_router
from app.api.health import router as health_router
from app.api.incidents import router as incidents_router
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    InMemoryApplicationProfileProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.observability import RequestIdMiddleware, configure_logging


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.incident_store = InMemoryIncidentStore()
    profile = fake_application_profile()
    application.state.application_profile_provider = InMemoryApplicationProfileProvider((profile,))
    application.state.enrollment_provider = FakeEnrollmentProvider.ready_for(profile)
    yield


configure_logging()
app = FastAPI(title="KubeCouncil", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.include_router(health_router)
app.include_router(applications_router)
app.include_router(incidents_router)
