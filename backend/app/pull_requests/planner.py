from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml

from app.domain.interfaces import ManifestRenderer
from app.domain.models import (
    CompatibilitySeverity,
    CouncilAction,
    ExperimentReport,
    ExperimentStatus,
    RepositoryChange,
    RepositoryChangeSet,
    RepositorySnapshot,
    ServiceProfile,
    ValidationResult,
    ValidationStatus,
)


class RepositoryChangePlanningError(RuntimeError):
    """Raised when a rehearsal cannot be translated into safe repository changes."""


class RepositoryChangePlanner:
    """Translates validated council actions into production Kustomize overlay patches."""

    def plan(
        self,
        snapshot: RepositorySnapshot,
        report: ExperimentReport,
        services: Sequence[ServiceProfile],
        renderer: ManifestRenderer,
    ) -> RepositoryChangeSet:
        if report.status != ExperimentStatus.SUCCESSFUL:
            raise RepositoryChangePlanningError("only successful experiments can create PRs")

        workspace = Path(snapshot.workspace_path).resolve()
        deployment_path = (workspace / snapshot.deployment_path).resolve()
        if not deployment_path.is_relative_to(workspace):
            raise RepositoryChangePlanningError("deployment path escapes repository workspace")
        if not (deployment_path / "kustomization.yaml").is_file():
            raise RepositoryChangePlanningError("deployment path must contain kustomization.yaml")

        service_map = {service.name: service for service in services}
        patch_documents = _patches_for_actions(report.applied_actions, service_map)
        if not patch_documents:
            raise RepositoryChangePlanningError(
                "experiment did not produce repository-applicable actions"
            )

        patch_directory = deployment_path / "kubecouncil-patches"
        patch_directory.mkdir(exist_ok=True)

        changed_paths: list[Path] = []
        for filename, document in patch_documents.items():
            patch_path = patch_directory / filename
            _write_yaml_documents(patch_path, (document,))
            changed_paths.append(patch_path)

        kustomization = deployment_path / "kustomization.yaml"
        patch_entries = tuple(
            Path("kubecouncil-patches") / path.name for path in changed_paths
        )
        _add_patch_entries(kustomization, patch_entries)
        changed_paths.append(kustomization)

        relative_paths = tuple(_relative_to_workspace(workspace, path) for path in changed_paths)
        _validate_changed_paths(relative_paths)
        _validate_patch_fields(tuple(changed_paths[:-1]))

        rendered = renderer.render(snapshot)
        blocking = tuple(
            issue.message
            for issue in rendered.compatibility_issues
            if issue.severity == CompatibilitySeverity.ERROR
        )
        if blocking:
            raise RepositoryChangePlanningError("; ".join(blocking))

        return RepositoryChangeSet(
            run_id=report.run_id,
            branch_name=f"kubecouncil/rehearsal-{report.run_id}",
            changes=tuple(
                RepositoryChange(
                    path=str(path),
                    rationale="generated from validated rehearsal action",
                )
                for path in relative_paths
            ),
            validation=ValidationResult(status=ValidationStatus.PASSED),
        )


def changed_file_contents(
    snapshot: RepositorySnapshot,
    change_set: RepositoryChangeSet,
) -> Mapping[Path, str]:
    workspace = Path(snapshot.workspace_path).resolve()
    return {
        Path(change.path): (workspace / change.path).read_text()
        for change in change_set.changes
    }


def _patches_for_actions(
    actions: Sequence[CouncilAction],
    services: Mapping[str, ServiceProfile],
) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for action in actions:
        service = services.get(action.target_service)
        if service is None:
            raise RepositoryChangePlanningError(
                f"target service {action.target_service!r} is unknown"
            )
        if action.action_type == "scale_deployment":
            replicas = _int_parameter(action, "replicas")
            documents[f"{action.target_service}-deployment.yaml"] = _deployment_patch(
                action.target_service,
                {"replicas": replicas},
            )
        elif action.action_type == "set_hpa_bounds":
            documents[f"{action.target_service}-hpa.yaml"] = _hpa_patch(
                action.target_service,
                _int_parameter(action, "min_replicas"),
                _int_parameter(action, "max_replicas"),
            )
        elif action.action_type == "set_resource_requests":
            documents[f"{action.target_service}-resources.yaml"] = _deployment_patch(
                action.target_service,
                {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "app",
                                    "resources": {
                                        "requests": {
                                            "cpu": _format_cpu(
                                                _int_parameter(action, "cpu_millis")
                                            ),
                                            "memory": _format_memory(
                                                _int_parameter(action, "memory_mib")
                                            ),
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            )
        elif action.action_type == "set_config_mode":
            mode = action.parameters.get("mode")
            if not isinstance(mode, str) or not mode:
                raise RepositoryChangePlanningError("set_config_mode requires a non-empty mode")
            if mode not in service.degradation_modes:
                raise RepositoryChangePlanningError(
                    f"mode {mode!r} is not approved for {action.target_service}"
                )
            documents[f"{action.target_service}-config-mode.yaml"] = _config_map_patch(
                f"{action.target_service}-config",
                {"MODE": mode},
            )
        elif action.action_type == "suspend_optional_deployment":
            if not service.optional or service.min_replicas != 0:
                raise RepositoryChangePlanningError(
                    f"{action.target_service} cannot be suspended in repository changes"
                )
            documents[f"{action.target_service}-deployment.yaml"] = _deployment_patch(
                action.target_service,
                {"replicas": 0},
            )
        elif action.action_type == "restore_deployment":
            raise RepositoryChangePlanningError(
                "restore_deployment is rehearsal-only and cannot be committed"
            )
        else:
            raise RepositoryChangePlanningError(f"unsupported action type: {action.action_type}")
    return documents


def _deployment_patch(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": spec,
    }


def _hpa_patch(name: str, min_replicas: int, max_replicas: int) -> dict[str, Any]:
    if max_replicas < min_replicas:
        raise RepositoryChangePlanningError("HPA max_replicas must cover min_replicas")
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name},
        "spec": {"minReplicas": min_replicas, "maxReplicas": max_replicas},
    }


def _config_map_patch(name: str, data: Mapping[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name},
        "data": dict(data),
    }


def _write_yaml_documents(path: Path, documents: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        yaml.safe_dump_all(
            tuple(documents),
            sort_keys=False,
            explicit_start=False,
        )
    )


def _add_patch_entries(kustomization_path: Path, patch_paths: Sequence[Path]) -> None:
    loaded = yaml.safe_load(kustomization_path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise RepositoryChangePlanningError("kustomization.yaml must be a mapping")
    kustomization = cast(dict[str, Any], loaded)
    patches = kustomization.get("patches")
    if patches is None:
        patch_entries: list[Any] = []
    elif isinstance(patches, list):
        patch_entries = list(patches)
    else:
        raise RepositoryChangePlanningError("kustomization patches must be a list")

    existing_paths = {
        entry.get("path") for entry in patch_entries if isinstance(entry, dict)
    }
    for patch_path in patch_paths:
        path_text = patch_path.as_posix()
        if path_text not in existing_paths:
            patch_entries.append({"path": path_text})
    kustomization["patches"] = patch_entries
    kustomization_path.write_text(yaml.safe_dump(kustomization, sort_keys=False))


def _validate_changed_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        text = path.as_posix()
        if text.startswith(".github/workflows/") or "/.github/workflows/" in text:
            raise RepositoryChangePlanningError("workflow files may not be modified")
        if ".." in path.parts:
            raise RepositoryChangePlanningError("changed paths must stay inside the repository")
        if not text.endswith((".yaml", ".yml")):
            raise RepositoryChangePlanningError(f"unsupported changed file type: {text}")


def _validate_patch_fields(paths: Sequence[Path]) -> None:
    for path in paths:
        for document in yaml.safe_load_all(path.read_text()):
            if not isinstance(document, dict):
                raise RepositoryChangePlanningError(f"{path.name} must contain Kubernetes objects")
            kind = document.get("kind")
            if kind == "Deployment":
                _validate_deployment_patch(document)
            elif kind == "HorizontalPodAutoscaler":
                _validate_mapping_keys(document, ("apiVersion", "kind", "metadata", "spec"))
                _validate_mapping_keys(
                    _mapping(document.get("spec")),
                    ("minReplicas", "maxReplicas"),
                )
            elif kind == "ConfigMap":
                _validate_mapping_keys(document, ("apiVersion", "kind", "metadata", "data"))
                _validate_mapping_keys(_mapping(document.get("data")), ("MODE",))
            else:
                raise RepositoryChangePlanningError(f"{kind} patches are not allowlisted")


def _validate_deployment_patch(document: Mapping[str, Any]) -> None:
    _validate_mapping_keys(document, ("apiVersion", "kind", "metadata", "spec"))
    spec = _mapping(document.get("spec"))
    _validate_mapping_keys(spec, ("replicas", "template"))
    if "template" in spec:
        template = _mapping(spec.get("template"))
        _validate_mapping_keys(template, ("spec",))
        pod_spec = _mapping(template.get("spec"))
        _validate_mapping_keys(pod_spec, ("containers",))
        containers = pod_spec.get("containers")
        if not isinstance(containers, list) or len(containers) != 1:
            raise RepositoryChangePlanningError("resource patches must target one named container")
        container = _mapping(containers[0])
        _validate_mapping_keys(container, ("name", "resources"))
        resources = _mapping(container.get("resources"))
        _validate_mapping_keys(resources, ("requests",))
        requests = _mapping(resources.get("requests"))
        _validate_mapping_keys(requests, ("cpu", "memory"))


def _validate_mapping_keys(mapping: Mapping[str, Any], allowed: Sequence[str]) -> None:
    extra = set(mapping) - set(allowed)
    if extra:
        raise RepositoryChangePlanningError(f"non-allowlisted patch fields: {sorted(extra)}")


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RepositoryChangePlanningError("patch field must be a mapping")
    return cast(Mapping[str, Any], value)


def _int_parameter(action: CouncilAction, key: str) -> int:
    value = action.parameters.get(key)
    if not isinstance(value, int):
        raise RepositoryChangePlanningError(f"{action.action_type} requires integer {key}")
    return value


def _format_cpu(cpu_millis: int) -> str:
    return f"{cpu_millis}m"


def _format_memory(memory_mib: int) -> str:
    return f"{memory_mib}Mi"


def _relative_to_workspace(workspace: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(workspace):
        raise RepositoryChangePlanningError("generated change escaped repository workspace")
    return resolved.relative_to(workspace)
