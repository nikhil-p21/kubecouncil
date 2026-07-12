from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.domain.incidents import ApplicationProfile
from app.scenario_controller.api import create_app
from app.scenario_controller.kubernetes import (
    KubernetesApiScenarioProvider,
    ScenarioKubernetesError,
)
from app.scenario_controller.models import (
    ScenarioAction,
    ScenarioControllerConfig,
    ScenarioDeploymentState,
    ScenarioName,
)
from app.scenario_controller.service import (
    InMemoryScenarioAuditStore,
    ScenarioController,
    ScenarioSafetyError,
)

ROOT = Path(__file__).resolve().parents[2]
DEMO_MANIFESTS = ROOT / "manifests/incident-response/demo"


class FakeScenarioKubernetesProvider:
    def __init__(self) -> None:
        self.states = {
            "recommendationservice": ScenarioDeploymentState(
                name="recommendationservice",
                namespace="online-boutique",
                resource_version="1",
                replicas=1,
                container="server",
                memory_request="220Mi",
                memory_limit="450Mi",
            ),
            "redis-cart": ScenarioDeploymentState(
                name="redis-cart",
                namespace="online-boutique",
                resource_version="1",
                replicas=1,
                container="redis",
                memory_request="70Mi",
                memory_limit="256Mi",
            ),
        }
        self.patches: list[dict[str, object]] = []

    def inspect_deployment(self, name: str) -> ScenarioDeploymentState:
        return self.states[name]

    def set_memory_resources(
        self,
        name: str,
        *,
        container: str,
        memory_request: str,
        memory_limit: str,
        expected_resource_version: str,
    ) -> ScenarioDeploymentState:
        current = self.states[name]
        assert current.resource_version == expected_resource_version
        assert current.container == container
        updated = current.model_copy(
            update={
                "memory_request": memory_request,
                "memory_limit": memory_limit,
                "resource_version": str(int(current.resource_version) + 1),
            }
        )
        self.states[name] = updated
        self.patches.append(
            {
                "name": name,
                "memory_request": memory_request,
                "memory_limit": memory_limit,
                "resource_version": expected_resource_version,
            }
        )
        return updated

    def set_replicas(
        self, name: str, *, replicas: int, expected_resource_version: str
    ) -> ScenarioDeploymentState:
        current = self.states[name]
        assert current.resource_version == expected_resource_version
        updated = current.model_copy(
            update={
                "replicas": replicas,
                "resource_version": str(int(current.resource_version) + 1),
            }
        )
        self.states[name] = updated
        self.patches.append(
            {
                "name": name,
                "replicas": replicas,
                "resource_version": expected_resource_version,
            }
        )
        return updated


class FakeAppsV1Api:
    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []
        self.patches: list[tuple[str, str, dict[str, Any], str]] = []
        self.document: dict[str, Any] = {
            "metadata": {"name": "recommendationservice", "resourceVersion": "7"},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "server",
                                "resources": {
                                    "requests": {"memory": "220Mi"},
                                    "limits": {"cpu": "200m", "memory": "450Mi"},
                                },
                            }
                        ]
                    }
                },
            },
        }

    def read_namespaced_deployment(self, *, name: str, namespace: str) -> object:
        self.reads.append((name, namespace))
        return self.document

    def patch_namespaced_deployment(
        self,
        *,
        name: str,
        namespace: str,
        body: dict[str, Any],
        _content_type: str,
    ) -> object:
        self.patches.append((name, namespace, body, _content_type))
        container_patch = body["spec"]["template"]["spec"]["containers"][0]
        container = self.document["spec"]["template"]["spec"]["containers"][0]
        container["resources"]["requests"]["memory"] = container_patch["resources"][
            "requests"
        ]["memory"]
        container["resources"]["limits"]["memory"] = container_patch["resources"][
            "limits"
        ]["memory"]
        self.document["metadata"]["resourceVersion"] = "8"
        return self.document


class FakeApiClient:
    def sanitize_for_serialization(self, value: object) -> object:
        return value


def _config(*, demo_mode: bool = True) -> ScenarioControllerConfig:
    return ScenarioControllerConfig(
        demo_mode=demo_mode,
        application_namespace="online-boutique",
        recommendation_deployment="recommendationservice",
        recommendation_container="server",
        recommendation_safe_memory_request="220Mi",
        recommendation_safe_memory_limit="450Mi",
        recommendation_unsafe_memory_request="25Mi",
        recommendation_unsafe_memory_limit="25Mi",
        redis_deployment="redis-cart",
        redis_safe_replicas=1,
    )


def _render_demo() -> list[dict[str, object]]:
    completed = subprocess.run(  # noqa: S603
        ("kubectl", "kustomize", str(DEMO_MANIFESTS)),
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


def test_recommendation_scenario_is_idempotent_and_resets_exact_resources() -> None:
    kubernetes = FakeScenarioKubernetesProvider()
    audit = InMemoryScenarioAuditStore()
    controller = ScenarioController(_config(), kubernetes, audit)

    injected = controller.transition(ScenarioName.RECOMMENDATION_OOM, ScenarioAction.INJECT)
    duplicate = controller.transition(ScenarioName.RECOMMENDATION_OOM, ScenarioAction.INJECT)
    reset = controller.transition(ScenarioName.RECOMMENDATION_OOM, ScenarioAction.RESET)

    assert injected.changed
    assert (injected.after.memory_request, injected.after.memory_limit) == ("25Mi", "25Mi")
    assert not duplicate.changed
    assert reset.changed
    assert (reset.after.memory_request, reset.after.memory_limit) == ("220Mi", "450Mi")
    assert [
        (patch["memory_request"], patch["memory_limit"])
        for patch in kubernetes.patches
    ] == [("25Mi", "25Mi"), ("220Mi", "450Mi")]
    assert [event.action for event in audit.list_events()] == [
        ScenarioAction.INJECT,
        ScenarioAction.INJECT,
        ScenarioAction.RESET,
    ]


def test_redis_scenario_scales_only_protected_dependency_and_resets() -> None:
    kubernetes = FakeScenarioKubernetesProvider()
    controller = ScenarioController(_config(), kubernetes, InMemoryScenarioAuditStore())

    injected = controller.transition(ScenarioName.REDIS_OUTAGE, ScenarioAction.INJECT)
    reset = controller.transition(ScenarioName.REDIS_OUTAGE, ScenarioAction.RESET)

    assert injected.after.name == "redis-cart"
    assert injected.after.replicas == 0
    assert reset.after.replicas == 1
    assert [patch["name"] for patch in kubernetes.patches] == ["redis-cart", "redis-cart"]


def test_scenario_controller_fails_closed_when_disabled_or_state_drifted() -> None:
    kubernetes = FakeScenarioKubernetesProvider()
    disabled = ScenarioController(
        _config(demo_mode=False), kubernetes, InMemoryScenarioAuditStore()
    )
    with pytest.raises(ScenarioSafetyError, match="disabled"):
        disabled.transition(ScenarioName.RECOMMENDATION_OOM, ScenarioAction.INJECT)

    kubernetes.states["recommendationservice"] = kubernetes.states[
        "recommendationservice"
    ].model_copy(update={"memory_limit": "300Mi"})
    enabled = ScenarioController(_config(), kubernetes, InMemoryScenarioAuditStore())
    with pytest.raises(ScenarioSafetyError, match="unexpected memory resources"):
        enabled.transition(ScenarioName.RECOMMENDATION_OOM, ScenarioAction.INJECT)
    assert kubernetes.patches == []


def test_kubernetes_adapter_builds_only_allowlisted_optimistic_patch() -> None:
    apps_api = FakeAppsV1Api()
    provider = KubernetesApiScenarioProvider(_config(), apps_api, FakeApiClient())

    before = provider.inspect_deployment("recommendationservice")
    after = provider.set_memory_resources(
        "recommendationservice",
        container="server",
        memory_request="25Mi",
        memory_limit="25Mi",
        expected_resource_version=before.resource_version,
    )

    assert (after.memory_request, after.memory_limit) == ("25Mi", "25Mi")
    name, namespace, patch, content_type = apps_api.patches[-1]
    assert (name, namespace) == ("recommendationservice", "online-boutique")
    assert content_type == "application/strategic-merge-patch+json"
    assert patch["metadata"] == {"resourceVersion": "7"}
    assert patch["spec"] == {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "server",
                        "resources": {
                            "requests": {"memory": "25Mi"},
                            "limits": {"memory": "25Mi"},
                        },
                    }
                ]
            }
        }
    }
    assert not {"annotations", "labels"}.intersection(json.dumps(patch))
    with pytest.raises(ScenarioKubernetesError, match="outside"):
        provider.inspect_deployment("frontend")


def test_demo_api_uses_separate_controller_and_exposes_audit_only_on_its_surface() -> None:
    kubernetes = FakeScenarioKubernetesProvider()
    audit = InMemoryScenarioAuditStore()
    controller = ScenarioController(_config(), kubernetes, audit)
    client = TestClient(create_app(controller, _config()))

    response = client.post("/api/demo/scenarios/recommendation_oom/inject")

    assert response.status_code == 200
    assert response.json()["after"]["memory_request"] == "25Mi"
    assert response.json()["after"]["memory_limit"] == "25Mi"
    audit_response = client.get("/api/demo/audit")
    assert audit_response.status_code == 200
    assert audit_response.json()[0]["actor"] == "scenario-controller"


def test_demo_api_readiness_fails_when_non_demo_profile_disables_controls() -> None:
    config = _config(demo_mode=False)
    controller = ScenarioController(
        config, FakeScenarioKubernetesProvider(), InMemoryScenarioAuditStore()
    )
    client = TestClient(create_app(controller, config))

    assert client.get("/ready").status_code == 503
    assert (
        client.post("/api/demo/scenarios/redis_outage/inject").json()["detail"]["code"]
        == "scenario_transition_refused"
    )


def test_demo_render_has_authoritative_profile_enrollment_and_steady_load() -> None:
    resources = _render_demo()
    by_kind_name = {
        (resource["kind"], resource["metadata"]["name"]): resource
        for resource in resources
    }
    namespace = by_kind_name[("Namespace", "online-boutique")]
    assert namespace["metadata"]["labels"]["kubecouncil.io/enrolled"] == "true"

    profile_config = by_kind_name[("ConfigMap", "kubecouncil-application-profile")]
    profile_raw = yaml.safe_load(profile_config["data"]["profile.yaml"])
    profile = ApplicationProfile.model_validate(profile_raw)
    assert profile.application_id == "online-boutique"
    assert {workload.reference.name for workload in profile.workloads} >= {
        "frontend",
        "checkoutservice",
        "recommendationservice",
        "redis-cart",
    }
    redis = next(
        workload for workload in profile.workloads if workload.reference.name == "redis-cart"
    )
    assert redis.protected_dependency and not redis.executable
    assert {mapping.target.name for mapping in profile.alert_mappings} == {
        "recommendationservice",
        "redis-cart",
    }
    serialized_profile = json.dumps(profile_raw, sort_keys=True).lower()
    assert not {"recommendation_oom", "redis_outage", "25mi", "ground_truth"}.intersection(
        serialized_profile
    )

    loadgenerator = by_kind_name[("Deployment", "loadgenerator")]
    env = loadgenerator["spec"]["template"]["spec"]["containers"][0]["env"]
    env_by_name = {item["name"]: item["value"] for item in env}
    assert env_by_name["USERS"] == "10"
    assert env_by_name["RATE"] == "1"

    recommendation = by_kind_name[("Deployment", "recommendationservice")]
    recommendation_resources = recommendation["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    scenario_deployment = by_kind_name[("Deployment", "scenario-controller")]
    scenario_env = {
        item["name"]: item["value"]
        for item in scenario_deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert scenario_env["RECOMMENDATION_SAFE_MEMORY_REQUEST"] == recommendation_resources[
        "requests"
    ]["memory"]
    assert scenario_env["RECOMMENDATION_SAFE_MEMORY_LIMIT"] == recommendation_resources["limits"][
        "memory"
    ]
    assert (
        scenario_env["RECOMMENDATION_UNSAFE_MEMORY_REQUEST"]
        == scenario_env["RECOMMENDATION_UNSAFE_MEMORY_LIMIT"]
    )
    assert int(scenario_env["RECOMMENDATION_UNSAFE_MEMORY_LIMIT"].removesuffix("Mi")) < int(
        recommendation_resources["limits"]["memory"].removesuffix("Mi")
    )

    managed = {
        resource["metadata"]["name"]
        for resource in resources
        if resource["kind"] == "Deployment"
        and resource["metadata"].get("labels", {}).get("kubecouncil.io/managed") == "true"
    }
    assert "recommendationservice" in managed
    assert "redis-cart" not in managed
    assert "loadgenerator" not in managed


def test_scenario_identity_and_permissions_are_separate_and_bounded() -> None:
    resources = _render_demo()
    roles = {
        (resource["metadata"]["namespace"], resource["metadata"]["name"]): resource
        for resource in resources
        if resource["kind"] == "Role"
    }
    scenario_role = roles[("online-boutique", "scenario-controller")]
    deployment_rule = next(
        rule for rule in scenario_role["rules"] if "deployments" in rule["resources"]
    )
    assert set(deployment_rule["resourceNames"]) == {"recommendationservice", "redis-cart"}
    assert set(deployment_rule["verbs"]) == {"get", "patch"}

    investigator_role = roles[("online-boutique", "investigator-read")]
    investigator_verbs = {verb for rule in investigator_role["rules"] for verb in rule["verbs"]}
    assert not {"create", "delete", "patch", "update"}.intersection(investigator_verbs)

    scenario_deployment = next(
        resource
        for resource in resources
        if resource["kind"] == "Deployment"
        and resource["metadata"]["name"] == "scenario-controller"
    )
    assert scenario_deployment["metadata"]["namespace"] == "kubecouncil-demo-control"
    assert scenario_deployment["spec"]["template"]["spec"]["serviceAccountName"] == (
        "scenario-controller"
    )
    pod_security = scenario_deployment["spec"]["template"]["spec"]["securityContext"]
    assert pod_security["runAsNonRoot"] is True
    assert pod_security["runAsUser"] == 1000
    assert pod_security["runAsGroup"] == 1000
    assert scenario_deployment["metadata"]["labels"]["kubecouncil.io/demo-only"] == "true"


def test_non_demo_bootstrap_contains_no_scenario_controller_workload() -> None:
    completed = subprocess.run(  # noqa: S603
        (
            "kubectl",
            "kustomize",
            str(ROOT / "manifests/incident-response/bootstrap"),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [document for document in yaml.safe_load_all(completed.stdout) if document]
    assert not any(
        document["kind"] == "Deployment"
        and document["metadata"]["name"] == "scenario-controller"
        for document in documents
    )
