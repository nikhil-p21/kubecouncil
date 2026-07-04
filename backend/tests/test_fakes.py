from datetime import UTC, datetime

from app.domain.fakes import (
    FakeCouncilRunner,
    FakeKubernetesClient,
    FakeLoadTestRunner,
    FakeManifestRenderer,
    FakePullRequestProvider,
    FakeRepositoryProvider,
    InMemoryRunStore,
)
from app.domain.models import (
    CouncilAction,
    DeploymentSource,
    ExperimentReport,
    ExperimentStatus,
    LoadTestResult,
    RehearsalPlan,
    RepositoryConnection,
    RepositorySnapshot,
    ScenarioObjective,
    ScenarioSpec,
    ValidationResult,
    ValidationStatus,
)


def test_interface_fakes_smoke() -> None:
    repository = FakeRepositoryProvider()
    connection = RepositoryConnection(
        repository_url="https://github.com/example/repo",
        ref="main",
        deployment_path="deploy",
    )
    snapshot = repository.connect(connection, "run-1")

    renderer = FakeManifestRenderer()
    source = renderer.render(snapshot)
    assert source.rendered_resource_count == 0
    assert renderer.service_profiles(source) == ()

    plan = RehearsalPlan(
        run_id="run-1",
        namespace="kc-rehearsal-run-1",
        source=source,
        services=(),
        resource_quota_cpu_millis=1000,
        resource_quota_memory_mib=1024,
    )
    kubernetes = FakeKubernetesClient()
    kubernetes.create_rehearsal(plan)
    assert "kc-rehearsal-run-1" in kubernetes.created
    kubernetes.delete_rehearsal("kc-rehearsal-run-1")
    assert kubernetes.created == {}

    store = InMemoryRunStore()
    store.put("run-1", "source", source)
    assert store.get("run-1", "source") == source

    scenario = ScenarioSpec(
        name="flash-sale",
        baseline_virtual_users=5,
        pressure_virtual_users=40,
        duration_seconds=45,
        objective=ScenarioObjective(success_rate_minimum=0.95, p95_latency_ms_maximum=2000),
    )
    result = LoadTestResult(
        scenario_name="flash-sale",
        phase="pressure",
        request_count=100,
        success_rate=0.9,
        p95_latency_ms=2500,
    )
    load_runner = FakeLoadTestRunner(result)
    assert load_runner.run("kc-rehearsal-run-1", scenario, "pressure") == result

    action = CouncilAction(
        action_type="suspend_optional_deployment",
        target_service="analytics-worker",
        target_namespace="kc-rehearsal-run-1",
        reason="release capacity",
    )
    council = FakeCouncilRunner(actions=(action,))
    council_plan = council.run("kc-rehearsal-run-1", (), scenario, result)
    assert council_plan.actions == (action,)

    pull_requests = FakePullRequestProvider()
    report = ExperimentReport(
        run_id="run-1",
        plan_id=council_plan.plan_id,
        status=ExperimentStatus.SUCCESSFUL,
        baseline=LoadTestResult(
            scenario_name="flash-sale",
            phase="baseline",
            request_count=100,
            success_rate=1,
            p95_latency_ms=100,
        ),
        pressure_before=result,
        pressure_after=LoadTestResult(
            scenario_name="flash-sale",
            phase="post_change",
            request_count=100,
            success_rate=0.99,
            p95_latency_ms=500,
        ),
        validation=ValidationResult(status=ValidationStatus.PASSED),
        applied_actions=(action,),
        rollback_guidance="restore snapshot",
    )
    pr = pull_requests.open_draft_pull_request(snapshot, report, {"deploy/patch.yaml": "patch"})
    assert pr.draft is True
    assert str(pr.pr_url) == "https://github.com/example/repo/pull/1"

    repository.cleanup("run-1")
    assert "run-1" in repository.cleaned_runs


def test_repository_snapshot_can_be_built_directly_for_tests() -> None:
    snapshot = RepositorySnapshot(
        run_id="run-1",
        repository_url="https://github.com/example/repo",
        ref="main",
        commit_sha="abcdef123456",
        workspace_path="/tmp/kc/run-1",
        deployment_path="deploy",
        captured_at=datetime.now(UTC),
    )
    source = DeploymentSource(
        repository=snapshot,
        kustomization_path="deploy/kustomization.yaml",
        rendered_resource_count=1,
    )
    assert source.repository.commit_sha == "abcdef123456"
