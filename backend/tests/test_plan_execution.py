from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from app.api.repositories import get_run_store
from app.api.runs import (
    get_council_runner,
    get_experiment_auditor,
    get_kubernetes_client,
    get_load_test_runner,
)
from app.domain.fakes import (
    FakeCouncilRunner,
    FakeExperimentAuditor,
    FakeKubernetesClient,
    FakeLoadTestRunner,
    InMemoryRunStore,
)
from app.domain.models import (
    CouncilAction,
    CouncilActionType,
    CouncilPlan,
    CouncilPlanStatus,
    DeploymentSource,
    ExperimentAudit,
    ExperimentStatus,
    LoadTestResult,
    LoadTestStatus,
    RehearsalPlan,
    RehearsalState,
    RehearsalStatus,
    RepositorySnapshot,
    ResourceRequests,
    ScenarioObjective,
    ScenarioSpec,
    ServiceProfile,
    ValidationResult,
    ValidationStatus,
)
from app.main import app
from app.rehearsal.executor import CouncilPlanExecutionError, CouncilPlanExecutor

NAMESPACE = "kc-rehearsal-run-1"
RUN_ID = "run-1"


def test_executor_applies_actions_in_order_and_reports_success() -> None:
    kubernetes = FakeKubernetesClient()
    after = load_result("post_change", success_rate=0.99, p95_latency_ms=500)
    executor = CouncilPlanExecutor(
        kubernetes,
        FakeLoadTestRunner(after),
        FakeExperimentAuditor(),
    )

    report, snapshot = executor.apply_and_verify(
        rehearsal_state(),
        valid_plan(),
        scenario(),
        load_result("baseline", success_rate=1, p95_latency_ms=100),
        load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
    )

    assert report.status == ExperimentStatus.SUCCESSFUL
    assert [action.action_type for action in kubernetes.applied_actions] == [
        "suspend_optional_deployment",
        "set_config_mode",
        "scale_deployment",
    ]
    assert snapshot.namespace == NAMESPACE
    assert {state.service_name for state in snapshot.services} == {
        "checkout",
        "recommendation",
        "analytics-worker",
    }


def test_executor_rolls_back_after_partial_action_failure() -> None:
    kubernetes = FakeKubernetesClient()
    kubernetes.fail_action_number = 2
    executor = CouncilPlanExecutor(
        kubernetes,
        FakeLoadTestRunner(load_result("post_change", success_rate=1, p95_latency_ms=400)),
        FakeExperimentAuditor(),
    )

    report, snapshot = executor.apply_and_verify(
        rehearsal_state(),
        valid_plan(),
        scenario(),
        load_result("baseline", success_rate=1, p95_latency_ms=100),
        load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
    )

    assert report.status == ExperimentStatus.UNSUCCESSFUL
    assert report.applied_actions == (valid_plan().actions[0],)
    assert "action application failed" in report.validation.errors[0]
    assert kubernetes.rollback_snapshots == [snapshot]


def test_executor_rolls_back_when_success_rate_decreases() -> None:
    kubernetes = FakeKubernetesClient()
    executor = CouncilPlanExecutor(
        kubernetes,
        FakeLoadTestRunner(load_result("post_change", success_rate=0.7, p95_latency_ms=3400)),
        FakeExperimentAuditor(),
    )

    report, snapshot = executor.apply_and_verify(
        rehearsal_state(),
        valid_plan(),
        scenario(),
        load_result("baseline", success_rate=1, p95_latency_ms=100),
        load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
    )

    assert report.status == ExperimentStatus.UNSUCCESSFUL
    assert "success rate decreased" in report.validation.errors[0]
    assert kubernetes.rollback_snapshots == [snapshot]


def test_executor_rolls_back_when_auditor_finds_severe_regression() -> None:
    kubernetes = FakeKubernetesClient()
    executor = CouncilPlanExecutor(
        kubernetes,
        FakeLoadTestRunner(load_result("post_change", success_rate=0.99, p95_latency_ms=500)),
        FakeExperimentAuditor(
            ExperimentAudit(
                summary="payment errors increased",
                severe_regressions=("payment availability regressed",),
                recommendation="reject",
            )
        ),
    )

    report, snapshot = executor.apply_and_verify(
        rehearsal_state(),
        valid_plan(),
        scenario(),
        load_result("baseline", success_rate=1, p95_latency_ms=100),
        load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
    )

    assert report.status == ExperimentStatus.UNSUCCESSFUL
    assert any("auditor severe regression" in error for error in report.validation.errors)
    assert kubernetes.rollback_snapshots == [snapshot]


def test_executor_rejects_unsuccessful_plan_before_mutation() -> None:
    kubernetes = FakeKubernetesClient()
    executor = CouncilPlanExecutor(
        kubernetes,
        FakeLoadTestRunner(load_result("post_change", success_rate=1, p95_latency_ms=500)),
        FakeExperimentAuditor(),
    )
    plan = valid_plan().model_copy(update={"status": CouncilPlanStatus.INFEASIBLE})

    with pytest.raises(CouncilPlanExecutionError, match="plan is not valid"):
        executor.apply_and_verify(
            rehearsal_state(),
            plan,
            scenario(),
            load_result("baseline", success_rate=1, p95_latency_ms=100),
            load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
        )

    assert kubernetes.applied_actions == []


def test_council_apply_verify_and_rollback_api_uses_fakes() -> None:
    store = InMemoryRunStore()
    store.put(RUN_ID, "rehearsal_state", rehearsal_state())
    store.put(
        RUN_ID,
        "baseline_result",
        load_result("baseline", success_rate=1, p95_latency_ms=100),
    )
    store.put(
        RUN_ID,
        "pressure_result",
        load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
    )
    kubernetes = FakeKubernetesClient()
    load_runner = FakeLoadTestRunner(
        load_result("post_change", success_rate=0.99, p95_latency_ms=500)
    )
    council = FakeCouncilRunner(actions=valid_plan().actions)

    app.dependency_overrides[get_run_store] = lambda: store
    app.dependency_overrides[get_kubernetes_client] = lambda: kubernetes
    app.dependency_overrides[get_load_test_runner] = lambda: load_runner
    app.dependency_overrides[get_experiment_auditor] = lambda: FakeExperimentAuditor()
    app.dependency_overrides[get_council_runner] = lambda: council
    client = TestClient(app)

    try:
        council_response = client.post(f"/api/runs/{RUN_ID}/council")
        apply_response = client.post(f"/api/runs/{RUN_ID}/plans/fake-plan/apply")
        verify_response = client.post(f"/api/runs/{RUN_ID}/verify")
        rollback_response = client.post(f"/api/runs/{RUN_ID}/rollback")
    finally:
        app.dependency_overrides.clear()

    assert council_response.status_code == 200
    assert apply_response.status_code == 200
    assert apply_response.json()["status"] == "successful"
    assert verify_response.status_code == 200
    assert rollback_response.status_code == 200
    assert rollback_response.json()["status"] == "passed"
    assert len(kubernetes.applied_actions) == 3


def valid_plan() -> CouncilPlan:
    return CouncilPlan(
        plan_id="plan-1",
        run_id=RUN_ID,
        namespace=NAMESPACE,
        actions=(
            action("suspend_optional_deployment", "analytics-worker", {}),
            action("set_config_mode", "recommendation", {"mode": "cached"}),
            action("scale_deployment", "checkout", {"replicas": 3}),
        ),
        validation=ValidationResult(status=ValidationStatus.PASSED),
    )


def action(
    action_type: CouncilActionType,
    service: str,
    parameters: dict[str, object],
) -> CouncilAction:
    return CouncilAction(
        action_type=action_type,
        target_service=service,
        target_namespace=NAMESPACE,
        parameters=parameters,
        reason=f"{action_type} {service}",
    )


def rehearsal_state() -> RehearsalState:
    snapshot = RepositorySnapshot(
        run_id=RUN_ID,
        repository_url="https://github.com/example/repo",
        ref="main",
        commit_sha="abcdef123456",
        workspace_path="/tmp/kubecouncil/run-1",
        deployment_path="deploy",
        captured_at=datetime.now(UTC),
    )
    source = DeploymentSource(
        repository=snapshot,
        kustomization_path="deploy/kustomization.yaml",
        rendered_resource_count=0,
    )
    plan = RehearsalPlan(
        run_id=RUN_ID,
        namespace=NAMESPACE,
        source=source,
        services=services(),
        resource_quota_cpu_millis=3200,
        resource_quota_memory_mib=4096,
    )
    return RehearsalState(
        run_id=RUN_ID,
        namespace=NAMESPACE,
        status=RehearsalStatus.DEPLOYED,
        plan=plan,
        readiness=ValidationResult(status=ValidationStatus.PASSED),
    )


def services() -> tuple[ServiceProfile, ...]:
    return (
        service(
            "checkout",
            replicas=2,
            minimum=2,
            maximum=6,
            cpu=450,
            criticality="critical",
            dependencies=("payment", "recommendation"),
        ),
        service(
            "recommendation",
            replicas=2,
            minimum=1,
            maximum=4,
            cpu=300,
            criticality="important",
            degradation_modes=("cached",),
            config_maps=("recommendation-config",),
        ),
        service(
            "analytics-worker",
            replicas=1,
            minimum=0,
            maximum=1,
            cpu=600,
            criticality="optional",
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
    criticality: str,
    dependencies: tuple[str, ...] = (),
    degradation_modes: tuple[str, ...] = (),
    config_maps: tuple[str, ...] = (),
    optional: bool = False,
) -> ServiceProfile:
    return ServiceProfile(
        name=name,
        image=f"{name}:latest",
        current_replicas=replicas,
        min_replicas=minimum,
        max_replicas=maximum,
        resource_requests=ResourceRequests(cpu_millis=cpu, memory_mib=256),
        criticality=criticality,
        dependencies=dependencies,
        degradation_modes=degradation_modes,
        config_maps=config_maps,
        optional=optional,
    )


def scenario() -> ScenarioSpec:
    return ScenarioSpec(
        name="flash-sale-fixed-capacity",
        baseline_virtual_users=5,
        pressure_virtual_users=40,
        duration_seconds=45,
        objective=ScenarioObjective(success_rate_minimum=0.95, p95_latency_ms_maximum=2000),
    )


def load_result(
    phase: Literal["baseline", "pressure", "post_change"],
    *,
    success_rate: float,
    p95_latency_ms: float,
) -> LoadTestResult:
    return LoadTestResult(
        scenario_name="flash-sale-fixed-capacity",
        phase=phase,
        request_count=100,
        success_rate=success_rate,
        p95_latency_ms=p95_latency_ms,
        status=LoadTestStatus.PASSED,
    )
