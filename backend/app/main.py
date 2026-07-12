import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.applications import router as applications_router
from app.api.health import router as health_router
from app.api.identity import router as identity_router
from app.api.incidents import router as incidents_router
from app.observability import RequestIdMiddleware, configure_logging
from app.runtime.composition import compose_runtime
from app.runtime.readiness import ReadinessRegistry
from app.runtime.workers import run_alert_worker
from app.services.identity import UnavailableIdentityProvider


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    await compose_runtime(application)
    alert_task: asyncio.Task[None] | None = None
    if application.state.runtime_mode == "deployed":
        alert_task = asyncio.create_task(run_alert_worker(application.state))
    try:
        yield
    finally:
        if alert_task is not None:
            alert_task.cancel()
            try:
                await alert_task
            except asyncio.CancelledError:
                pass


configure_logging()
app = FastAPI(title="KubeCouncil", version="0.1.0", lifespan=lifespan)
app.state.identity_provider = UnavailableIdentityProvider()
initial_readiness = ReadinessRegistry()
initial_readiness.set("api", ready=True, detail="API process is available.")
app.state.readiness = initial_readiness
app.add_middleware(RequestIdMiddleware)
app.include_router(health_router)
app.include_router(identity_router)
app.include_router(applications_router)
app.include_router(incidents_router)
