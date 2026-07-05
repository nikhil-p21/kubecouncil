import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import yaml

from app.domain.models import (
    LoadTestFailureType,
    LoadTestResult,
    LoadTestStatus,
    ScenarioObjective,
    ScenarioSpec,
)

ScenarioPhase = Literal["baseline", "pressure", "post_change"]


FLASH_SALE_SCENARIO = ScenarioSpec(
    name="flash-sale-fixed-capacity",
    baseline_virtual_users=5,
    pressure_virtual_users=40,
    duration_seconds=45,
    objective=ScenarioObjective(success_rate_minimum=0.95, p95_latency_ms_maximum=2000),
)


class LoadTestInfrastructureError(RuntimeError):
    """Raised when the Kubernetes Job could not be created, completed or inspected."""


class LoadTestOutputError(RuntimeError):
    """Raised when k6 logs do not contain parseable machine-readable metrics."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, arguments: Sequence[str], input_text: str | None = None) -> CommandResult:
        ...


class KubectlK6LoadTestRunner:
    """Runs k6 as a Kubernetes Job inside an isolated rehearsal namespace."""

    def __init__(
        self,
        command: Sequence[str] = ("kubectl",),
        image: str = "grafana/k6:0.49.0",
        target_url: str = "http://gateway",
        wait_buffer_seconds: int = 60,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._command = tuple(command)
        self._image = image
        self._target_url = target_url
        self._wait_buffer_seconds = wait_buffer_seconds
        self._command_runner = command_runner or self._subprocess_run

    def run(self, namespace: str, scenario: ScenarioSpec, phase: str) -> LoadTestResult:
        if not namespace.startswith("kc-rehearsal-"):
            raise LoadTestInfrastructureError("namespace must begin with kc-rehearsal-")
        if phase not in {"baseline", "pressure", "post_change"}:
            raise LoadTestInfrastructureError(f"unsupported scenario phase: {phase}")

        typed_phase = cast(ScenarioPhase, phase)
        job_name = _resource_name(f"kc-k6-{scenario.name}-{typed_phase}")
        config_map_name = _resource_name(f"{job_name}-script")
        manifest = self._job_manifest(namespace, job_name, config_map_name, scenario, typed_phase)

        try:
            self._run(("apply", "-n", namespace, "-f", "-"), input_text=manifest)
            self._run(
                (
                    "wait",
                    "-n",
                    namespace,
                    "--for=condition=complete",
                    f"job/{job_name}",
                    f"--timeout={scenario.duration_seconds + self._wait_buffer_seconds}s",
                )
            )
            logs = self._run(("logs", "-n", namespace, f"job/{job_name}"))
            return parse_k6_summary(logs, scenario, typed_phase)
        finally:
            self._cleanup(namespace, job_name, config_map_name)

    def _job_manifest(
        self,
        namespace: str,
        job_name: str,
        config_map_name: str,
        scenario: ScenarioSpec,
        phase: str,
    ) -> str:
        labels = {
            "app.kubernetes.io/name": "kubecouncil-k6",
            "kubecouncil.io/rehearsal": "true",
            "kubecouncil.io/scenario": scenario.name,
            "kubecouncil.io/phase": phase,
        }
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": config_map_name, "namespace": namespace, "labels": labels},
                "data": {"flash-sale.js": _K6_SCRIPT},
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
                "spec": {
                    "backoffLimit": 0,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "k6",
                                    "image": self._image,
                                    "command": ["/bin/sh", "-c"],
                                    "args": [
                                        "k6 run --summary-export=/tmp/k6-summary.json "
                                        "/scripts/flash-sale.js && cat /tmp/k6-summary.json"
                                    ],
                                    "env": [
                                        {"name": "TARGET_URL", "value": self._target_url},
                                        {"name": "PHASE", "value": phase},
                                        {
                                            "name": "BASELINE_VUS",
                                            "value": str(scenario.baseline_virtual_users),
                                        },
                                        {
                                            "name": "PRESSURE_VUS",
                                            "value": str(scenario.pressure_virtual_users),
                                        },
                                        {
                                            "name": "DURATION",
                                            "value": f"{scenario.duration_seconds}s",
                                        },
                                    ],
                                    "volumeMounts": [
                                        {
                                            "name": "script",
                                            "mountPath": "/scripts",
                                            "readOnly": True,
                                        }
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "script", "configMap": {"name": config_map_name}}
                            ],
                        },
                    },
                },
            },
        ]
        return yaml.safe_dump_all(documents, sort_keys=False)

    def _run(self, arguments: Sequence[str], input_text: str | None = None) -> str:
        result = self._command_runner((*self._command, *arguments), input_text)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "kubectl command failed"
            raise LoadTestInfrastructureError(message)
        return result.stdout

    def _cleanup(self, namespace: str, job_name: str, config_map_name: str) -> None:
        try:
            self._run(
                (
                    "delete",
                    "-n",
                    namespace,
                    "job",
                    job_name,
                    "configmap",
                    config_map_name,
                    "--ignore-not-found=true",
                )
            )
        except LoadTestInfrastructureError:
            return

    def _subprocess_run(
        self, arguments: Sequence[str], input_text: str | None = None
    ) -> CommandResult:
        result = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def parse_k6_summary(logs: str, scenario: ScenarioSpec, phase: ScenarioPhase) -> LoadTestResult:
    document = _last_json_object(logs)
    metrics = document.get("metrics")
    if not isinstance(metrics, dict):
        raise LoadTestOutputError("k6 summary is missing metrics")

    request_count = int(_metric_value(metrics, "http_reqs", "count"))
    failure_rate = float(_metric_value(metrics, "http_req_failed", "rate"))
    p95_latency_ms = float(_metric_value(metrics, "http_req_duration", "p(95)"))
    success_rate = max(0.0, min(1.0, 1.0 - failure_rate))

    result = LoadTestResult(
        scenario_name=scenario.name,
        phase=phase,
        request_count=request_count,
        success_rate=success_rate,
        p95_latency_ms=p95_latency_ms,
    )
    return evaluate_objective(result, scenario.objective)


def evaluate_objective(result: LoadTestResult, objective: ScenarioObjective) -> LoadTestResult:
    errors: list[str] = []
    if result.success_rate < objective.success_rate_minimum:
        errors.append(
            f"success rate {result.success_rate:.3f} below minimum "
            f"{objective.success_rate_minimum:.3f}"
        )
    if result.p95_latency_ms > objective.p95_latency_ms_maximum:
        errors.append(
            f"p95 latency {result.p95_latency_ms:.1f}ms exceeds maximum "
            f"{objective.p95_latency_ms_maximum}ms"
        )

    if errors:
        return result.model_copy(
            update={
                "errors": tuple((*result.errors, *errors)),
                "status": LoadTestStatus.FAILED,
                "failure_type": LoadTestFailureType.OBJECTIVE,
            }
        )
    return result.model_copy(update={"status": LoadTestStatus.PASSED, "failure_type": None})


def _last_json_object(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    parsed: dict[str, object] | None = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "metrics" in value:
            parsed = value
    if parsed is None:
        raise LoadTestOutputError("k6 logs do not contain a JSON summary")
    return parsed


def _metric_value(metrics: dict[str, object], metric_name: str, value_name: str) -> float:
    metric = metrics.get(metric_name)
    if not isinstance(metric, dict):
        raise LoadTestOutputError(f"k6 summary is missing metric: {metric_name}")
    values = metric.get("values")
    if isinstance(values, dict) and value_name in values:
        return float(values[value_name])
    if value_name in metric:
        return float(cast(float, metric[value_name]))
    raise LoadTestOutputError(f"k6 summary is missing value: {metric_name}.{value_name}")


def _resource_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return name[:63].rstrip("-")


_K6_SCRIPT = """
import http from "k6/http";
import { check, sleep } from "k6";

const target = __ENV.TARGET_URL || "http://gateway";
const phase = __ENV.PHASE || "baseline";
const baselineVus = Number(__ENV.BASELINE_VUS || "5");
const pressureVus = Number(__ENV.PRESSURE_VUS || "40");
const duration = __ENV.DURATION || "45s";

export const options = {
  scenarios: {
    traffic: {
      executor: "constant-vus",
      vus: phase === "pressure" ? pressureVus : baselineVus,
      duration,
    },
  },
};

export default function () {
  const response = http.get(target);
  check(response, {
    "request succeeded": (res) => res.status >= 200 && res.status < 500,
    "checkout path returned": (res) => res.body.includes("checkout"),
  });
  sleep(1);
}
""".strip()
