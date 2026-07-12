"""Deterministic scenario transitions with no KubeCouncil remediation path."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Literal, NoReturn, Protocol
from uuid import uuid4

from app.scenario_controller.models import (
    ScenarioAction,
    ScenarioAuditEvent,
    ScenarioControllerConfig,
    ScenarioDeploymentState,
    ScenarioName,
    ScenarioTransitionResult,
)


class ScenarioSafetyError(RuntimeError):
    """Raised before mutation when demo scope or live state is ambiguous."""


class ScenarioProviderError(RuntimeError):
    """Raised by the narrow Kubernetes provider without exposing provider payloads."""


class ScenarioKubernetesProvider(Protocol):
    def inspect_deployment(self, name: str) -> ScenarioDeploymentState: ...

    def set_memory_resources(
        self,
        name: str,
        *,
        container: str,
        memory_request: str,
        memory_limit: str,
        expected_resource_version: str,
    ) -> ScenarioDeploymentState: ...

    def set_replicas(
        self, name: str, *, replicas: int, expected_resource_version: str
    ) -> ScenarioDeploymentState: ...


class ScenarioAuditStore(Protocol):
    def append(self, event: ScenarioAuditEvent) -> None: ...

    def list_events(self) -> tuple[ScenarioAuditEvent, ...]: ...


class InMemoryScenarioAuditStore:
    """Local fake; the deployed variant additionally emits structured Cloud Logging records."""

    def __init__(self) -> None:
        self._events: list[ScenarioAuditEvent] = []

    def append(self, event: ScenarioAuditEvent) -> None:
        self._events.append(event)

    def list_events(self) -> tuple[ScenarioAuditEvent, ...]:
        return tuple(self._events)


class LoggingScenarioAuditStore(InMemoryScenarioAuditStore):
    """Controller-namespace audit stream, deliberately separate from Investigation Records."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        super().__init__()
        self._logger = logger or logging.getLogger("kubecouncil.scenario-controller.audit")

    def append(self, event: ScenarioAuditEvent) -> None:
        super().append(event)
        self._logger.info(
            json.dumps(
                {
                    "event_type": "demo_scenario_transition",
                    **event.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )


class ScenarioController:
    """Allows only two named demo transitions against two exact Deployments."""

    def __init__(
        self,
        config: ScenarioControllerConfig,
        kubernetes: ScenarioKubernetesProvider,
        audit: ScenarioAuditStore,
    ) -> None:
        self._config = config
        self._kubernetes = kubernetes
        self._audit = audit

    def audit_events(self) -> tuple[ScenarioAuditEvent, ...]:
        return self._audit.list_events()

    def transition(
        self, scenario: ScenarioName, action: ScenarioAction
    ) -> ScenarioTransitionResult:
        target = self._target(scenario)
        if not self._config.demo_mode:
            self._refuse(scenario, action, target, None, "demo Scenario Controller is disabled")
        try:
            before = self._kubernetes.inspect_deployment(target)
        except ScenarioProviderError:
            self._refuse(
                scenario,
                action,
                target,
                None,
                "Kubernetes could not inspect the allowlisted scenario target",
            )
        try:
            after, changed = self._apply(scenario, action, before)
        except ScenarioSafetyError as error:
            self._refuse(scenario, action, target, before, str(error))
        except ScenarioProviderError:
            self._refuse(
                scenario,
                action,
                target,
                before,
                "Kubernetes rejected the allowlisted scenario transition",
            )
        event = self._event(
            scenario=scenario,
            action=action,
            outcome="applied" if changed else "noop",
            target=target,
            before=before,
            after=after,
        )
        self._audit.append(event)
        return ScenarioTransitionResult(
            scenario=scenario,
            action=action,
            changed=changed,
            before=before,
            after=after,
            audit_event_id=event.event_id,
        )

    def _apply(
        self,
        scenario: ScenarioName,
        action: ScenarioAction,
        before: ScenarioDeploymentState,
    ) -> tuple[ScenarioDeploymentState, bool]:
        if scenario is ScenarioName.RECOMMENDATION_OOM:
            return self._recommendation(action, before)
        return self._redis(action, before)

    def _recommendation(
        self, action: ScenarioAction, before: ScenarioDeploymentState
    ) -> tuple[ScenarioDeploymentState, bool]:
        safe = (
            self._config.recommendation_safe_memory_request,
            self._config.recommendation_safe_memory_limit,
        )
        unsafe = (
            self._config.recommendation_unsafe_memory_request,
            self._config.recommendation_unsafe_memory_limit,
        )
        expected = safe if action is ScenarioAction.INJECT else unsafe
        target = unsafe if action is ScenarioAction.INJECT else safe
        current = (before.memory_request, before.memory_limit)
        if current == target:
            return before, False
        if current != expected:
            raise ScenarioSafetyError(
                "unexpected memory resources on recommendationservice: "
                f"request={before.memory_request}, limit={before.memory_limit}"
            )
        after = self._kubernetes.set_memory_resources(
            self._config.recommendation_deployment,
            container=self._config.recommendation_container,
            memory_request=target[0],
            memory_limit=target[1],
            expected_resource_version=before.resource_version,
        )
        if (after.memory_request, after.memory_limit) != target:
            raise ScenarioSafetyError(
                "recommendation memory resource patch did not reach the requested state"
            )
        return after, True

    def _redis(
        self, action: ScenarioAction, before: ScenarioDeploymentState
    ) -> tuple[ScenarioDeploymentState, bool]:
        safe = self._config.redis_safe_replicas
        target = 0 if action is ScenarioAction.INJECT else safe
        expected = safe if action is ScenarioAction.INJECT else 0
        if before.replicas == target:
            return before, False
        if before.replicas != expected:
            raise ScenarioSafetyError(f"unexpected replica count on redis-cart: {before.replicas}")
        after = self._kubernetes.set_replicas(
            self._config.redis_deployment,
            replicas=target,
            expected_resource_version=before.resource_version,
        )
        if after.replicas != target:
            raise ScenarioSafetyError("redis replica patch did not reach the requested state")
        return after, True

    def _target(self, scenario: ScenarioName) -> str:
        if scenario is ScenarioName.RECOMMENDATION_OOM:
            return self._config.recommendation_deployment
        return self._config.redis_deployment

    def _refuse(
        self,
        scenario: ScenarioName,
        action: ScenarioAction,
        target: str,
        before: ScenarioDeploymentState | None,
        reason: str,
    ) -> NoReturn:
        self._audit.append(
            self._event(
                scenario=scenario,
                action=action,
                outcome="refused",
                target=target,
                before=before,
                after=None,
                reason=reason,
            )
        )
        raise ScenarioSafetyError(reason)

    def _event(
        self,
        *,
        scenario: ScenarioName,
        action: ScenarioAction,
        outcome: Literal["applied", "noop", "refused"],
        target: str,
        before: ScenarioDeploymentState | None,
        after: ScenarioDeploymentState | None,
        reason: str | None = None,
    ) -> ScenarioAuditEvent:
        return ScenarioAuditEvent(
            event_id=f"scenario-{uuid4().hex}",
            recorded_at=datetime.now(UTC),
            scenario=scenario,
            action=action,
            outcome=outcome,
            target_namespace=self._config.application_namespace,
            target_name=target,
            before=before,
            after=after,
            reason=reason,
        )
