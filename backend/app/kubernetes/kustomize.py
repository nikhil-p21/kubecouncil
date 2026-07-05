import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from app.domain.interfaces import ManifestRenderer
from app.domain.models import (
    CompatibilityIssue,
    CompatibilitySeverity,
    DeploymentSource,
    HpaBounds,
    ManifestResource,
    RepositorySnapshot,
    ResourceRequests,
    ServiceProfile,
)

CommandRunner = Callable[[Sequence[str], Path], str]

SUPPORTED_KINDS = {
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
UNSUPPORTED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "PersistentVolume",
    "PersistentVolumeClaim",
    "StatefulSet",
}


class ManifestRenderError(RuntimeError):
    """Raised when Kustomize cannot render a deployment."""


class ManifestParseError(ValueError):
    """Raised when rendered Kubernetes YAML is not structurally parseable."""


def default_command_runner(command: Sequence[str], cwd: Path) -> str:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "kubectl kustomize failed"
        raise ManifestRenderError(stderr)
    return result.stdout


class KustomizeManifestRenderer(ManifestRenderer):
    """Renders Kustomize manifests and derives service profiles from plain YAML."""

    def __init__(
        self,
        command_runner: CommandRunner = default_command_runner,
        command: Sequence[str] = ("kubectl", "kustomize"),
    ) -> None:
        self._command_runner = command_runner
        self._command = tuple(command)

    def render(self, snapshot: RepositorySnapshot) -> DeploymentSource:
        workspace = Path(snapshot.workspace_path)
        deployment_path = workspace / snapshot.deployment_path
        output = self._command_runner((*self._command, str(deployment_path)), workspace)
        resources = parse_rendered_manifest(output)
        issues = build_compatibility_report(resources)
        return DeploymentSource(
            repository=snapshot,
            kustomization_path=f"{snapshot.deployment_path}/kustomization.yaml",
            rendered_resource_count=len(resources),
            rendered_resources=resources,
            compatibility_issues=issues,
        )

    def service_profiles(self, source: DeploymentSource) -> Sequence[ServiceProfile]:
        return build_service_profiles(source.rendered_resources)


def parse_rendered_manifest(rendered_yaml: str) -> tuple[ManifestResource, ...]:
    try:
        documents = tuple(yaml.safe_load_all(rendered_yaml))
    except yaml.YAMLError as exc:
        raise ManifestParseError(f"rendered manifest is not valid YAML: {exc}") from exc

    resources: list[ManifestResource] = []
    for index, document in enumerate(documents, start=1):
        if document is None:
            continue
        if not isinstance(document, dict):
            raise ManifestParseError(f"document {index} is not a Kubernetes resource object")
        resource = _manifest_resource(document, index)
        resources.append(resource)
    return tuple(resources)


def build_compatibility_report(
    resources: Sequence[ManifestResource],
) -> tuple[CompatibilityIssue, ...]:
    deployment_names = {resource.name for resource in resources if resource.kind == "Deployment"}
    issues: list[CompatibilityIssue] = []
    for resource in resources:
        if resource.kind in UNSUPPORTED_KINDS:
            issues.append(
                _issue(
                    CompatibilitySeverity.ERROR,
                    resource,
                    f"{resource.kind} is not supported by the MVP rehearsal analyser",
                    resource.source,
                )
            )
        elif resource.kind == "Secret":
            issues.append(
                _issue(
                    CompatibilitySeverity.ERROR,
                    resource,
                    "production Secret resources must not be copied into rehearsal",
                    resource.source,
                )
            )
        elif resource.kind not in SUPPORTED_KINDS:
            issues.append(
                _issue(
                    CompatibilitySeverity.WARNING,
                    resource,
                    f"{resource.kind} is not currently analysed by KubeCouncil",
                    resource.source,
                )
            )

        for secret_source in _secret_reference_sources(resource):
            issues.append(
                _issue(
                    CompatibilitySeverity.ERROR,
                    resource,
                    "unresolved Secret reference must be replaced before rehearsal",
                    secret_source,
                )
            )

        if resource.kind == "Deployment":
            annotations = _annotations(resource)
            for dependency in _split_annotation(annotations.get("kubecouncil.io/dependencies")):
                if dependency not in deployment_names:
                    issues.append(
                        _issue(
                            CompatibilitySeverity.WARNING,
                            resource,
                            f"dependency {dependency!r} is not a rendered Deployment",
                            f"{resource.source}:metadata.annotations.kubecouncil.io/dependencies",
                        )
                    )
    return tuple(issues)


def build_service_profiles(resources: Sequence[ManifestResource]) -> tuple[ServiceProfile, ...]:
    hpas = _hpas_by_target(resources)
    profiles: list[ServiceProfile] = []
    for resource in resources:
        if resource.kind != "Deployment":
            continue
        hpa = hpas.get(resource.name)
        profiles.append(_service_profile(resource, hpa))
    return tuple(sorted(profiles, key=lambda profile: profile.name))


def _manifest_resource(document: Mapping[str, Any], index: int) -> ManifestResource:
    metadata = _mapping(document.get("metadata"))
    name = _string(metadata.get("name"))
    kind = _string(document.get("kind"))
    api_version = _string(document.get("apiVersion"))
    if not name or not kind or not api_version:
        raise ManifestParseError(f"document {index} is missing apiVersion, kind or metadata.name")
    namespace = _string(metadata.get("namespace")) or None
    source = f"rendered.yaml#{index}:{kind}/{name}"
    return ManifestResource(
        api_version=api_version,
        kind=kind,
        name=name,
        namespace=namespace,
        source=source,
        content=dict(document),
    )


def _service_profile(resource: ManifestResource, hpa: ManifestResource | None) -> ServiceProfile:
    annotations = _annotations(resource)
    spec = _mapping(resource.content.get("spec"))
    template = _mapping(spec.get("template"))
    pod_spec = _mapping(template.get("spec"))
    containers = _sequence(pod_spec.get("containers"))
    primary_container = _mapping(containers[0]) if containers else {}
    resources = _mapping(primary_container.get("resources"))
    requests = _mapping(resources.get("requests"))
    replicas = _int(spec.get("replicas"), default=1)
    min_replicas = _int(annotations.get("kubecouncil.io/min-replicas"), default=replicas)
    max_replicas = _int(annotations.get("kubecouncil.io/max-replicas"), default=max(replicas, 1))
    hpa_bounds = _hpa_bounds(hpa)
    if hpa_bounds is not None:
        min_replicas = hpa_bounds.min_replicas
        max_replicas = hpa_bounds.max_replicas

    return ServiceProfile(
        name=resource.name,
        image=_string(primary_container.get("image")) or "unknown",
        current_replicas=replicas,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        resource_requests=ResourceRequests(
            cpu_millis=parse_cpu_millis(requests.get("cpu")),
            memory_mib=parse_memory_mib(requests.get("memory")),
        ),
        criticality=_criticality(annotations.get("kubecouncil.io/criticality")),
        dependencies=tuple(_split_annotation(annotations.get("kubecouncil.io/dependencies"))),
        degradation_modes=tuple(
            _split_annotation(annotations.get("kubecouncil.io/degradation-modes"))
        ),
        optional=_bool(annotations.get("kubecouncil.io/optional")),
        config_maps=tuple(_config_maps_for_deployment(resource)),
        hpa=hpa_bounds,
        namespace=resource.namespace,
        sources={
            "name": f"{resource.source}:metadata.name",
            "image": f"{resource.source}:spec.template.spec.containers[0].image",
            "current_replicas": f"{resource.source}:spec.replicas",
            "min_replicas": _replica_source(resource, hpa, "minReplicas"),
            "max_replicas": _replica_source(resource, hpa, "maxReplicas"),
            "resource_requests": (
                f"{resource.source}:spec.template.spec.containers[0].resources.requests"
            ),
            "criticality": f"{resource.source}:metadata.annotations.kubecouncil.io/criticality",
            "dependencies": f"{resource.source}:metadata.annotations.kubecouncil.io/dependencies",
            "degradation_modes": (
                f"{resource.source}:metadata.annotations.kubecouncil.io/degradation-modes"
            ),
            "optional": f"{resource.source}:metadata.annotations.kubecouncil.io/optional",
            "config_maps": f"{resource.source}:spec.template.spec.containers[*].envFrom",
            "hpa": hpa.source if hpa is not None else "not configured",
        },
    )


def parse_cpu_millis(value: object) -> int:
    text = _string(value)
    if not text:
        return 0
    if text.endswith("m"):
        return int(text[:-1])
    return int(float(text) * 1000)


def parse_memory_mib(value: object) -> int:
    text = _string(value)
    if not text:
        return 0
    units = {
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "K": 1000 / 1024 / 1024,
        "M": 1000 / 1024,
        "G": 1000,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(float(text) / 1024 / 1024)


def _hpas_by_target(resources: Sequence[ManifestResource]) -> dict[str, ManifestResource]:
    hpas: dict[str, ManifestResource] = {}
    for resource in resources:
        if resource.kind != "HorizontalPodAutoscaler":
            continue
        spec = _mapping(resource.content.get("spec"))
        target = _mapping(spec.get("scaleTargetRef"))
        if _string(target.get("kind")) == "Deployment":
            target_name = _string(target.get("name"))
            if target_name:
                hpas[target_name] = resource
    return hpas


def _hpa_bounds(hpa: ManifestResource | None) -> HpaBounds | None:
    if hpa is None:
        return None
    spec = _mapping(hpa.content.get("spec"))
    return HpaBounds(
        min_replicas=_int(spec.get("minReplicas"), default=1),
        max_replicas=_int(spec.get("maxReplicas"), default=1),
    )


def _replica_source(resource: ManifestResource, hpa: ManifestResource | None, field: str) -> str:
    if hpa is not None:
        return f"{hpa.source}:spec.{field}"
    annotation = "min-replicas" if field == "minReplicas" else "max-replicas"
    return f"{resource.source}:metadata.annotations.kubecouncil.io/{annotation}"


def _config_maps_for_deployment(resource: ManifestResource) -> list[str]:
    spec = _mapping(resource.content.get("spec"))
    template = _mapping(spec.get("template"))
    pod_spec = _mapping(template.get("spec"))
    names: set[str] = set()
    for container in _sequence(pod_spec.get("containers")):
        container_map = _mapping(container)
        for env_from in _sequence(container_map.get("envFrom")):
            config_ref = _mapping(_mapping(env_from).get("configMapRef"))
            name = _string(config_ref.get("name"))
            if name:
                names.add(name)
        for env in _sequence(container_map.get("env")):
            value_from = _mapping(_mapping(env).get("valueFrom"))
            config_ref = _mapping(value_from.get("configMapKeyRef"))
            name = _string(config_ref.get("name"))
            if name:
                names.add(name)
    return sorted(names)


def _secret_reference_sources(resource: ManifestResource) -> list[str]:
    spec = _mapping(resource.content.get("spec"))
    template = _mapping(spec.get("template"))
    pod_spec = _mapping(template.get("spec"))
    sources: list[str] = []
    for container_index, container in enumerate(_sequence(pod_spec.get("containers"))):
        container_map = _mapping(container)
        for env_from_index, env_from in enumerate(_sequence(container_map.get("envFrom"))):
            if "secretRef" in _mapping(env_from):
                sources.append(
                    f"{resource.source}:containers[{container_index}].envFrom[{env_from_index}]"
                )
        for env_index, env in enumerate(_sequence(container_map.get("env"))):
            value_from = _mapping(_mapping(env).get("valueFrom"))
            if "secretKeyRef" in value_from:
                sources.append(f"{resource.source}:containers[{container_index}].env[{env_index}]")
    for volume_index, volume in enumerate(_sequence(pod_spec.get("volumes"))):
        if "secret" in _mapping(volume):
            sources.append(f"{resource.source}:volumes[{volume_index}]")
    return sources


def _issue(
    severity: CompatibilitySeverity,
    resource: ManifestResource,
    message: str,
    source: str,
) -> CompatibilityIssue:
    return CompatibilityIssue(
        severity=severity,
        resource_kind=resource.kind,
        resource_name=resource.name,
        message=message,
        source=source,
    )


def _annotations(resource: ManifestResource) -> Mapping[str, str]:
    metadata = _mapping(resource.content.get("metadata"))
    annotations = _mapping(metadata.get("annotations"))
    return {str(key): str(value) for key, value in annotations.items()}


def _split_annotation(value: str | None) -> list[str]:
    if value is None:
        return []
    return [
        item.strip()
        for item in value.split(",")
        if item.strip() and item.strip().lower() not in {"none", "n/a"}
    ]


def _criticality(value: str | None) -> str:
    if value in {"critical", "important", "optional"}:
        return value
    return "important"


def _bool(value: str | None) -> bool:
    return str(value).lower() == "true"


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}


def _sequence(value: object) -> Sequence[Any]:
    return cast(Sequence[Any], value) if isinstance(value, list) else ()


def _string(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return int(str(value))
