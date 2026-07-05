from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

from app.domain.models import (
    AnalysisResult,
    CompatibilityIssue,
    CompatibilitySeverity,
    DeploymentSource,
    ManifestResource,
    RehearsalPlan,
    ResourceRequests,
    ServiceProfile,
)

REHEARSAL_NAMESPACE_PREFIX = "kc-rehearsal-"
REHEARSAL_LABELS = {
    "app.kubernetes.io/managed-by": "kubecouncil",
    "kubecouncil.io/rehearsal": "true",
}
DEFAULT_REHEARSAL_TTL_HOURS = 6
OMITTED_KINDS = {"Ingress", "Secret"}
GENERATED_KINDS = {"Namespace", "ResourceQuota"}
CLUSTER_SCOPED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "MutatingWebhookConfiguration",
    "PersistentVolume",
    "ValidatingWebhookConfiguration",
}
SUPPORTED_REHEARSAL_KINDS = {
    "ConfigMap",
    "Deployment",
    "HorizontalPodAutoscaler",
    "Job",
    "Namespace",
    "NetworkPolicy",
    "PodDisruptionBudget",
    "ResourceQuota",
    "Service",
}


class RehearsalPlanningError(ValueError):
    """Raised when a safe rehearsal twin cannot be generated."""


class RehearsalPlanner:
    """Builds a sanitized, run-scoped Kubernetes rehearsal plan from analysis output."""

    def __init__(self, overlay_root: Path = Path("/tmp/kubecouncil-rehearsals")) -> None:
        self._overlay_root = overlay_root

    def build_plan(self, analysis: AnalysisResult) -> RehearsalPlan:
        namespace = rehearsal_namespace(analysis.run_id)
        self._ensure_namespace_does_not_reuse_source(analysis.source, namespace)
        self._ensure_no_cluster_scoped_resources(analysis.source.rendered_resources)

        overlay_path = self._overlay_root / analysis.run_id
        self._ensure_overlay_outside_source(
            overlay_path,
            Path(analysis.source.repository.workspace_path),
        )

        sanitized_resources, substitutions = sanitize_resources(
            analysis.source.rendered_resources,
            namespace,
            analysis.run_id,
        )
        quota = resource_quota_for_services(analysis.services)
        namespace_resource = _namespace_resource(namespace, analysis.run_id)
        quota_resource = _quota_resource(namespace, analysis.run_id, quota)
        rendered_resources = (namespace_resource, quota_resource, *sanitized_resources)
        self._write_overlay(overlay_path, rendered_resources)

        return RehearsalPlan(
            run_id=analysis.run_id,
            namespace=namespace,
            source=analysis.source,
            services=analysis.services,
            compatibility_issues=tuple(
                issue for issue in analysis.compatibility_issues if issue.severity != "error"
            ),
            resource_quota_cpu_millis=quota.cpu_millis,
            resource_quota_memory_mib=quota.memory_mib,
            overlay_path=str(overlay_path),
            rendered_resources=rendered_resources,
            safety_substitutions=tuple(substitutions),
        )

    def _write_overlay(self, overlay_path: Path, resources: Sequence[ManifestResource]) -> None:
        overlay_path.mkdir(parents=True, exist_ok=True)
        manifest_path = overlay_path / "resources.yaml"
        kustomization_path = overlay_path / "kustomization.yaml"
        documents = [resource.content for resource in resources]
        manifest_path.write_text(
            yaml.safe_dump_all(documents, sort_keys=False),
            encoding="utf-8",
        )
        kustomization_path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "kustomize.config.k8s.io/v1beta1",
                    "kind": "Kustomization",
                    "resources": ["resources.yaml"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _ensure_overlay_outside_source(self, overlay_path: Path, workspace_path: Path) -> None:
        try:
            overlay_path.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            return
        raise RehearsalPlanningError(
            "rehearsal overlay must be generated outside source repository"
        )

    def _ensure_namespace_does_not_reuse_source(
        self,
        source: DeploymentSource,
        namespace: str,
    ) -> None:
        source_namespaces = {
            resource.namespace for resource in source.rendered_resources if resource.namespace
        }
        if namespace in source_namespaces:
            raise RehearsalPlanningError("rehearsal namespace must not be the source namespace")

    def _ensure_no_cluster_scoped_resources(self, resources: Sequence[ManifestResource]) -> None:
        blocked = [
            resource for resource in resources if resource.kind in CLUSTER_SCOPED_KINDS
        ]
        if blocked:
            names = ", ".join(f"{resource.kind}/{resource.name}" for resource in blocked)
            raise RehearsalPlanningError(f"cluster-scoped resources cannot be rehearsed: {names}")


def rehearsal_namespace(run_id: str) -> str:
    safe_run_id = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    if not safe_run_id:
        raise RehearsalPlanningError("run_id must contain a Kubernetes-safe character")
    return f"{REHEARSAL_NAMESPACE_PREFIX}{safe_run_id}"


def sanitize_resources(
    resources: Sequence[ManifestResource],
    namespace: str,
    run_id: str,
) -> tuple[tuple[ManifestResource, ...], list[str]]:
    sanitized: list[ManifestResource] = []
    substitutions: list[str] = []
    for resource in resources:
        if resource.kind in GENERATED_KINDS:
            substitutions.append(
                f"generated rehearsal {resource.kind} instead of source {resource.name}"
            )
            continue
        if resource.kind in OMITTED_KINDS:
            substitutions.append(f"omitted production {resource.kind}/{resource.name}")
            continue
        if resource.kind not in SUPPORTED_REHEARSAL_KINDS:
            substitutions.append(f"omitted unsupported {resource.kind}/{resource.name}")
            continue

        content = copy.deepcopy(resource.content)
        _rewrite_metadata(content, namespace, run_id)
        if resource.kind == "Deployment":
            substitutions.extend(_sanitize_deployment(content, resource.name))
        elif resource.kind == "ConfigMap":
            substitutions.extend(_sanitize_config_map(content, resource.name))
        sanitized.append(_resource_from_content(content, resource.source))
    return tuple(sanitized), substitutions


def resource_quota_for_services(services: Sequence[ServiceProfile]) -> ResourceRequests:
    cpu = sum(
        service.current_replicas * service.resource_requests.cpu_millis for service in services
    )
    memory = sum(
        service.current_replicas * service.resource_requests.memory_mib for service in services
    )
    return ResourceRequests(cpu_millis=max(cpu + 500, 1000), memory_mib=max(memory + 512, 1024))


def compatibility_issue_for_planning_error(error: RehearsalPlanningError) -> CompatibilityIssue:
    return CompatibilityIssue(
        severity=CompatibilitySeverity.ERROR,
        resource_kind="RehearsalPlan",
        resource_name="rehearsal",
        message=str(error),
        source="rehearsal planner",
    )


def _namespace_resource(namespace: str, run_id: str) -> ManifestResource:
    content: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {**REHEARSAL_LABELS, "kubecouncil.io/run-id": run_id},
            "annotations": {
                "kubecouncil.io/cleanup": "delete-namespace",
                "kubecouncil.io/source": "rehearsal-copy",
                "kubecouncil.io/expires-at": _expires_at(),
            },
        },
    }
    return ManifestResource(
        api_version="v1",
        kind="Namespace",
        name=namespace,
        namespace=namespace,
        source="generated:Namespace",
        content=content,
    )


def _quota_resource(namespace: str, run_id: str, quota: ResourceRequests) -> ManifestResource:
    content: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": _managed_metadata("kubecouncil-rehearsal-quota", namespace, run_id),
        "spec": {
            "hard": {
                "requests.cpu": f"{quota.cpu_millis}m",
                "requests.memory": f"{quota.memory_mib}Mi",
            }
        },
    }
    return _resource_from_content(content, "generated:ResourceQuota")


def _rewrite_metadata(content: dict[str, Any], namespace: str, run_id: str) -> None:
    metadata = _dict(content.setdefault("metadata", {}))
    metadata["namespace"] = namespace
    name = str(metadata.get("name", "resource"))
    managed = _managed_metadata(name, namespace, run_id)
    metadata["labels"] = managed["labels"]
    metadata["annotations"] = managed["annotations"]


def _managed_metadata(name: str, namespace: str, run_id: str) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": namespace,
        "labels": {**REHEARSAL_LABELS, "kubecouncil.io/run-id": run_id},
        "annotations": {
            "kubecouncil.io/cleanup": "delete-namespace",
            "kubecouncil.io/source": "rehearsal-copy",
            "kubecouncil.io/expires-at": _expires_at(),
        },
    }


def _expires_at() -> str:
    return (datetime.now(UTC) + timedelta(hours=DEFAULT_REHEARSAL_TTL_HOURS)).isoformat()


def _sanitize_deployment(content: dict[str, Any], resource_name: str) -> list[str]:
    substitutions: list[str] = []
    spec = _dict(content.get("spec"))
    template = _dict(spec.get("template"))
    pod_metadata = _dict(template.setdefault("metadata", {}))
    pod_metadata["labels"] = {
        **_dict(pod_metadata.get("labels")),
        **REHEARSAL_LABELS,
    }
    pod_spec = _dict(template.get("spec"))
    for container in _iter_dicts(pod_spec.get("containers")):
        substitutions.extend(_remove_secret_env(container, resource_name))
    volumes = []
    for volume in _iter_dicts(pod_spec.get("volumes")):
        if "secret" in volume:
            substitutions.append(f"removed Secret volume from Deployment/{resource_name}")
            continue
        volumes.append(volume)
    if volumes:
        pod_spec["volumes"] = volumes
    else:
        pod_spec.pop("volumes", None)
    template["spec"] = pod_spec
    spec["template"] = template
    content["spec"] = spec
    return substitutions


def _sanitize_config_map(content: dict[str, Any], resource_name: str) -> list[str]:
    data = _dict(content.setdefault("data", {}))
    substitutions = ["added rehearsal ConfigMap marker"]
    data["KUBECOUNCIL_REHEARSAL"] = "true"
    for key in tuple(data):
        if key == "MODE" and str(data[key]).lower() == "live":
            data[key] = "cached"
            substitutions.append(f"set ConfigMap/{resource_name} MODE=cached")
        elif key.endswith("_URL") or key.endswith("_ENDPOINT"):
            data[key] = _internal_service_url(str(data[key]))
            substitutions.append(f"rewrote ConfigMap/{resource_name} {key} to internal service")
    content["data"] = data
    return substitutions


def _remove_secret_env(container: dict[str, Any], resource_name: str) -> list[str]:
    substitutions: list[str] = []
    env_from = []
    for item in _iter_dicts(container.get("envFrom")):
        if "secretRef" in item:
            substitutions.append(f"removed Secret envFrom from Deployment/{resource_name}")
            continue
        env_from.append(item)
    if env_from:
        container["envFrom"] = env_from
    else:
        container.pop("envFrom", None)

    env = []
    for item in _iter_dicts(container.get("env")):
        value_from = _dict(item.get("valueFrom"))
        if "secretKeyRef" in value_from:
            substitutions.append(f"removed Secret env var from Deployment/{resource_name}")
            continue
        env.append(item)
    if env:
        container["env"] = env
    else:
        container.pop("env", None)
    return substitutions


def _internal_service_url(value: str) -> str:
    service = value.removeprefix("http://").removeprefix("https://").split(":", maxsplit=1)[0]
    if not service:
        return value
    return f"http://{service}"


def _resource_from_content(content: Mapping[str, Any], source: str) -> ManifestResource:
    metadata = _dict(content.get("metadata"))
    return ManifestResource(
        api_version=str(content["apiVersion"]),
        kind=str(content["kind"]),
        name=str(metadata["name"]),
        namespace=str(metadata.get("namespace")),
        source=source,
        content=dict(content),
    )


def _dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _iter_dicts(value: object) -> Iterable[dict[str, Any]]:
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, dict))
