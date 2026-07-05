import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.council import (
    CouncilAgentClient,
    CouncilPlanValidator,
    GeminiAdkCouncilRunner,
    parse_experiment_audit,
    parse_service_proposal,
)
from app.domain.models import (
    CouncilAction,
    CouncilPlan,
    CouncilPlanStatus,
    ExperimentAudit,
    LoadTestResult,
    ResourceRequests,
    ScenarioObjective,
    ScenarioSpec,
    ServiceProfile,
    ServiceProposal,
    ValidationResult,
    ValidationStatus,
)

NAMESPACE = "kc-rehearsal-run-1"
RUN_ID = "run-1"
QUOTA = ResourceRequests(cpu_millis=3200, memory_mib=4096)


class FakeCouncilAgentClient(CouncilAgentClient):
    def __init__(
        self,
        coordinator_plan: CouncilPlan,
        repaired_plan: CouncilPlan | None = None,
    ) -> None:
        self.coordinator_plan = coordinator_plan
        self.repaired_plan = repaired_plan or coordinator_plan
        self.proposal_payloads: dict[str, Mapping[str, Any]] = {}
        self.repair_payload: Mapping[str, Any] | None = None

    def service_proposal(
        self,
        agent_name: str,
        payload: Mapping[str, Any],
    ) -> ServiceProposal:
        service_name = payload["service_profile"]["name"]
        self.proposal_payloads[agent_name] = payload
        return ServiceProposal(
            service_name=str(service_name),
            proposed_actions=(),
            rationale=f"{service_name} accepts coordinator allocation",
        )

    def coordinate(
        self,
        payload: Mapping[str, Any],
    ) -> CouncilPlan:
        return self.coordinator_plan

    def repair(
        self,
        payload: Mapping[str, Any],
    ) -> CouncilPlan:
        self.repair_payload = payload
        return self.repaired_plan

    def audit(
        self,
        payload: Mapping[str, Any],
    ) -> ExperimentAudit:
        return ExperimentAudit(
            summary="fake audit",
            severe_regressions=(),
            recommendation="approve",
        )


def test_structured_service_proposal_parsing() -> None:
    payload = {
        "service_name": "checkout",
        "proposed_actions": [
            {
                "action_type": "scale_deployment",
                "target_service": "checkout",
                "target_namespace": NAMESPACE,
                "parameters": {"replicas": 3},
                "reason": "increase checkout capacity",
            }
        ],
        "rationale": "checkout is the pressure bottleneck",
    }

    proposal = parse_service_proposal(json.dumps(payload))

    assert proposal.service_name == "checkout"
    assert proposal.proposed_actions[0].parameters == {"replicas": 3}


def test_invalid_action_type_is_rejected_during_structured_parsing() -> None:
    payload = {
        "service_name": "checkout",
        "proposed_actions": [
            {
                "action_type": "kubectl_exec",
                "target_service": "checkout",
                "target_namespace": NAMESPACE,
                "parameters": {},
                "reason": "not allowlisted",
            }
        ],
        "rationale": "unsafe",
    }

    with pytest.raises(ValidationError):
        parse_service_proposal(payload)


def test_structured_experiment_audit_parsing() -> None:
    audit = parse_experiment_audit(
        {
            "summary": "post-change metrics improved",
            "severe_regressions": [],
            "recommendation": "approve",
        }
    )

    assert audit.recommendation == "approve"


def test_validator_rejects_critical_service_suspension() -> None:
    plan = plan_with(
        action(
            "suspend_optional_deployment",
            "payment",
            {},
            "payment is expensive",
        )
    )

    validated = CouncilPlanValidator().validate(plan, NAMESPACE, services(), QUOTA)

    assert validated.status == CouncilPlanStatus.INVALID
    assert "payment is not optional" in validated.validation.errors[0]


def test_validator_rejects_unavailable_degradation_mode() -> None:
    plan = plan_with(
        action(
            "set_config_mode",
            "payment",
            {"mode": "cached"},
            "try unsupported payment cache",
        )
    )

    validated = CouncilPlanValidator().validate(plan, NAMESPACE, services(), QUOTA)

    assert validated.status == CouncilPlanStatus.INVALID
    assert "payment does not declare degradation mode cached" in validated.validation.errors


def test_validator_rejects_capacity_overflow() -> None:
    plan = plan_with(
        action(
            "scale_deployment",
            "checkout",
            {"replicas": 6},
            "increase checkout too far",
        )
    )

    validated = CouncilPlanValidator().validate(plan, NAMESPACE, services(), QUOTA)

    assert validated.status == CouncilPlanStatus.INVALID
    assert any("requested CPU" in error for error in validated.validation.errors)


def test_validator_rejects_duplicate_or_conflicting_actions() -> None:
    plan = plan_with(
        action("scale_deployment", "checkout", {"replicas": 3}, "increase checkout"),
        action("scale_deployment", "checkout", {"replicas": 4}, "increase checkout again"),
    )

    validated = CouncilPlanValidator().validate(plan, NAMESPACE, services(), QUOTA)

    assert validated.status == CouncilPlanStatus.INVALID
    assert any("duplicate or conflicting action" in error for error in validated.validation.errors)


def test_runner_repairs_invalid_coordinator_plan_once() -> None:
    invalid_plan = plan_with(
        action("scale_deployment", "checkout", {"replicas": 7}, "too many checkout pods")
    )
    repaired_plan = valid_demo_plan()
    agent_client = FakeCouncilAgentClient(invalid_plan, repaired_plan)
    runner = GeminiAdkCouncilRunner(agent_client=agent_client)

    result = runner.run(
        NAMESPACE,
        services(),
        scenario(),
        pressure_result(),
        run_id=RUN_ID,
        resource_quota=QUOTA,
    )

    assert result.status == CouncilPlanStatus.VALID
    assert result.repair_attempted is True
    assert agent_client.repair_payload is not None
    assert "validation_errors" in agent_client.repair_payload
    assert {proposal.service_name for proposal in result.representative_proposals} == {
        "checkout",
        "payment",
        "recommendation",
        "analytics-worker",
    }
    checkout_payload = agent_client.proposal_payloads["checkout_representative"]
    assert checkout_payload["service_profile"]["name"] == "checkout"
    assert {dependency["name"] for dependency in checkout_payload["dependencies"]} == {
        "payment",
        "recommendation",
    }


def test_runner_returns_infeasible_plan_without_repair() -> None:
    infeasible = CouncilPlan(
        plan_id="plan-infeasible",
        run_id=RUN_ID,
        namespace=NAMESPACE,
        actions=(),
        validation=ValidationResult(status=ValidationStatus.PASSED),
        infeasible_reason="no safe resource release can fit checkout",
    )
    agent_client = FakeCouncilAgentClient(infeasible)
    runner = GeminiAdkCouncilRunner(agent_client=agent_client)

    result = runner.run(
        NAMESPACE,
        services(),
        scenario(),
        pressure_result(),
        run_id=RUN_ID,
        resource_quota=ResourceRequests(cpu_millis=1, memory_mib=1),
    )

    assert result.status == CouncilPlanStatus.INFEASIBLE
    assert result.infeasible_reason == "no safe resource release can fit checkout"
    assert agent_client.repair_payload is None


def valid_demo_plan() -> CouncilPlan:
    return plan_with(
        action(
            "suspend_optional_deployment",
            "analytics-worker",
            {},
            "release optional analytics capacity",
        ),
        action(
            "set_config_mode",
            "recommendation",
            {"mode": "cached"},
            "use declared recommendation degradation mode",
        ),
        action(
            "set_resource_requests",
            "recommendation",
            {"cpu_millis": 150, "memory_mib": 256},
            "reduce recommendation CPU cost in cached mode",
        ),
        action(
            "scale_deployment",
            "checkout",
            {"replicas": 3},
            "increase checkout capacity",
        ),
    )


def plan_with(*actions: CouncilAction) -> CouncilPlan:
    return CouncilPlan(
        plan_id="plan-1",
        run_id=RUN_ID,
        namespace=NAMESPACE,
        actions=actions,
        validation=ValidationResult(status=ValidationStatus.PASSED),
    )


def action(
    action_type: str,
    target_service: str,
    parameters: Mapping[str, Any],
    reason: str,
) -> CouncilAction:
    return CouncilAction(
        action_type=action_type,
        target_service=target_service,
        target_namespace=NAMESPACE,
        parameters=dict(parameters),
        reason=reason,
    )


def services() -> tuple[ServiceProfile, ...]:
    return (
        service(
            "gateway",
            replicas=2,
            minimum=2,
            maximum=5,
            cpu=200,
            memory=256,
            criticality="critical",
            dependencies=("checkout",),
        ),
        service(
            "checkout",
            replicas=2,
            minimum=2,
            maximum=6,
            cpu=450,
            memory=512,
            criticality="critical",
            dependencies=("payment", "recommendation"),
            degradation_modes=("queue-admission",),
        ),
        service(
            "payment",
            replicas=2,
            minimum=2,
            maximum=4,
            cpu=250,
            memory=256,
            criticality="critical",
        ),
        service(
            "recommendation",
            replicas=2,
            minimum=1,
            maximum=4,
            cpu=300,
            memory=256,
            criticality="important",
            degradation_modes=("cached",),
        ),
        service(
            "analytics-worker",
            replicas=1,
            minimum=0,
            maximum=1,
            cpu=600,
            memory=512,
            criticality="optional",
            degradation_modes=("suspend",),
            optional=True,
        ),
    )


def service(
    name: str,
    *,
    replicas: int,
    minimum: int,
    maximum: int,
    cpu: int,
    memory: int,
    criticality: str,
    dependencies: tuple[str, ...] = (),
    degradation_modes: tuple[str, ...] = (),
    optional: bool = False,
) -> ServiceProfile:
    return ServiceProfile(
        name=name,
        image=f"{name}:latest",
        current_replicas=replicas,
        min_replicas=minimum,
        max_replicas=maximum,
        resource_requests=ResourceRequests(cpu_millis=cpu, memory_mib=memory),
        criticality=criticality,
        dependencies=dependencies,
        degradation_modes=degradation_modes,
        optional=optional,
        namespace="shop-demo",
    )


def scenario() -> ScenarioSpec:
    return ScenarioSpec(
        name="flash-sale-fixed-capacity",
        baseline_virtual_users=5,
        pressure_virtual_users=40,
        duration_seconds=45,
        objective=ScenarioObjective(success_rate_minimum=0.95, p95_latency_ms_maximum=2000),
    )


def pressure_result() -> LoadTestResult:
    return LoadTestResult(
        scenario_name="flash-sale-fixed-capacity",
        phase="pressure",
        request_count=120,
        success_rate=0.82,
        p95_latency_ms=3100,
    )
