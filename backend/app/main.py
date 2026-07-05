from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.repositories import router as repositories_router
from app.api.runs import router as runs_router
from app.observability import RequestIdMiddleware, configure_logging

configure_logging()
app = FastAPI(title="KubeCouncil", version="0.1.0")
app.add_middleware(RequestIdMiddleware)
app.include_router(health_router)
app.include_router(repositories_router)
app.include_router(runs_router)
