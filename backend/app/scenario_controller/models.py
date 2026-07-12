"""Strict contracts for the isolated demo Scenario Controller."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.models import KubeCouncilModel

_KUBERNETES_NAME = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
_MEMORY_QUANTITY = re.compile(r"^[1-9][0-9]*(?:Ki|Mi|Gi|K|M|G)?$")


class ScenarioName(StrEnum):
    RECOMMENDATION_OOM = "recommendation_oom"
    REDIS_OUTAGE = "redis_outage"


class ScenarioAction(StrEnum):
    INJECT = "inject"
    RESET = "reset"


class ScenarioDeploymentState(KubeCouncilModel):
    name: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    namespace: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    resource_version: str = Field(min_length=1)
    replicas: int = Field(ge=0)
    container: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    memory_request: str = Field(min_length=1)
    memory_limit: str = Field(min_length=1)

    @model_validator(mode="after")
    def memory_is_a_kubernetes_quantity(self) -> ScenarioDeploymentState:
        if any(
            _MEMORY_QUANTITY.fullmatch(value) is None
            for value in (self.memory_request, self.memory_limit)
        ):
            raise ValueError("memory resources must be positive Kubernetes memory quantities")
        return self


class ScenarioControllerConfig(KubeCouncilModel):
    demo_mode: bool
    application_namespace: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    recommendation_deployment: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    recommendation_container: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    recommendation_safe_memory_request: str = Field(min_length=1)
    recommendation_safe_memory_limit: str = Field(min_length=1)
    recommendation_unsafe_memory_request: str = Field(min_length=1)
    recommendation_unsafe_memory_limit: str = Field(min_length=1)
    redis_deployment: str = Field(min_length=1, pattern=_KUBERNETES_NAME)
    redis_safe_replicas: int = Field(ge=1)

    @model_validator(mode="after")
    def has_distinct_bounded_targets(self) -> ScenarioControllerConfig:
        for value in (
            self.recommendation_safe_memory_request,
            self.recommendation_safe_memory_limit,
            self.recommendation_unsafe_memory_request,
            self.recommendation_unsafe_memory_limit,
        ):
            if _MEMORY_QUANTITY.fullmatch(value) is None:
                raise ValueError("scenario memory limits must be positive Kubernetes quantities")
        if self.recommendation_safe_memory_limit == self.recommendation_unsafe_memory_limit:
            raise ValueError("safe and unsafe recommendation memory limits must differ")
        if self.recommendation_unsafe_memory_request != self.recommendation_unsafe_memory_limit:
            raise ValueError("unsafe recommendation memory request and limit must match")
        if self.recommendation_deployment == self.redis_deployment:
            raise ValueError("scenario targets must be distinct")
        return self


class ScenarioAuditEvent(KubeCouncilModel):
    event_id: str = Field(min_length=1)
    recorded_at: datetime
    scenario: ScenarioName
    action: ScenarioAction
    actor: Literal["scenario-controller"] = "scenario-controller"
    outcome: Literal["applied", "noop", "refused"]
    target_namespace: str = Field(min_length=1)
    target_name: str = Field(min_length=1)
    before: ScenarioDeploymentState | None = None
    after: ScenarioDeploymentState | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class ScenarioTransitionResult(KubeCouncilModel):
    scenario: ScenarioName
    action: ScenarioAction
    changed: bool
    before: ScenarioDeploymentState
    after: ScenarioDeploymentState
    audit_event_id: str = Field(min_length=1)
