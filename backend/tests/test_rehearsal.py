import os
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.repositories import get_run_store
from app.api.runs import get_kubernetes_client, get_rehearsal_planner
from app.domain.fakes import FakeKubernetesClient, InMemoryRunStore
from app.domain.models import AnalysisResult, RepositorySnapshot, ValidationStatus
from app.kubernetes.client import KubectlKubernetesClient
from app.kubernetes.kustomize import build_service_profiles, parse_rendered_manifest
from app.main import app
from app.rehearsal.planner import RehearsalPlanner, RehearsalPlanningError


def snapshot(tmp_path: Path) -> RepositorySnapshot:
    workspace = tmp_path / "repo"
    deployment = workspace / "deploy" / "overlays" / "production"
    deployment.mkdir(parents=True)
    (deployment / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    return RepositorySnapshot(
        run_id="run-1",
        repository_url="https://github.com/example/repo",
        ref="main",
        commit_sha="abcdef123456",
        workspace_path=str(workspace),
        deployment_path="deploy/overlays/production",
        captured_at=datetime.now(UTC),
    )


def analysis(tmp_path: Path, rendered_yaml: str) -> AnalysisResult:
    resources = parse_rendered_manifest(rendered_yaml)
    source = snapshot(tmp_path)
    from app.domain.models import DeploymentSource

    deployment_source = DeploymentSource(
        repository=source,
        kustomization_path="deploy/overlays/production/kustomization.yaml",
        rendered_resource_count=len(resources),
        rendered_resources=resources,
    )
    return AnalysisResult(
        run_id=source.run_id,
        source=deployment_source,
        services=build_service_profiles(resources),
    )


RENDERED_YAML = dedent(
    """
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: checkout
      namespace: shop-demo
      labels:
        app: checkout
      annotations:
        kubecouncil.io/criticality: critical
        kubecouncil.io/min-replicas: "1"
        kubecouncil.io/max-replicas: "5"
    spec:
      replicas: 2
      selector:
        matchLabels:
          app: checkout
      template:
        metadata:
          labels:
            app: checkout
        spec:
          containers:
            - name: app
              image: checkout:latest
              envFrom:
                - configMapRef:
                    name: checkout-config
                - secretRef:
                    name: checkout-secret
              env:
                - name: API_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: checkout-secret
                      key: token
              resources:
                requests:
                  cpu: 250m
                  memory: 256Mi
          volumes:
            - name: token
              secret:
                secretName: checkout-secret
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: checkout
      namespace: shop-demo
    spec:
      selector:
        app: checkout
      ports:
        - port: 80
          targetPort: 8080
    ---
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: checkout-config
      namespace: shop-demo
    data:
      MODE: live
      PAYMENT_URL: http://payment.shop-demo.svc.cluster.local:8080
    ---
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: checkout
      namespace: shop-demo
    spec: {}
    ---
    apiVersion: v1
    kind: Secret
    metadata:
      name: checkout-secret
      namespace: shop-demo
    data:
      token: ZXhhbXBsZQ==
    """
).strip()


def test_planner_generates_safe_overlay_outside_source_and_leaves_source_unchanged(
    tmp_path: Path,
) -> None:
    run_analysis = analysis(tmp_path, RENDERED_YAML)
    source_file = (
        Path(run_analysis.source.repository.workspace_path) / run_analysis.source.kustomization_path
    )
    before = source_file.read_text(encoding="utf-8")
    planner = RehearsalPlanner(overlay_root=tmp_path / "overlays")

    plan = planner.build_plan(run_analysis)

    assert plan.namespace == "kc-rehearsal-run-1"
    assert plan.overlay_path is not None
    assert Path(plan.overlay_path).is_relative_to(tmp_path / "overlays")
    assert source_file.read_text(encoding="utf-8") == before
    assert {resource.kind for resource in plan.rendered_resources} == {
        "Namespace",
        "ResourceQuota",
        "Deployment",
        "Service",
        "ConfigMap",
    }
    assert all(resource.namespace == plan.namespace for resource in plan.rendered_resources)
    assert any(
        "omitted production Secret/checkout-secret" in item
        for item in plan.safety_substitutions
    )
    assert any("omitted production Ingress/checkout" in item for item in plan.safety_substitutions)

    overlay_documents = tuple(
        yaml.safe_load_all((Path(plan.overlay_path) / "resources.yaml").read_text(encoding="utf-8"))
    )
    deployment = next(
        document for document in overlay_documents if document["kind"] == "Deployment"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert "secretRef" not in str(container)
    assert "secret" not in deployment["spec"]["template"]["spec"]
    config_map = next(document for document in overlay_documents if document["kind"] == "ConfigMap")
    assert config_map["data"]["MODE"] == "cached"
    assert config_map["data"]["KUBECOUNCIL_REHEARSAL"] == "true"


def test_planner_rejects_cluster_scoped_resources(tmp_path: Path) -> None:
    run_analysis = analysis(
        tmp_path,
        dedent(
            """
            apiVersion: rbac.authorization.k8s.io/v1
            kind: ClusterRole
            metadata:
              name: unsafe
            rules: []
            """
        ),
    )

    with pytest.raises(RehearsalPlanningError, match="cluster-scoped"):
        RehearsalPlanner(overlay_root=tmp_path / "overlays").build_plan(run_analysis)


def test_planner_refuses_overlay_inside_source_repository(tmp_path: Path) -> None:
    run_analysis = analysis(tmp_path, RENDERED_YAML)
    source_root = Path(run_analysis.source.repository.workspace_path)

    with pytest.raises(RehearsalPlanningError, match="outside source repository"):
        RehearsalPlanner(overlay_root=source_root / ".scratch").build_plan(run_analysis)


def test_namespace_guard_rejects_non_rehearsal_namespaces() -> None:
    from app.domain.models import RehearsalResource

    with pytest.raises(ValidationError):
        RehearsalResource(
            api_version="apps/v1",
            kind="Deployment",
            name="checkout",
            namespace="production",
        )


def test_fake_kubernetes_deployment_and_cleanup_are_idempotent(tmp_path: Path) -> None:
    plan = RehearsalPlanner(overlay_root=tmp_path / "overlays").build_plan(
        analysis(tmp_path, RENDERED_YAML)
    )
    kubernetes = FakeKubernetesClient()

    validation = kubernetes.validate_rehearsal(plan)
    resources = kubernetes.create_rehearsal(plan)
    kubernetes.delete_rehearsal(plan.namespace)
    kubernetes.delete_rehearsal(plan.namespace)

    assert validation.status == ValidationStatus.PASSED
    assert {resource.kind for resource in resources} == {
        "Namespace",
        "ResourceQuota",
        "Deployment",
        "Service",
        "ConfigMap",
    }
    assert kubernetes.created == {}
    assert kubernetes.deleted == [plan.namespace, plan.namespace]


def test_rehearsal_api_creates_reads_and_deletes_state(tmp_path: Path) -> None:
    store = InMemoryRunStore()
    run_analysis = analysis(tmp_path, RENDERED_YAML)
    store.put(run_analysis.run_id, "analysis_result", run_analysis)
    kubernetes = FakeKubernetesClient()

    app.dependency_overrides[get_run_store] = lambda: store
    app.dependency_overrides[get_rehearsal_planner] = lambda: RehearsalPlanner(
        overlay_root=tmp_path / "overlays"
    )
    app.dependency_overrides[get_kubernetes_client] = lambda: kubernetes
    client = TestClient(app)

    try:
        created = client.post(f"/api/runs/{run_analysis.run_id}/rehearsal")
        fetched = client.get(f"/api/runs/{run_analysis.run_id}/rehearsal")
        deleted = client.delete(f"/api/runs/{run_analysis.run_id}/rehearsal")
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert created.json()["status"] == "deployed"
    assert fetched.status_code == 200
    assert fetched.json()["namespace"] == "kc-rehearsal-run-1"
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert kubernetes.deleted == ["kc-rehearsal-run-1"]


def test_rehearsal_api_requires_analysis() -> None:
    app.dependency_overrides[get_run_store] = lambda: InMemoryRunStore()
    client = TestClient(app)

    try:
        response = client.post("/api/runs/missing/rehearsal")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "analysis_not_found"


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("KUBECOUNCIL_RUN_K8S_INTEGRATION") != "1",
    reason="requires an explicit Kubernetes context opt-in",
)
def test_kubernetes_rehearsal_integration_creates_and_deletes_namespace(tmp_path: Path) -> None:
    run_analysis = analysis(
        tmp_path,
        dedent(
            """
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: rehearsal-smoke
              namespace: shop-demo
            data:
              MODE: live
            """
        ),
    )
    plan = RehearsalPlanner(overlay_root=tmp_path / "overlays").build_plan(run_analysis)
    kubernetes = KubectlKubernetesClient()

    try:
        resources = kubernetes.create_rehearsal(plan)
    finally:
        kubernetes.delete_rehearsal(plan.namespace)

    assert any(resource.kind == "ConfigMap" for resource in resources)
