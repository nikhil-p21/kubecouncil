import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.domain.models import (
    CouncilAction,
    CouncilWorkloadSnapshot,
    HpaBounds,
    RehearsalPlan,
    RehearsalResource,
    ResourceRequests,
    ServiceProfile,
    ServiceRuntimeState,
    ValidationResult,
    ValidationStatus,
)


class KubernetesOperationError(RuntimeError):
    """Raised when a Kubernetes operation fails for a rehearsal namespace."""


class KubectlKubernetesClient:
    """Runs guarded Kubernetes operations for a generated rehearsal overlay."""

    def __init__(self, command: Sequence[str] = ("kubectl",)) -> None:
        self._command = tuple(command)

    def create_rehearsal(self, plan: RehearsalPlan) -> tuple[RehearsalResource, ...]:
        validation = self.validate_rehearsal(plan)
        if validation.status == ValidationStatus.FAILED:
            raise KubernetesOperationError("; ".join(validation.errors))

        manifest_path = _manifest_path(plan)
        self._run(("create", "namespace", plan.namespace, "--dry-run=client", "-o", "yaml"))
        self._run(("apply", "--server-side", "--dry-run=server", "-f", str(manifest_path)))
        self._run(("apply", "-f", str(manifest_path)))
        for resource in plan.rendered_resources:
            if resource.kind == "Deployment":
                self._run(
                    (
                        "rollout",
                        "status",
                        f"deployment/{resource.name}",
                        "-n",
                        plan.namespace,
                        "--timeout=120s",
                    )
                )
        self._ensure_pods_ready(plan.namespace)
        return tuple(
            RehearsalResource(
                api_version=resource.api_version,
                kind=resource.kind,
                name=resource.name,
                namespace=plan.namespace,
            )
            for resource in plan.rendered_resources
        )

    def validate_rehearsal(self, plan: RehearsalPlan) -> ValidationResult:
        errors: list[str] = []
        if not plan.namespace.startswith("kc-rehearsal-"):
            errors.append("namespace must begin with kc-rehearsal-")
        if plan.overlay_path is None:
            errors.append("rehearsal plan is missing an overlay path")
        else:
            manifest_path = Path(plan.overlay_path) / "resources.yaml"
            if not manifest_path.exists():
                errors.append(f"rendered rehearsal manifest does not exist: {manifest_path}")
        for resource in plan.rendered_resources:
            if resource.kind in {"ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition"}:
                errors.append(
                    f"cluster-scoped resource is not allowed: {resource.kind}/{resource.name}"
                )
            if resource.kind == "Secret":
                errors.append(f"production Secret is not allowed: Secret/{resource.name}")
            if resource.namespace != plan.namespace:
                errors.append(
                    f"resource namespace must be {plan.namespace}: {resource.kind}/{resource.name}"
                )
        if errors:
            return ValidationResult(status=ValidationStatus.FAILED, errors=tuple(errors))
        return ValidationResult(status=ValidationStatus.PASSED)

    def delete_rehearsal(self, namespace: str) -> None:
        if not namespace.startswith("kc-rehearsal-"):
            raise KubernetesOperationError("namespace must begin with kc-rehearsal-")
        self._run(("delete", "namespace", namespace, "--ignore-not-found=true"))

    def snapshot_workloads(
        self,
        namespace: str,
        services: Sequence[ServiceProfile],
    ) -> CouncilWorkloadSnapshot:
        _require_rehearsal_namespace(namespace)
        states: list[ServiceRuntimeState] = []
        for service in services:
            deployment = self._json(
                ("get", "deployment", service.name, "-n", namespace, "-o", "json")
            )
            hpa = self._optional_json(
                ("get", "hpa", service.name, "-n", namespace, "-o", "json")
            )
            states.append(
                ServiceRuntimeState(
                    service_name=service.name,
                    replicas=_deployment_replicas(deployment),
                    hpa=_hpa_bounds(hpa),
                    resource_requests=_deployment_requests(deployment, service),
                    config_values=self._snapshot_config_values(namespace, service),
                )
            )
        return CouncilWorkloadSnapshot(namespace=namespace, services=tuple(states))

    def apply_council_action(self, action: CouncilAction) -> None:
        _require_rehearsal_namespace(action.target_namespace)
        if action.action_type == "scale_deployment":
            replicas = int(action.parameters["replicas"])
            self._scale(action.target_namespace, action.target_service, replicas)
        elif action.action_type == "set_hpa_bounds":
            hpa_patch: dict[str, Any] = {
                "spec": {
                    "minReplicas": int(action.parameters["min_replicas"]),
                    "maxReplicas": int(action.parameters["max_replicas"]),
                }
            }
            self._patch(action.target_namespace, "hpa", action.target_service, hpa_patch)
        elif action.action_type == "set_resource_requests":
            requests = ResourceRequests(
                cpu_millis=int(action.parameters["cpu_millis"]),
                memory_mib=int(action.parameters["memory_mib"]),
            )
            self._patch_deployment_requests(
                action.target_namespace,
                action.target_service,
                requests,
            )
            self._rollout(action.target_namespace, action.target_service)
        elif action.action_type == "set_config_mode":
            config_patch: dict[str, Any] = {"data": {"MODE": str(action.parameters["mode"])}}
            self._patch(
                action.target_namespace,
                "configmap",
                f"{action.target_service}-config",
                config_patch,
            )
            self._run(
                (
                    "rollout",
                    "restart",
                    f"deployment/{action.target_service}",
                    "-n",
                    action.target_namespace,
                )
            )
            self._rollout(action.target_namespace, action.target_service)
        elif action.action_type == "suspend_optional_deployment":
            self._scale(action.target_namespace, action.target_service, 0)
        elif action.action_type == "restore_deployment":
            self._rollout(action.target_namespace, action.target_service)
        else:
            raise KubernetesOperationError(f"unsupported council action: {action.action_type}")

    def rollback_workloads(self, snapshot: CouncilWorkloadSnapshot) -> None:
        _require_rehearsal_namespace(snapshot.namespace)
        for state in snapshot.services:
            self._scale(snapshot.namespace, state.service_name, state.replicas)
            if state.hpa is not None:
                self._patch(
                    snapshot.namespace,
                    "hpa",
                    state.service_name,
                    {
                        "spec": {
                            "minReplicas": state.hpa.min_replicas,
                            "maxReplicas": state.hpa.max_replicas,
                        }
                    },
                )
            self._patch_deployment_requests(
                snapshot.namespace,
                state.service_name,
                state.resource_requests,
            )
            self._restore_config_values(snapshot.namespace, state)
            self._rollout(snapshot.namespace, state.service_name)

    def _ensure_pods_ready(self, namespace: str) -> None:
        output = self._run(
            (
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                "kubecouncil.io/rehearsal=true",
                "-o",
                "json",
            )
        )
        document = json.loads(output) if output.strip() else {"items": []}
        for pod in document.get("items", []):
            if not _pod_is_ready(pod):
                name = pod.get("metadata", {}).get("name", "unknown")
                raise KubernetesOperationError(f"pod is not ready: {name}")

    def _run(self, arguments: Sequence[str]) -> str:
        result = subprocess.run(
            [*self._command, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "kubectl command failed"
            raise KubernetesOperationError(stderr)
        return result.stdout

    def _json(self, arguments: Sequence[str]) -> dict[str, Any]:
        output = self._run(arguments)
        document = json.loads(output) if output.strip() else {}
        if not isinstance(document, dict):
            raise KubernetesOperationError("kubectl returned non-object JSON")
        return document

    def _optional_json(self, arguments: Sequence[str]) -> dict[str, Any] | None:
        try:
            return self._json(arguments)
        except KubernetesOperationError as exc:
            if "NotFound" in str(exc) or "not found" in str(exc):
                return None
            raise

    def _snapshot_config_values(
        self,
        namespace: str,
        service: ServiceProfile,
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for configmap in service.config_maps:
            document = self._optional_json(
                ("get", "configmap", configmap, "-n", namespace, "-o", "json")
            )
            if document is None:
                continue
            data = document.get("data", {})
            if isinstance(data, dict):
                for key, value in data.items():
                    values[f"{configmap}.{key}"] = str(value)
        return values

    def _restore_config_values(self, namespace: str, state: ServiceRuntimeState) -> None:
        grouped: dict[str, dict[str, str]] = {}
        for key, value in state.config_values.items():
            configmap, _, data_key = key.partition(".")
            if configmap and data_key:
                grouped.setdefault(configmap, {})[data_key] = value
        for configmap, data in grouped.items():
            self._patch(namespace, "configmap", configmap, {"data": data})

    def _patch(
        self,
        namespace: str,
        resource_kind: str,
        name: str,
        patch: dict[str, Any],
    ) -> None:
        self._run(
            (
                "patch",
                resource_kind,
                name,
                "-n",
                namespace,
                "--type",
                "merge",
                "-p",
                json.dumps(patch, sort_keys=True),
            )
        )

    def _patch_deployment_requests(
        self,
        namespace: str,
        service_name: str,
        requests: ResourceRequests,
    ) -> None:
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {
                                    "requests": {
                                        "cpu": f"{requests.cpu_millis}m",
                                        "memory": f"{requests.memory_mib}Mi",
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
        self._patch(namespace, "deployment", service_name, patch)

    def _scale(self, namespace: str, service_name: str, replicas: int) -> None:
        self._run(
            (
                "scale",
                f"deployment/{service_name}",
                "-n",
                namespace,
                f"--replicas={replicas}",
            )
        )
        self._rollout(namespace, service_name)

    def _rollout(self, namespace: str, service_name: str) -> None:
        self._run(
            (
                "rollout",
                "status",
                f"deployment/{service_name}",
                "-n",
                namespace,
                "--timeout=120s",
            )
        )


def _manifest_path(plan: RehearsalPlan) -> Path:
    if plan.overlay_path is None:
        raise KubernetesOperationError("rehearsal plan is missing an overlay path")
    return Path(plan.overlay_path) / "resources.yaml"


def _require_rehearsal_namespace(namespace: str) -> None:
    if not namespace.startswith("kc-rehearsal-"):
        raise KubernetesOperationError("namespace must begin with kc-rehearsal-")


def _deployment_replicas(document: dict[str, Any]) -> int:
    replicas = document.get("spec", {}).get("replicas", 1)
    return int(replicas)


def _deployment_requests(
    document: dict[str, Any],
    service: ServiceProfile,
) -> ResourceRequests:
    containers = (
        document.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if isinstance(containers, list):
        for container in containers:
            if not isinstance(container, dict):
                continue
            if container.get("name") not in {"app", service.name}:
                continue
            requests = container.get("resources", {}).get("requests", {})
            if isinstance(requests, dict):
                return ResourceRequests(
                    cpu_millis=_parse_cpu_millis(str(requests.get("cpu", "0"))),
                    memory_mib=_parse_memory_mib(str(requests.get("memory", "0"))),
                )
    return service.resource_requests


def _hpa_bounds(document: dict[str, Any] | None) -> HpaBounds | None:
    if document is None:
        return None
    spec = document.get("spec", {})
    return HpaBounds(
        min_replicas=int(spec.get("minReplicas", 0)),
        max_replicas=int(spec.get("maxReplicas", 1)),
    )


def _parse_cpu_millis(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def _parse_memory_mib(value: str) -> int:
    normalized = value.strip()
    if normalized.endswith("Mi"):
        return int(normalized[:-2])
    if normalized.endswith("Gi"):
        return int(normalized[:-2]) * 1024
    return int(normalized)


def _pod_is_ready(pod: dict[str, Any]) -> bool:
    status = pod.get("status", {})
    if status.get("phase") != "Running":
        return False
    conditions = status.get("conditions", [])
    if not isinstance(conditions, list):
        return False
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
        if isinstance(condition, dict)
    )
