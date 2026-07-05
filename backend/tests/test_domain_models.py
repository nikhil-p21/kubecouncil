from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.models import (
    AnalysisResult,
    CompatibilityIssue,
    CompatibilitySeverity,
    CouncilAction,
    CouncilPlan,
    DependencyEdge,
    DeploymentSource,
    ExperimentReport,
    ExperimentStatus,
    HpaBounds,
    LoadTestResult,
    ManifestResource,
    PullRequestResult,
    RehearsalPlan,
    RepositoryConnection,
    RepositorySnapshot,
    ResourceRequests,
    ScenarioObjective,
    ScenarioSpec,
    ServiceProfile,
    ServiceProposal,
    ValidationResult,
    ValidationStatus,
)


def snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        run_id="run-1",
        repository_url="https://github.com/example/repo",
        ref="main",
        commit_sha="abcdef123456",
        workspace_path="/tmp/kc/run-1",
        deployment_path="deploy/overlays/production",
        captured_at=datetime.now(UTC),
    )


def load_result(phase: str = "pressure") -> LoadTestResult:
    return LoadTestResult(
        scenario_name="flash-sale",
        phase=phase,
        request_count=100,
        success_rate=0.99,
        p95_latency_ms=125.0,
    )


def action() -> CouncilAction:
    return CouncilAction(
        action_type="scale_deployment",
        target_service="checkout",
        target_namespace="kc-rehearsal-run-1",
        parameters={"replicas": 3},
        reason="increase checkout capacity",
    )


@pytest.mark.parametrize(
    "model",
    [
        RepositoryConnection(
            repository_url="https://github.com/example/repo",
            ref="main",
            deployment_path="deploy/overlays/production",
        ),
        snapshot(),
        DeploymentSource(
            repository=snapshot(),
            kustomization_path="deploy/overlays/production/kustomization.yaml",
            rendered_resource_count=3,
            rendered_resources=(
                ManifestResource(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="checkout",
                    source="rendered.yaml#1:Deployment/checkout",
                    content={"apiVersion": "apps/v1", "kind": "Deployment"},
                ),
            ),
        ),
        CompatibilityIssue(
            severity=CompatibilitySeverity.WARNING,
            resource_kind="Deployment",
            resource_name="checkout",
            message="missing optional annotation",
            source="deployment.yaml",
        ),
        DependencyEdge(from_service="gateway", to_service="checkout"),
        ServiceProfile(
            name="checkout",
            image="checkout:latest",
            current_replicas=2,
            min_replicas=1,
            max_replicas=5,
            resource_requests=ResourceRequests(cpu_millis=200, memory_mib=256),
            criticality="critical",
            dependencies=("payment",),
            degradation_modes=("cached",),
            optional=False,
            hpa=HpaBounds(min_replicas=1, max_replicas=5),
        ),
        RehearsalPlan(
            run_id="run-1",
            namespace="kc-rehearsal-run-1",
            source=DeploymentSource(
                repository=snapshot(),
                kustomization_path="deploy/overlays/production/kustomization.yaml",
                rendered_resource_count=1,
            ),
            services=(),
            resource_quota_cpu_millis=1000,
            resource_quota_memory_mib=1024,
        ),
        AnalysisResult(
            run_id="run-1",
            source=DeploymentSource(
                repository=snapshot(),
                kustomization_path="deploy/overlays/production/kustomization.yaml",
                rendered_resource_count=1,
            ),
            services=(),
            dependency_edges=(DependencyEdge(from_service="checkout", to_service="payment"),),
        ),
        ScenarioSpec(
            name="flash-sale",
            baseline_virtual_users=5,
            pressure_virtual_users=40,
            duration_seconds=45,
            objective=ScenarioObjective(success_rate_minimum=0.95, p95_latency_ms_maximum=2000),
        ),
        load_result(),
        ServiceProposal(
            service_name="checkout",
            proposed_actions=(action(),),
            rationale="needs CPU",
        ),
        CouncilPlan(
            plan_id="plan-1",
            run_id="run-1",
            namespace="kc-rehearsal-run-1",
            actions=(action(),),
            validation=ValidationResult(status=ValidationStatus.PASSED),
        ),
        ValidationResult(status=ValidationStatus.PASSED),
        ExperimentReport(
            run_id="run-1",
            plan_id="plan-1",
            status=ExperimentStatus.SUCCESSFUL,
            baseline=load_result("baseline"),
            pressure_before=load_result("pressure"),
            pressure_after=load_result("post_change"),
            validation=ValidationResult(status=ValidationStatus.PASSED),
            applied_actions=(action(),),
            rollback_guidance="restore recorded deployment settings",
        ),
        PullRequestResult(
            run_id="run-1",
            branch_name="kubecouncil/rehearsal-run-1",
            commit_sha="abcdef123456",
            pr_url="https://github.com/example/repo/pull/1",
            draft=True,
            changed_files=("deploy/overlays/production/patch.yaml",),
        ),
    ],
)
def test_models_round_trip(model):
    encoded = model.model_dump_json()
    assert type(model).model_validate_json(encoded) == model


def test_invalid_action_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CouncilAction(
            action_type="kubectl_exec",
            target_service="checkout",
            target_namespace="kc-rehearsal-run-1",
            parameters={},
            reason="not allowed",
        )


def test_invalid_namespace_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CouncilAction(
            action_type="scale_deployment",
            target_service="checkout",
            target_namespace="production",
            parameters={"replicas": 10},
            reason="unsafe namespace",
        )


def test_pull_requests_must_be_draft() -> None:
    with pytest.raises(ValidationError):
        PullRequestResult(
            run_id="run-1",
            branch_name="kubecouncil/rehearsal-run-1",
            commit_sha="abcdef123456",
            pr_url="https://github.com/example/repo/pull/1",
            draft=False,
            changed_files=("deploy/patch.yaml",),
        )
