from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Protocol

from pydantic import ValidationError

from app.domain.models import (
    CouncilAction,
    CouncilPlan,
    CouncilPlanStatus,
    ExperimentAudit,
    KubeCouncilModel,
    LoadTestResult,
    ResourceRequests,
    RestoreDeploymentParameters,
    ScaleDeploymentParameters,
    ScenarioSpec,
    ServiceProfile,
    ServiceProposal,
    SetConfigModeParameters,
    SetHpaBoundsParameters,
    SetResourceRequestsParameters,
    SuspendOptionalDeploymentParameters,
    ValidationResult,
    ValidationStatus,
)

ACTION_PARAMETER_MODELS: Mapping[str, type[KubeCouncilModel]] = {
    "scale_deployment": ScaleDeploymentParameters,
    "set_hpa_bounds": SetHpaBoundsParameters,
    "set_resource_requests": SetResourceRequestsParameters,
    "set_config_mode": SetConfigModeParameters,
    "suspend_optional_deployment": SuspendOptionalDeploymentParameters,
    "restore_deployment": RestoreDeploymentParameters,
}

REPLICA_ACTIONS = {
    "scale_deployment",
    "suspend_optional_deployment",
    "restore_deployment",
}

REPRESENTATIVE_TARGETS: Mapping[str, str] = {
    "checkout": "checkout_representative",
    "payment": "payment_representative",
    "recommendation": "recommendation_representative",
    "analytics-worker": "analytics_representative",
}

COUNCIL_COORDINATOR = "council_coordinator"
EXPERIMENT_AUDITOR = "experiment_auditor"
DEFAULT_MODEL_ENV = "KUBECOUNCIL_GEMINI_MODEL"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

class CouncilAgentError(RuntimeError):
    """Raised when a council agent cannot produce valid structured output."""


class CouncilAgentClient(Protocol):
    """Structured-output boundary for Gemini/ADK council agents."""

    def service_proposal(
        self,
        agent_name: str,
        payload: Mapping[str, Any],
    ) -> ServiceProposal:
        ...

    def coordinate(
        self,
        payload: Mapping[str, Any],
    ) -> CouncilPlan:
        ...

    def repair(
        self,
        payload: Mapping[str, Any],
    ) -> CouncilPlan:
        ...

    def audit(
        self,
        payload: Mapping[str, Any],
    ) -> ExperimentAudit:
        ...


class CouncilPlanValidator:
    """Validates council plans deterministically without invoking a model."""

    def validate(
        self,
        plan: CouncilPlan,
        namespace: str,
        services: Sequence[ServiceProfile],
        resource_quota: ResourceRequests,
    ) -> CouncilPlan:
        errors: list[str] = []
        warnings: list[str] = []
        service_by_name = {service.name: service for service in services}

        if plan.namespace != namespace:
            errors.append(f"plan namespace {plan.namespace} does not match {namespace}")
        if plan.infeasible_reason:
            if plan.actions:
                errors.append("infeasible plans must not include actions")
            return _validated_plan(
                plan,
                errors,
                warnings,
                CouncilPlanStatus.INVALID if errors else CouncilPlanStatus.INFEASIBLE,
            )

        final_replicas = {service.name: service.current_replicas for service in services}
        final_requests = {
            service.name: service.resource_requests.model_copy() for service in services
        }
        hpa_bounds = {
            service.name: {
                "min_replicas": service.hpa.min_replicas if service.hpa else service.min_replicas,
                "max_replicas": service.hpa.max_replicas if service.hpa else service.max_replicas,
            }
            for service in services
        }
        changed_fields: set[tuple[str, str]] = set()

        for action in plan.actions:
            self._validate_action(
                action,
                namespace,
                service_by_name,
                changed_fields,
                final_replicas,
                final_requests,
                hpa_bounds,
                errors,
            )

        for service in services:
            replicas = final_replicas[service.name]
            bounds = hpa_bounds[service.name]
            if replicas < service.min_replicas:
                errors.append(
                    f"{service.name} replicas {replicas} is below minimum {service.min_replicas}"
                )
            if replicas > service.max_replicas:
                errors.append(
                    f"{service.name} replicas {replicas} exceeds maximum {service.max_replicas}"
                )
            if bounds["min_replicas"] < service.min_replicas:
                errors.append(
                    f"{service.name} HPA minimum {bounds['min_replicas']} is below "
                    f"configured minimum {service.min_replicas}"
                )
            if bounds["max_replicas"] > service.max_replicas:
                errors.append(
                    f"{service.name} HPA maximum {bounds['max_replicas']} exceeds "
                    f"configured maximum {service.max_replicas}"
                )
            if bounds["max_replicas"] < bounds["min_replicas"]:
                errors.append(f"{service.name} HPA maximum must cover minimum")
            if service.criticality == "critical" and replicas < service.min_replicas:
                errors.append(f"critical service {service.name} may not be reduced below minimum")

        for service in services:
            if service.criticality != "critical":
                continue
            for dependency in service.dependencies:
                dependency_profile = service_by_name.get(dependency)
                if dependency_profile is None:
                    continue
                if final_replicas[dependency] < dependency_profile.min_replicas:
                    errors.append(
                        f"critical service {service.name} requires dependency {dependency} "
                        "to remain available"
                    )

        requested_cpu = sum(
            final_replicas[name] * requests.cpu_millis
            for name, requests in final_requests.items()
        )
        requested_memory = sum(
            final_replicas[name] * requests.memory_mib
            for name, requests in final_requests.items()
        )
        if requested_cpu > resource_quota.cpu_millis:
            errors.append(
                f"requested CPU {requested_cpu}m exceeds quota {resource_quota.cpu_millis}m"
            )
        if requested_memory > resource_quota.memory_mib:
            errors.append(
                f"requested memory {requested_memory}Mi exceeds quota "
                f"{resource_quota.memory_mib}Mi"
            )

        status = CouncilPlanStatus.INVALID if errors else CouncilPlanStatus.VALID
        return _validated_plan(plan, errors, warnings, status)

    def _validate_action(
        self,
        action: CouncilAction,
        namespace: str,
        services: Mapping[str, ServiceProfile],
        changed_fields: set[tuple[str, str]],
        final_replicas: dict[str, int],
        final_requests: dict[str, ResourceRequests],
        hpa_bounds: dict[str, dict[str, int]],
        errors: list[str],
    ) -> None:
        if action.action_type not in ACTION_PARAMETER_MODELS:
            errors.append(f"action type {action.action_type} is not allowlisted")
            return
        if action.target_namespace != namespace:
            errors.append(f"{action.target_service} targets namespace {action.target_namespace}")
        service = services.get(action.target_service)
        if service is None:
            errors.append(f"target service {action.target_service} does not exist")
            return

        parameter_model = ACTION_PARAMETER_MODELS[action.action_type]
        try:
            parameters = parameter_model.model_validate(action.parameters)
        except ValidationError as exc:
            errors.append(f"{action.target_service} {action.action_type} parameters invalid: {exc}")
            return

        field = _action_field(action.action_type)
        key = (action.target_service, field)
        if key in changed_fields:
            errors.append(f"duplicate or conflicting action for {action.target_service} {field}")
        changed_fields.add(key)

        if action.action_type == "scale_deployment":
            scale = _cast_parameters(parameters, ScaleDeploymentParameters)
            final_replicas[action.target_service] = scale.replicas
        elif action.action_type == "set_hpa_bounds":
            bounds = _cast_parameters(parameters, SetHpaBoundsParameters)
            hpa_bounds[action.target_service] = {
                "min_replicas": bounds.min_replicas,
                "max_replicas": bounds.max_replicas,
            }
        elif action.action_type == "set_resource_requests":
            requests = _cast_parameters(parameters, SetResourceRequestsParameters)
            final_requests[action.target_service] = ResourceRequests(
                cpu_millis=requests.cpu_millis,
                memory_mib=requests.memory_mib,
            )
        elif action.action_type == "set_config_mode":
            mode = _cast_parameters(parameters, SetConfigModeParameters).mode
            if mode not in service.degradation_modes:
                errors.append(
                    f"{service.name} does not declare degradation mode {mode}"
                )
        elif action.action_type == "suspend_optional_deployment":
            if not service.optional or service.criticality != "optional":
                errors.append(f"{service.name} is not optional and cannot be suspended")
            final_replicas[action.target_service] = 0
        elif action.action_type == "restore_deployment":
            final_replicas[action.target_service] = max(
                service.current_replicas,
                service.min_replicas,
            )


class GeminiAdkCouncilRunner:
    """Runs the service representatives and coordinator behind the CouncilRunner interface."""

    def __init__(
        self,
        agent_client: CouncilAgentClient | None = None,
        validator: CouncilPlanValidator | None = None,
    ) -> None:
        self._agent_client = agent_client or AdkGeminiCouncilAgentClient()
        self._validator = validator or CouncilPlanValidator()

    def run(
        self,
        namespace: str,
        services: Sequence[ServiceProfile],
        scenario: ScenarioSpec,
        pressure_result: LoadTestResult,
        *,
        run_id: str = "unknown-run",
        resource_quota: ResourceRequests | None = None,
    ) -> CouncilPlan:
        quota = resource_quota or _default_resource_quota(services)
        proposals = self._representative_proposals(
            namespace,
            services,
            scenario,
            pressure_result,
            quota,
        )
        payload = {
            "run_id": run_id,
            "namespace": namespace,
            "service_profiles": _jsonable(services),
            "scenario": _jsonable(scenario),
            "pressure_result": _jsonable(pressure_result),
            "resource_quota": _jsonable(quota),
            "allowlisted_actions": allowlisted_action_definitions(),
            "representative_proposals": _jsonable(proposals),
        }
        plan = self._agent_client.coordinate(payload)
        plan = plan.model_copy(update={"representative_proposals": proposals})
        validated = self._validator.validate(plan, namespace, services, quota)
        if validated.status in {CouncilPlanStatus.VALID, CouncilPlanStatus.INFEASIBLE}:
            return validated

        repair_payload = {
            **payload,
            "invalid_plan": _jsonable(validated),
            "validation_errors": list(validated.validation.errors),
        }
        repaired = self._agent_client.repair(repair_payload).model_copy(
            update={"representative_proposals": proposals, "repair_attempted": True}
        )
        return self._validator.validate(repaired, namespace, services, quota)

    def _representative_proposals(
        self,
        namespace: str,
        services: Sequence[ServiceProfile],
        scenario: ScenarioSpec,
        pressure_result: LoadTestResult,
        quota: ResourceRequests,
    ) -> tuple[ServiceProposal, ...]:
        service_by_name = {service.name: service for service in services}
        selected_targets = [
            target for target in REPRESENTATIVE_TARGETS if target in service_by_name
        ]
        proposals: dict[str, ServiceProposal] = {}
        with ThreadPoolExecutor(max_workers=max(len(selected_targets), 1)) as executor:
            futures = {
                executor.submit(
                    self._agent_client.service_proposal,
                    REPRESENTATIVE_TARGETS[target],
                    _representative_payload(
                        namespace,
                        service_by_name[target],
                        service_by_name,
                        scenario,
                        pressure_result,
                        quota,
                    ),
                ): target
                for target in selected_targets
            }
            for future in as_completed(futures):
                target = futures[future]
                proposal = future.result()
                if proposal.service_name != target:
                    raise CouncilAgentError(
                        f"{REPRESENTATIVE_TARGETS[target]} returned proposal for "
                        f"{proposal.service_name}"
                    )
                proposals[target] = proposal
        return tuple(proposals[target] for target in selected_targets)


class AdkGeminiCouncilAgentClient:
    """Runs Gemini-backed ADK agents with Pydantic output schemas."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.getenv(DEFAULT_MODEL_ENV, DEFAULT_GEMINI_MODEL)

    def service_proposal(
        self,
        agent_name: str,
        payload: Mapping[str, Any],
    ) -> ServiceProposal:
        return self._run_structured_agent(
            agent_name,
            _representative_instruction(agent_name),
            payload,
            ServiceProposal,
        )

    def coordinate(
        self,
        payload: Mapping[str, Any],
    ) -> CouncilPlan:
        return self._run_structured_agent(
            COUNCIL_COORDINATOR,
            _coordinator_instruction(),
            payload,
            CouncilPlan,
        )

    def repair(
        self,
        payload: Mapping[str, Any],
    ) -> CouncilPlan:
        return self._run_structured_agent(
            COUNCIL_COORDINATOR,
            _repair_instruction(),
            payload,
            CouncilPlan,
        )

    def audit(
        self,
        payload: Mapping[str, Any],
    ) -> ExperimentAudit:
        return self._run_structured_agent(
            EXPERIMENT_AUDITOR,
            _auditor_instruction(),
            payload,
            ExperimentAudit,
        )

    def _run_structured_agent[ParsedModel: KubeCouncilModel](
        self,
        agent_name: str,
        instruction: str,
        payload: Mapping[str, Any],
        output_model: type[ParsedModel],
    ) -> ParsedModel:
        try:
            return asyncio.run(
                self._run_structured_agent_async(agent_name, instruction, payload, output_model)
            )
        except ValidationError as exc:
            raise CouncilAgentError(f"{agent_name} returned invalid structured output") from exc

    async def _run_structured_agent_async[ParsedModel: KubeCouncilModel](
        self,
        agent_name: str,
        instruction: str,
        payload: Mapping[str, Any],
        output_model: type[ParsedModel],
    ) -> ParsedModel:
        agents_module = importlib.import_module("google.adk.agents")
        runners_module = importlib.import_module("google.adk.runners")
        sessions_module = importlib.import_module("google.adk.sessions")
        genai_types = importlib.import_module("google.genai.types")

        llm_agent = agents_module.LlmAgent(
            name=agent_name,
            model=self._model_name,
            description=f"KubeCouncil {agent_name}",
            instruction=instruction,
            output_schema=output_model,
        )
        app_name = "kubecouncil"
        user_id = "kubecouncil"
        session_id = f"{agent_name}-session"
        session_service = sessions_module.InMemorySessionService()
        await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        runner = runners_module.Runner(
            agent=llm_agent,
            app_name=app_name,
            session_service=session_service,
        )
        content = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=json.dumps(payload, sort_keys=True))],
        )
        final_text: str | None = None
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if getattr(event, "is_final_response", lambda: False)():
                response = getattr(event, "content", None)
                parts = getattr(response, "parts", ()) if response is not None else ()
                final_text = "".join(str(getattr(part, "text", "")) for part in parts)
        if not final_text:
            raise CouncilAgentError(f"{agent_name} did not return a final response")
        return output_model.model_validate_json(final_text)


def parse_service_proposal(payload: str | bytes | Mapping[str, Any]) -> ServiceProposal:
    return _parse_model(payload, ServiceProposal)


def parse_council_plan(payload: str | bytes | Mapping[str, Any]) -> CouncilPlan:
    return _parse_model(payload, CouncilPlan)


def parse_experiment_audit(payload: str | bytes | Mapping[str, Any]) -> ExperimentAudit:
    return _parse_model(payload, ExperimentAudit)


def allowlisted_action_definitions() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "action_type": action_type,
            "parameters_schema": parameter_model.model_json_schema(),
        }
        for action_type, parameter_model in ACTION_PARAMETER_MODELS.items()
    )


def _parse_model[ParsedModel: KubeCouncilModel](
    payload: str | bytes | Mapping[str, Any],
    model: type[ParsedModel],
) -> ParsedModel:
    if isinstance(payload, str | bytes):
        return model.model_validate_json(payload)
    return model.model_validate(payload)


def _representative_payload(
    namespace: str,
    service: ServiceProfile,
    services: Mapping[str, ServiceProfile],
    scenario: ScenarioSpec,
    pressure_result: LoadTestResult,
    quota: ResourceRequests,
) -> Mapping[str, Any]:
    dependencies = [services[name] for name in service.dependencies if name in services]
    dependents = [
        candidate
        for candidate in services.values()
        if service.name in candidate.dependencies
    ]
    return {
        "namespace": namespace,
        "service_profile": _jsonable(service),
        "dependencies": _jsonable(dependencies),
        "dependents": _jsonable(dependents),
        "scenario_objective": _jsonable(scenario.objective),
        "pressure_result": _jsonable(pressure_result),
        "resource_quota": _jsonable(quota),
        "allowlisted_actions": allowlisted_action_definitions(),
    }


def _representative_instruction(agent_name: str) -> str:
    return (
        f"You are the KubeCouncil {agent_name}. Return only a ServiceProposal JSON object. "
        "Use only allowlisted actions and target only the provided service."
    )


def _coordinator_instruction() -> str:
    return (
        "You are the KubeCouncil coordinator. Combine service proposals into one "
        "CouncilPlan JSON object. Return INFEASIBLE by setting infeasible_reason and "
        "leaving actions empty when no safe allocation exists."
    )


def _repair_instruction() -> str:
    return (
        "You are repairing a rejected CouncilPlan. Use the validation_errors to return "
        "one corrected CouncilPlan JSON object, or return an infeasible plan."
    )


def _auditor_instruction() -> str:
    return (
        "You are the KubeCouncil experiment auditor. Return only an ExperimentAudit JSON "
        "object. Flag severe regressions and recommend approve, reject, or inconclusive."
    )


def _validated_plan(
    plan: CouncilPlan,
    errors: Sequence[str],
    warnings: Sequence[str],
    status: CouncilPlanStatus,
) -> CouncilPlan:
    validation_status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    return plan.model_copy(
        update={
            "validation": ValidationResult(
                status=validation_status,
                errors=tuple(errors),
                warnings=tuple(warnings),
            ),
            "status": status,
        }
    )


def _action_field(action_type: str) -> str:
    if action_type in REPLICA_ACTIONS:
        return "replicas"
    if action_type == "set_resource_requests":
        return "resource_requests"
    if action_type == "set_config_mode":
        return "config_mode"
    if action_type == "set_hpa_bounds":
        return "hpa_bounds"
    return action_type


def _cast_parameters[ParsedModel: KubeCouncilModel](
    model: KubeCouncilModel,
    target: type[ParsedModel],
) -> ParsedModel:
    if isinstance(model, target):
        return model
    return target.model_validate(model.model_dump())


def _jsonable(value: Any) -> Any:
    if isinstance(value, KubeCouncilModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_jsonable(item) for item in value]
    return value


def _default_resource_quota(services: Sequence[ServiceProfile]) -> ResourceRequests:
    requested_cpu = sum(
        service.current_replicas * service.resource_requests.cpu_millis for service in services
    )
    requested_memory = sum(
        service.current_replicas * service.resource_requests.memory_mib for service in services
    )
    return ResourceRequests(
        cpu_millis=max(requested_cpu + 500, 1000),
        memory_mib=max(requested_memory + 512, 1024),
    )
