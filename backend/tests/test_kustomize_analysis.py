from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

from fastapi.testclient import TestClient

from app.api.repositories import get_run_store
from app.api.runs import get_manifest_renderer
from app.domain.fakes import InMemoryRunStore
from app.domain.models import RepositorySnapshot
from app.kubernetes.kustomize import (
    KustomizeManifestRenderer,
    build_compatibility_report,
    build_service_profiles,
    parse_rendered_manifest,
)
from app.main import app


def snapshot(tmp_path: Path) -> RepositorySnapshot:
    deployment = tmp_path / "repo" / "deploy" / "overlays" / "production"
    deployment.mkdir(parents=True)
    (deployment / "kustomization.yaml").write_text("apiVersion: kustomize.config.k8s.io/v1beta1\n")
    return RepositorySnapshot(
        run_id="run-1",
        repository_url="https://github.com/example/repo",
        ref="main",
        commit_sha="abcdef123456",
        workspace_path=str(tmp_path / "repo"),
        deployment_path="deploy/overlays/production",
        captured_at=datetime.now(UTC),
    )


DEMO_RENDERED_YAML = dedent(
    """
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: gateway
      namespace: shop-demo
      annotations:
        kubecouncil.io/criticality: critical
        kubecouncil.io/min-replicas: "2"
        kubecouncil.io/max-replicas: "5"
        kubecouncil.io/dependencies: checkout
        kubecouncil.io/degradation-modes: none
        kubecouncil.io/optional: "false"
    spec:
      replicas: 2
      template:
        spec:
          containers:
            - name: app
              image: kubecouncil-demo:latest
              envFrom:
                - configMapRef:
                    name: gateway-config
              resources:
                requests:
                  cpu: 200m
                  memory: 256Mi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: checkout
      namespace: shop-demo
      annotations:
        kubecouncil.io/criticality: critical
        kubecouncil.io/min-replicas: "2"
        kubecouncil.io/max-replicas: "6"
        kubecouncil.io/dependencies: payment,recommendation
        kubecouncil.io/degradation-modes: queue-admission
        kubecouncil.io/optional: "false"
    spec:
      replicas: 2
      template:
        spec:
          containers:
            - name: app
              image: kubecouncil-demo:latest
              envFrom:
                - configMapRef:
                    name: checkout-config
              resources:
                requests:
                  cpu: 450m
                  memory: 512Mi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: payment
      namespace: shop-demo
      annotations:
        kubecouncil.io/criticality: critical
        kubecouncil.io/min-replicas: "2"
        kubecouncil.io/max-replicas: "4"
        kubecouncil.io/dependencies: none
        kubecouncil.io/degradation-modes: none
        kubecouncil.io/optional: "false"
    spec:
      replicas: 2
      template:
        spec:
          containers:
            - name: app
              image: kubecouncil-demo:latest
              resources:
                requests:
                  cpu: 250m
                  memory: 256Mi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: recommendation
      namespace: shop-demo
      annotations:
        kubecouncil.io/criticality: important
        kubecouncil.io/min-replicas: "1"
        kubecouncil.io/max-replicas: "4"
        kubecouncil.io/dependencies: none
        kubecouncil.io/degradation-modes: cached
        kubecouncil.io/optional: "false"
    spec:
      replicas: 2
      template:
        spec:
          containers:
            - name: app
              image: kubecouncil-demo:latest
              envFrom:
                - configMapRef:
                    name: recommendation-config
              resources:
                requests:
                  cpu: 300m
                  memory: 256Mi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: analytics-worker
      namespace: shop-demo
      annotations:
        kubecouncil.io/criticality: optional
        kubecouncil.io/min-replicas: "0"
        kubecouncil.io/max-replicas: "1"
        kubecouncil.io/dependencies: none
        kubecouncil.io/degradation-modes: suspend
        kubecouncil.io/optional: "true"
    spec:
      replicas: 1
      template:
        spec:
          containers:
            - name: app
              image: kubecouncil-demo:latest
              envFrom:
                - configMapRef:
                    name: analytics-worker-config
              resources:
                requests:
                  cpu: 600m
                  memory: 512Mi
    ---
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    metadata:
      name: checkout
      namespace: shop-demo
    spec:
      minReplicas: 2
      maxReplicas: 6
      scaleTargetRef:
        apiVersion: apps/v1
        kind: Deployment
        name: checkout
    ---
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: checkout-config
      namespace: shop-demo
    data:
      SERVICE_NAME: checkout
    """
).strip()


def test_multi_document_yaml_parsing() -> None:
    resources = parse_rendered_manifest(DEMO_RENDERED_YAML)

    assert len(resources) == 7
    assert resources[0].kind == "Deployment"
    assert resources[0].name == "gateway"
    assert resources[0].source == "rendered.yaml#1:Deployment/gateway"


def test_kustomize_renderer_discovers_demo_services_and_annotations(tmp_path: Path) -> None:
    rendered_commands: list[tuple[str, ...]] = []

    def command_runner(command: tuple[str, ...], cwd: Path) -> str:
        rendered_commands.append(command)
        assert cwd == tmp_path / "repo"
        return DEMO_RENDERED_YAML

    repository_snapshot = snapshot(tmp_path)
    kustomization = Path(repository_snapshot.workspace_path) / repository_snapshot.deployment_path
    before = (kustomization / "kustomization.yaml").read_text()
    renderer = KustomizeManifestRenderer(command_runner=command_runner)

    source = renderer.render(repository_snapshot)
    profiles = {profile.name: profile for profile in renderer.service_profiles(source)}

    assert rendered_commands == [
        (
            "kubectl",
            "kustomize",
            str(Path(repository_snapshot.workspace_path) / "deploy/overlays/production"),
        )
    ]
    assert set(profiles) == {"gateway", "checkout", "payment", "recommendation", "analytics-worker"}
    assert profiles["checkout"].dependencies == ("payment", "recommendation")
    assert profiles["checkout"].hpa is not None
    assert profiles["checkout"].hpa.max_replicas == 6
    assert profiles["checkout"].resource_requests.cpu_millis == 450
    assert profiles["analytics-worker"].optional is True
    assert profiles["recommendation"].degradation_modes == ("cached",)
    assert profiles["checkout"].sources["resource_requests"].endswith("resources.requests")
    assert (kustomization / "kustomization.yaml").read_text() == before


def test_unsupported_resources_are_reported() -> None:
    resources = parse_rendered_manifest(
        dedent(
            """
            apiVersion: apps/v1
            kind: StatefulSet
            metadata:
              name: database
            spec: {}
            ---
            apiVersion: apiextensions.k8s.io/v1
            kind: CustomResourceDefinition
            metadata:
              name: widgets.example.com
            spec: {}
            """
        )
    )

    issues = build_compatibility_report(resources)

    assert [issue.resource_kind for issue in issues] == ["StatefulSet", "CustomResourceDefinition"]
    assert all(issue.severity == "error" for issue in issues)


def test_secret_references_are_rejected() -> None:
    resources = parse_rendered_manifest(
        dedent(
            """
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: checkout
              annotations:
                kubecouncil.io/criticality: critical
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      image: checkout:latest
                      env:
                        - name: API_TOKEN
                          valueFrom:
                            secretKeyRef:
                              name: checkout-secret
                              key: token
                  volumes:
                    - name: secret-volume
                      secret:
                        secretName: checkout-secret
            """
        )
    )

    issues = build_compatibility_report(resources)

    assert len(issues) == 2
    assert all("Secret reference" in issue.message for issue in issues)


def test_dependency_to_unknown_service_is_reported() -> None:
    resources = parse_rendered_manifest(
        dedent(
            """
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: checkout
              annotations:
                kubecouncil.io/dependencies: external-payment
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      image: checkout:latest
            """
        )
    )

    issues = build_compatibility_report(resources)

    assert len(issues) == 1
    assert "external-payment" in issues[0].message
    assert issues[0].severity == "warning"


def test_build_service_profiles_without_api() -> None:
    resources = parse_rendered_manifest(DEMO_RENDERED_YAML)
    profiles = build_service_profiles(resources)

    assert [profile.name for profile in profiles] == [
        "analytics-worker",
        "checkout",
        "gateway",
        "payment",
        "recommendation",
    ]


def test_analyse_run_api_persists_profiles_and_dependency_graph(tmp_path: Path) -> None:
    store = InMemoryRunStore()
    repository_snapshot = snapshot(tmp_path)
    store.put(repository_snapshot.run_id, "repository_snapshot", repository_snapshot)
    renderer = KustomizeManifestRenderer(command_runner=lambda _command, _cwd: DEMO_RENDERED_YAML)
    app.dependency_overrides[get_run_store] = lambda: store
    app.dependency_overrides[get_manifest_renderer] = lambda: renderer
    client = TestClient(app)

    try:
        response = client.post(f"/api/runs/{repository_snapshot.run_id}/analyse")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-1"
    assert body["source"]["rendered_resource_count"] == 7
    assert {service["name"] for service in body["services"]} == {
        "gateway",
        "checkout",
        "payment",
        "recommendation",
        "analytics-worker",
    }
    assert {"from_service": "checkout", "to_service": "payment", "required": True} in body[
        "dependency_edges"
    ]
    assert store.get("run-1", "analysis_result") is not None
    assert store.get("run-1", "service_profiles") is not None


def test_analyse_run_api_requires_connected_repository() -> None:
    app.dependency_overrides[get_run_store] = lambda: InMemoryRunStore()
    client = TestClient(app)

    try:
        response = client.post("/api/runs/missing/analyse")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "repository_snapshot_not_found"
