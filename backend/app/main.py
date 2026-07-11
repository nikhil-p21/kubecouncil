from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.incidents import router as incidents_router
from app.domain.incident_fakes import InMemoryIncidentStore
from app.observability import RequestIdMiddleware, configure_logging


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.incident_store = InMemoryIncidentStore()
    yield


configure_logging()
app = FastAPI(title="KubeCouncil", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.include_router(health_router)
app.include_router(incidents_router)
