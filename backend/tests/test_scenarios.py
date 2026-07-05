from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.repositories import get_run_store
from app.api.runs import get_load_test_runner
from app.domain.fakes import FakeLoadTestRunner, InMemoryRunStore
from app.domain.models import (
    DeploymentSource,
    LoadTestFailureType,
    LoadTestResult,
    LoadTestStatus,
    RehearsalPlan,
    RehearsalState,
    RehearsalStatus,
    RepositorySnapshot,
    ValidationResult,
    ValidationStatus,
)
from app.main import app
from app.scenarios.k6 import (
    FLASH_SALE_SCENARIO,
    CommandResult,
    KubectlK6LoadTestRunner,
    LoadTestInfrastructureError,
    LoadTestOutputError,
    evaluate_objective,
    parse_k6_summary,
)

K6_SUMMARY = """
some k6 text
{
  "metrics": {
    "http_reqs": {"values": {"count": 120}},
    "http_req_failed": {"values": {"rate": 0.01}},
    "http_req_duration": {"values": {"p(95)": 150.5}}
  }
}
"""


class RecordingCommandRunner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, arguments: Sequence[str], input_text: str | None = None) -> CommandResult:
        self.calls.append((tuple(arguments), input_text))
        if not self.responses:
            raise AssertionError(f"unexpected command: {arguments}")
        return self.responses.pop(0)


def test_parse_k6_summary_returns_objective_evaluation() -> None:
    result = parse_k6_summary(K6_SUMMARY, FLASH_SALE_SCENARIO, "baseline")

    assert result.request_count == 120
    assert result.success_rate == 0.99
    assert result.p95_latency_ms == 150.5
    assert result.status == LoadTestStatus.PASSED
    assert result.failure_type is None


def test_evaluate_objective_marks_test_failure_without_infrastructure_failure() -> None:
    result = evaluate_objective(
        LoadTestResult(
            scenario_name=FLASH_SALE_SCENARIO.name,
            phase="pressure",
            request_count=100,
            success_rate=0.8,
            p95_latency_ms=2500,
        ),
        FLASH_SALE_SCENARIO.objective,
    )

    assert result.status == LoadTestStatus.FAILED
    assert result.failure_type == LoadTestFailureType.OBJECTIVE
    assert "success rate" in result.errors[0]


def test_parse_k6_summary_rejects_malformed_logs() -> None:
    with pytest.raises(LoadTestOutputError, match="JSON summary"):
        parse_k6_summary("no machine readable output", FLASH_SALE_SCENARIO, "pressure")


def test_k6_runner_applies_waits_reads_logs_and_deletes_job() -> None:
    command_runner = RecordingCommandRunner(
        (
            CommandResult(returncode=0, stdout="", stderr=""),
            CommandResult(returncode=0, stdout="", stderr=""),
            CommandResult(returncode=0, stdout=K6_SUMMARY, stderr=""),
            CommandResult(returncode=0, stdout="", stderr=""),
        )
    )
    runner = KubectlK6LoadTestRunner(command_runner=command_runner)

    result = runner.run("kc-rehearsal-run-1", FLASH_SALE_SCENARIO, "pressure")

    commands = [call[0] for call in command_runner.calls]
    assert commands[0][:4] == ("kubectl", "apply", "-n", "kc-rehearsal-run-1")
    assert commands[1][1:4] == ("wait", "-n", "kc-rehearsal-run-1")
    assert commands[2] == (
        "kubectl",
        "logs",
        "-n",
        "kc-rehearsal-run-1",
        "job/kc-k6-flash-sale-fixed-capacity-pressure",
    )
    assert commands[3][1:5] == ("delete", "-n", "kc-rehearsal-run-1", "job")
    assert command_runner.calls[0][1] is not None
    assert result.status == LoadTestStatus.PASSED


def test_k6_runner_timeout_is_infrastructure_failure_and_still_deletes_job() -> None:
    command_runner = RecordingCommandRunner(
        (
            CommandResult(returncode=0, stdout="", stderr=""),
            CommandResult(returncode=1, stdout="", stderr="timed out waiting for condition"),
            CommandResult(returncode=0, stdout="", stderr=""),
        )
    )
    runner = KubectlK6LoadTestRunner(command_runner=command_runner)

    with pytest.raises(LoadTestInfrastructureError, match="timed out"):
        runner.run("kc-rehearsal-run-1", FLASH_SALE_SCENARIO, "pressure")

    assert command_runner.calls[-1][0][1:5] == ("delete", "-n", "kc-rehearsal-run-1", "job")


def test_k6_runner_rejects_non_rehearsal_namespace() -> None:
    runner = KubectlK6LoadTestRunner(command_runner=RecordingCommandRunner(()))

    with pytest.raises(LoadTestInfrastructureError, match="namespace"):
        runner.run("production", FLASH_SALE_SCENARIO, "baseline")


def test_scenario_api_runs_and_persists_results(tmp_path: Path) -> None:
    store = InMemoryRunStore()
    store.put("run-1", "rehearsal_state", rehearsal_state(tmp_path))
    baseline = LoadTestResult(
        scenario_name=FLASH_SALE_SCENARIO.name,
        phase="baseline",
        request_count=100,
        success_rate=1,
        p95_latency_ms=100,
        status=LoadTestStatus.PASSED,
    )
    runner = FakeLoadTestRunner(baseline)

    app.dependency_overrides[get_run_store] = lambda: store
    app.dependency_overrides[get_load_test_runner] = lambda: runner
    client = TestClient(app)

    try:
        baseline_response = client.post("/api/runs/run-1/baseline")
        results_response = client.get("/api/runs/run-1/results")
    finally:
        app.dependency_overrides.clear()

    assert baseline_response.status_code == 200
    assert baseline_response.json()["status"] == "passed"
    assert results_response.status_code == 200
    assert results_response.json()["baseline"]["request_count"] == 100
    assert runner.calls == [("kc-rehearsal-run-1", FLASH_SALE_SCENARIO.name, "baseline")]


def test_scenario_api_requires_deployed_rehearsal() -> None:
    app.dependency_overrides[get_run_store] = lambda: InMemoryRunStore()
    client = TestClient(app)

    try:
        response = client.post("/api/runs/run-1/pressure")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rehearsal_not_deployed"


def rehearsal_state(tmp_path: Path) -> RehearsalState:
    snapshot = RepositorySnapshot(
        run_id="run-1",
        repository_url="https://github.com/example/repo",
        ref="main",
        commit_sha="abcdef123456",
        workspace_path=str(tmp_path),
        deployment_path="deploy",
        captured_at=datetime.now(UTC),
    )
    source = DeploymentSource(
        repository=snapshot,
        kustomization_path="deploy/kustomization.yaml",
        rendered_resource_count=0,
    )
    plan = RehearsalPlan(
        run_id="run-1",
        namespace="kc-rehearsal-run-1",
        source=source,
        services=(),
        resource_quota_cpu_millis=1000,
        resource_quota_memory_mib=1024,
    )
    return RehearsalState(
        run_id="run-1",
        namespace="kc-rehearsal-run-1",
        status=RehearsalStatus.DEPLOYED,
        plan=plan,
        readiness=ValidationResult(status=ValidationStatus.PASSED),
    )
