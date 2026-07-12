"""Internal demo-only HTTP surface for controlled scenario transitions."""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from app.scenario_controller.kubernetes import KubernetesApiScenarioProvider
from app.scenario_controller.models import (
    ScenarioAction,
    ScenarioAuditEvent,
    ScenarioControllerConfig,
    ScenarioName,
    ScenarioTransitionResult,
)
from app.scenario_controller.service import (
    LoggingScenarioAuditStore,
    ScenarioController,
    ScenarioSafetyError,
)


def config_from_environment() -> ScenarioControllerConfig:
    return ScenarioControllerConfig(
        demo_mode=os.getenv("KUBECOUNCIL_DEMO_MODE", "false").lower() == "true",
        application_namespace=os.getenv(
            "KUBECOUNCIL_APPLICATION_NAMESPACE", "online-boutique"
        ),
        recommendation_deployment="recommendationservice",
        recommendation_container="server",
        recommendation_safe_memory_request=os.getenv(
            "RECOMMENDATION_SAFE_MEMORY_REQUEST", "220Mi"
        ),
        recommendation_safe_memory_limit=os.getenv(
            "RECOMMENDATION_SAFE_MEMORY_LIMIT", "450Mi"
        ),
        recommendation_unsafe_memory_request=os.getenv(
            "RECOMMENDATION_UNSAFE_MEMORY_REQUEST", "25Mi"
        ),
        recommendation_unsafe_memory_limit=os.getenv(
            "RECOMMENDATION_UNSAFE_MEMORY_LIMIT", "25Mi"
        ),
        redis_deployment="redis-cart",
        redis_safe_replicas=1,
    )


def build_controller(config: ScenarioControllerConfig) -> ScenarioController:
    return ScenarioController(
        config,
        KubernetesApiScenarioProvider(config),
        LoggingScenarioAuditStore(),
    )


def create_app(
    controller: ScenarioController | None = None,
    config: ScenarioControllerConfig | None = None,
) -> FastAPI:
    selected_config = config or config_from_environment()
    selected_controller = controller
    application = FastAPI(title="KubeCouncil Demo Scenario Controller")

    def required_controller() -> ScenarioController:
        nonlocal selected_controller
        if selected_controller is None:
            selected_controller = build_controller(selected_config)
        return selected_controller

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "component": "scenario-controller"}

    @application.get("/ready")
    def ready() -> dict[str, object]:
        if not selected_config.demo_mode:
            raise HTTPException(status_code=503, detail={"code": "demo_mode_disabled"})
        return {"ready": True, "demo_only": True}

    @application.post(
        "/api/demo/scenarios/{scenario}/{action}",
        response_model=ScenarioTransitionResult,
    )
    def transition(
        scenario: ScenarioName, action: ScenarioAction
    ) -> ScenarioTransitionResult:
        try:
            return required_controller().transition(scenario, action)
        except ScenarioSafetyError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "scenario_transition_refused", "message": str(error)},
            ) from error

    @application.get("/api/demo/audit", response_model=list[ScenarioAuditEvent])
    def audit() -> list[ScenarioAuditEvent]:
        return list(required_controller().audit_events())

    return application


app = create_app()
