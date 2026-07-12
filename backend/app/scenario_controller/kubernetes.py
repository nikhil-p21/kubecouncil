"""Narrow Kubernetes API adapter available only to the demo Scenario Controller."""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from app.scenario_controller.models import ScenarioControllerConfig, ScenarioDeploymentState
from app.scenario_controller.service import ScenarioProviderError


class ScenarioKubernetesError(ScenarioProviderError):
    """Raised when an object falls outside the exact Scenario Controller contract."""


class AppsV1Api(Protocol):
    def read_namespaced_deployment(self, *, name: str, namespace: str) -> object: ...

    def patch_namespaced_deployment(
        self,
        *,
        name: str,
        namespace: str,
        body: dict[str, Any],
        _content_type: str,
    ) -> object: ...


class KubernetesApiClient(Protocol):
    def sanitize_for_serialization(self, value: object) -> object: ...


class KubernetesApiScenarioProvider:
    """Can inspect and patch only the two configured Online Boutique Deployments."""

    def __init__(
        self,
        config: ScenarioControllerConfig,
        apps_api: AppsV1Api | None = None,
        api_client: KubernetesApiClient | None = None,
    ) -> None:
        self._config = config
        if apps_api is None or api_client is None:
            kubernetes_config = importlib.import_module("kubernetes.config")
            kubernetes_client = importlib.import_module("kubernetes.client")
            kubernetes_config.load_incluster_config()
            apps_api = kubernetes_client.AppsV1Api()
            api_client = kubernetes_client.ApiClient()
        self._apps_api = apps_api
        self._api_client = api_client
        self._containers = {
            config.recommendation_deployment: config.recommendation_container,
            config.redis_deployment: "redis",
        }

    def inspect_deployment(self, name: str) -> ScenarioDeploymentState:
        self._require_target(name)
        try:
            response = self._apps_api.read_namespaced_deployment(
                name=name,
                namespace=self._config.application_namespace,
            )
        except Exception as error:
            raise ScenarioKubernetesError(
                "Kubernetes could not read the allowlisted scenario target"
            ) from error
        return self._state(self._document(response), name)

    def set_memory_resources(
        self,
        name: str,
        *,
        container: str,
        memory_request: str,
        memory_limit: str,
        expected_resource_version: str,
    ) -> ScenarioDeploymentState:
        self._require_target(name)
        if name != self._config.recommendation_deployment:
            raise ScenarioKubernetesError(
                "memory mutation is allowed only for recommendationservice"
            )
        if container != self._config.recommendation_container:
            raise ScenarioKubernetesError("recommendation container does not match configuration")
        patch = {
            "metadata": {"resourceVersion": expected_resource_version},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container,
                                "resources": {
                                    "requests": {"memory": memory_request},
                                    "limits": {"memory": memory_limit},
                                },
                            }
                        ]
                    }
                }
            },
        }
        return self._patch(
            name,
            patch,
            content_type="application/strategic-merge-patch+json",
        )

    def set_replicas(
        self, name: str, *, replicas: int, expected_resource_version: str
    ) -> ScenarioDeploymentState:
        self._require_target(name)
        if name != self._config.redis_deployment:
            raise ScenarioKubernetesError("replica mutation is allowed only for redis-cart")
        patch = {
            "metadata": {"resourceVersion": expected_resource_version},
            "spec": {"replicas": replicas},
        }
        return self._patch(name, patch, content_type="application/merge-patch+json")

    def _patch(
        self, name: str, patch: dict[str, Any], *, content_type: str
    ) -> ScenarioDeploymentState:
        try:
            response = self._apps_api.patch_namespaced_deployment(
                name=name,
                namespace=self._config.application_namespace,
                body=patch,
                _content_type=content_type,
            )
        except Exception as error:
            raise ScenarioKubernetesError(
                "Kubernetes rejected the allowlisted scenario patch"
            ) from error
        return self._state(self._document(response), name)

    def _document(self, response: object) -> dict[str, Any]:
        document = (
            response
            if isinstance(response, dict)
            else self._api_client.sanitize_for_serialization(response)
        )
        if not isinstance(document, dict):
            raise ScenarioKubernetesError("Kubernetes returned a non-object response")
        return document

    def _state(self, document: dict[str, Any], expected_name: str) -> ScenarioDeploymentState:
        metadata = document.get("metadata", {})
        spec = document.get("spec", {})
        template_spec = spec.get("template", {}).get("spec", {})
        if metadata.get("name") != expected_name:
            raise ScenarioKubernetesError("Kubernetes returned an unexpected Deployment")
        expected_container = self._containers[expected_name]
        container = next(
            (
                item
                for item in template_spec.get("containers", [])
                if item.get("name") == expected_container
            ),
            None,
        )
        if not isinstance(container, dict):
            raise ScenarioKubernetesError("configured scenario container is absent")
        resources = container.get("resources", {})
        memory_request = resources.get("requests", {}).get("memory")
        memory_limit = resources.get("limits", {}).get("memory")
        if not isinstance(memory_request, str) or not isinstance(memory_limit, str):
            raise ScenarioKubernetesError(
                "configured scenario container has incomplete memory resources"
            )
        return ScenarioDeploymentState(
            name=expected_name,
            namespace=self._config.application_namespace,
            resource_version=str(metadata.get("resourceVersion", "")),
            replicas=int(spec.get("replicas", 1)),
            container=expected_container,
            memory_request=memory_request,
            memory_limit=memory_limit,
        )

    def _require_target(self, name: str) -> None:
        if name not in self._containers:
            raise ScenarioKubernetesError("Deployment is outside Scenario Controller scope")
