from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from fastapi.testclient import TestClient

from app.api.repositories import get_run_store
from app.api.runs import (
    get_manifest_renderer,
    get_pull_request_provider,
    get_repository_change_planner,
)
from app.domain.fakes import InMemoryRunStore
from app.domain.interfaces import ManifestRenderer
from app.domain.models import (
    CouncilAction,
    DeploymentSource,
    ExperimentReport,
    ExperimentStatus,
    LoadTestResult,
    PullRequestResult,
    RehearsalPlan,
    RehearsalState,
    RehearsalStatus,
    RepositorySnapshot,
    ResourceRequests,
    ServiceProfile,
    ValidationResult,
    ValidationStatus,
)
from app.main import app
from app.pull_requests.github import GitPullRequestProvider, build_pull_request_body
from app.pull_requests.planner import RepositoryChangePlanner, changed_file_contents

RUN_ID = "run-1"
NAMESPACE = "kc-rehearsal-run-1"


def test_change_planner_writes_allowlisted_kustomize_patches(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    snapshot = snapshot_for(workspace)
    report = successful_report()
    planner = RepositoryChangePlanner()

    change_set = planner.plan(snapshot, report, services(), StaticRenderer())

    paths = {change.path for change in change_set.changes}
    assert paths == {
        "deploy/overlays/production/kubecouncil-patches/analytics-worker-deployment.yaml",
        "deploy/overlays/production/kubecouncil-patches/checkout-deployment.yaml",
        "deploy/overlays/production/kubecouncil-patches/recommendation-config-mode.yaml",
        "deploy/overlays/production/kustomization.yaml",
    }
    checkout = yaml.safe_load(
        (
            workspace
            / "deploy/overlays/production/kubecouncil-patches/checkout-deployment.yaml"
        ).read_text()
    )
    assert checkout["spec"]["replicas"] == 3
    recommendation = yaml.safe_load(
        (
            workspace
            / "deploy/overlays/production/kubecouncil-patches/recommendation-config-mode.yaml"
        ).read_text()
    )
    assert recommendation["data"]["MODE"] == "cached"
    kustomization = yaml.safe_load(
        (workspace / "deploy/overlays/production/kustomization.yaml").read_text()
    )
    assert {"path": "kubecouncil-patches/checkout-deployment.yaml"} in kustomization["patches"]


def test_change_planner_rejects_unsuccessful_experiment(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    snapshot = snapshot_for(workspace)
    report = successful_report().model_copy(update={"status": ExperimentStatus.UNSUCCESSFUL})

    try:
        RepositoryChangePlanner().plan(snapshot, report, services(), StaticRenderer())
    except Exception as exc:
        assert "only successful experiments" in str(exc)
    else:
        raise AssertionError("planner accepted unsuccessful experiment")


def test_git_provider_creates_local_branch_and_commit(tmp_path: Path) -> None:
    workspace = create_git_workspace(tmp_path)
    snapshot = snapshot_for(workspace)
    report = successful_report()
    planner = RepositoryChangePlanner()
    change_set = planner.plan(snapshot, report, services(), StaticRenderer())
    changed_files = changed_file_contents(snapshot, change_set)

    result = GitPullRequestProvider(push=False).open_draft_pull_request(
        snapshot,
        report,
        changed_files,
    )

    assert result.branch_name == "kubecouncil/rehearsal-run-1"
    assert result.draft is True
    assert set(result.changed_files) == {path.as_posix() for path in changed_files}
    assert run_git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == result.branch_name
    assert run_git(workspace, "log", "-1", "--pretty=%s") == (
        "chore: apply kubecouncil rehearsal run-1"
    )


def test_pull_request_body_includes_required_evidence(tmp_path: Path) -> None:
    snapshot = snapshot_for(create_workspace(tmp_path))
    body = build_pull_request_body(
        snapshot,
        successful_report(),
        {Path("deploy/overlays/production/kustomization.yaml"): "patch"},
    )

    assert "requires human review" in body
    assert "Source commit: `abcdef123456`" in body
    assert "Rehearsal namespace: `kc-rehearsal-run-1`" in body
    assert "Pressure after" in body
    assert "rollback snapshot" in body


def test_pull_request_api_is_idempotent_and_uses_fakes(tmp_path: Path) -> None:
    workspace = create_git_workspace(tmp_path)
    store = InMemoryRunStore()
    snapshot = snapshot_for(workspace)
    report = successful_report()
    store.put(RUN_ID, "repository_snapshot", snapshot)
    store.put(RUN_ID, "rehearsal_state", rehearsal_state(snapshot))
    store.put(RUN_ID, "experiment_report", report)
    provider = RecordingPullRequestProvider()

    app.dependency_overrides[get_run_store] = lambda: store
    app.dependency_overrides[get_manifest_renderer] = lambda: StaticRenderer()
    app.dependency_overrides[get_repository_change_planner] = lambda: RepositoryChangePlanner()
    app.dependency_overrides[get_pull_request_provider] = lambda: provider
    client = TestClient(app)

    try:
        first = client.post(f"/api/runs/{RUN_ID}/pull-request")
        second = client.post(f"/api/runs/{RUN_ID}/pull-request")
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert provider.calls == 1


def test_pull_request_api_rejects_failed_experiment(tmp_path: Path) -> None:
    store = InMemoryRunStore()
    snapshot = snapshot_for(create_workspace(tmp_path))
    store.put(RUN_ID, "repository_snapshot", snapshot)
    store.put(RUN_ID, "rehearsal_state", rehearsal_state(snapshot))
    store.put(
        RUN_ID,
        "experiment_report",
        successful_report().model_copy(update={"status": ExperimentStatus.INCONCLUSIVE}),
    )

    app.dependency_overrides[get_run_store] = lambda: store
    client = TestClient(app)

    try:
        response = client.post(f"/api/runs/{RUN_ID}/pull-request")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "experiment_not_successful"


class StaticRenderer(ManifestRenderer):
    def render(self, snapshot: RepositorySnapshot) -> DeploymentSource:
        return DeploymentSource(
            repository=snapshot,
            kustomization_path=f"{snapshot.deployment_path}/kustomization.yaml",
            rendered_resource_count=3,
        )

    def service_profiles(self, source: DeploymentSource) -> tuple[ServiceProfile, ...]:
        return services()


class RecordingPullRequestProvider:
    def __init__(self) -> None:
        self.calls = 0

    def open_draft_pull_request(
        self,
        snapshot: RepositorySnapshot,
        report: ExperimentReport,
        changed_files: dict[Path, str],
    ) -> PullRequestResult:
        self.calls += 1
        return PullRequestResult(
            run_id=report.run_id,
            branch_name=f"kubecouncil/rehearsal-{report.run_id}",
            commit_sha="1234567890abcdef",
            pr_url="https://github.com/example/repo/pull/99",
            draft=True,
            changed_files=tuple(path.as_posix() for path in changed_files),
        )


def create_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    deployment = workspace / "deploy" / "overlays" / "production"
    deployment.mkdir(parents=True)
    (deployment / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - ../../base\n"
    )
    return workspace


def create_git_workspace(tmp_path: Path) -> Path:
    workspace = create_workspace(tmp_path)
    run_git(workspace, "init", "--initial-branch", "main")
    run_git(workspace, "add", ".")
    run_git(
        workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
    )
    return workspace


def run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def snapshot_for(workspace: Path) -> RepositorySnapshot:
    return RepositorySnapshot(
        run_id=RUN_ID,
        repository_url=workspace.as_uri(),
        ref="main",
        commit_sha="abcdef123456",
        workspace_path=str(workspace),
        deployment_path="deploy/overlays/production",
        captured_at=datetime.now(UTC),
    )


def successful_report() -> ExperimentReport:
    return ExperimentReport(
        run_id=RUN_ID,
        plan_id="plan-1",
        status=ExperimentStatus.SUCCESSFUL,
        baseline=load_result("baseline", success_rate=1, p95_latency_ms=100),
        pressure_before=load_result("pressure", success_rate=0.82, p95_latency_ms=3100),
        pressure_after=load_result("post_change", success_rate=0.99, p95_latency_ms=500),
        validation=ValidationResult(status=ValidationStatus.PASSED),
        applied_actions=(
            action("suspend_optional_deployment", "analytics-worker", {}),
            action("set_config_mode", "recommendation", {"mode": "cached"}),
            action("scale_deployment", "checkout", {"replicas": 3}),
        ),
        rollback_guidance="restore the recorded rollback snapshot",
    )


def rehearsal_state(snapshot: RepositorySnapshot) -> RehearsalState:
    source = DeploymentSource(
        repository=snapshot,
        kustomization_path=f"{snapshot.deployment_path}/kustomization.yaml",
        rendered_resource_count=3,
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


def action(action_type: str, service: str, parameters: dict[str, object]) -> CouncilAction:
    return CouncilAction(
        action_type=action_type,
        target_service=service,
        target_namespace=NAMESPACE,
        parameters=parameters,
        reason=f"{action_type} {service}",
    )


def services() -> tuple[ServiceProfile, ...]:
    return (
        service("checkout", replicas=2, minimum=2, maximum=6, cpu=450, criticality="critical"),
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
        degradation_modes=degradation_modes,
        config_maps=config_maps,
        optional=optional,
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
    )
