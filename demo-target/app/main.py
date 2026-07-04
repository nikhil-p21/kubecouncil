import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI
from pydantic import BaseModel, Field


class WorkResult(BaseModel):
    service: str
    mode: str
    latency_ms: int
    cpu_iterations: int
    downstream: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class DemoSettings:
    service_name: str
    mode: str
    latency_ms: int
    cpu_iterations: int
    checkout_url: str
    payment_url: str
    recommendation_url: str
    mock_internal_calls: bool

    @classmethod
    def from_env(cls) -> "DemoSettings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", "gateway"),
            mode=os.getenv("MODE", "live"),
            latency_ms=int(os.getenv("LATENCY_MS", "25")),
            cpu_iterations=int(os.getenv("CPU_ITERATIONS", "2000")),
            checkout_url=os.getenv("CHECKOUT_URL", "http://checkout/work"),
            payment_url=os.getenv("PAYMENT_URL", "http://payment/work"),
            recommendation_url=os.getenv("RECOMMENDATION_URL", "http://recommendation/work"),
            mock_internal_calls=os.getenv("MOCK_INTERNAL_CALLS", "false").lower() == "true",
        )


def create_app(settings: DemoSettings | None = None) -> FastAPI:
    app_settings = settings or DemoSettings.from_env()
    app = FastAPI(title=f"KubeCouncil demo {app_settings.service_name}")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": app_settings.service_name}

    @app.get("/")
    def root() -> WorkResult:
        return run_service(app_settings)

    @app.get("/work")
    def work() -> WorkResult:
        return run_service(app_settings)

    return app


def run_service(settings: DemoSettings) -> WorkResult:
    effective_latency = _latency_for_mode(settings)
    _burn_cpu(settings.cpu_iterations)
    time.sleep(effective_latency / 1000)

    downstream: dict[str, Any] = {}
    if settings.service_name == "gateway":
        downstream["checkout"] = _call_downstream("checkout", settings.checkout_url, settings)
    elif settings.service_name == "checkout":
        downstream["payment"] = _call_downstream("payment", settings.payment_url, settings)
        downstream["recommendation"] = _call_downstream(
            "recommendation",
            settings.recommendation_url,
            settings,
        )

    return WorkResult(
        service=settings.service_name,
        mode=settings.mode,
        latency_ms=effective_latency,
        cpu_iterations=settings.cpu_iterations,
        downstream=downstream,
    )


def _latency_for_mode(settings: DemoSettings) -> int:
    if settings.service_name == "recommendation" and settings.mode == "cached":
        return max(5, settings.latency_ms // 4)
    return settings.latency_ms


def _burn_cpu(iterations: int) -> int:
    total = 0
    for index in range(iterations):
        total += (index * index) % 97
    return total


def _call_downstream(name: str, url: str, settings: DemoSettings) -> dict[str, Any]:
    if settings.mock_internal_calls:
        return {"service": name, "status": "mocked"}

    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (TimeoutError, URLError, json.JSONDecodeError) as exc:
        return {"service": name, "status": "unavailable", "error": exc.__class__.__name__}


app = create_app()
