import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_TARGET = REPO_ROOT / "demo-target"
BASE = DEMO_TARGET / "deploy" / "base"
PRODUCTION = DEMO_TARGET / "deploy" / "overlays" / "production"

REQUIRED_SERVICES = {"gateway", "checkout", "payment", "recommendation", "analytics-worker"}
REQUIRED_ANNOTATIONS = {
    "kubecouncil.io/criticality",
    "kubecouncil.io/min-replicas",
    "kubecouncil.io/max-replicas",
    "kubecouncil.io/dependencies",
    "kubecouncil.io/degradation-modes",
    "kubecouncil.io/optional",
}


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    with path.open() as file:
        return [doc for doc in yaml.safe_load_all(file) if doc]


def base_resources() -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for file_name in yaml.safe_load((BASE / "kustomization.yaml").read_text())["resources"]:
        resources.extend(load_yaml_documents(BASE / file_name))
    return resources


def deployments() -> dict[str, dict[str, Any]]:
    return {
        resource["metadata"]["name"]: resource
        for resource in base_resources()
        if resource["kind"] == "Deployment"
    }


def test_demo_service_endpoints() -> None:
    sys.path.insert(0, str(DEMO_TARGET / "app"))
    try:
        from main import DemoSettings, create_app
    finally:
        sys.path.remove(str(DEMO_TARGET / "app"))

    app = create_app(
        DemoSettings(
            service_name="gateway",
            mode="live",
            latency_ms=0,
            cpu_iterations=1,
            checkout_url="http://checkout/work",
            payment_url="http://payment/work",
            recommendation_url="http://recommendation/work",
            mock_internal_calls=True,
        ),
    )
    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok", "service": "gateway"}
    response = client.get("/work")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "gateway"
    assert body["downstream"]["checkout"]["status"] == "mocked"


def test_recommendation_cached_mode_reduces_latency() -> None:
    sys.path.insert(0, str(DEMO_TARGET / "app"))
    try:
        from main import DemoSettings, run_service
    finally:
        sys.path.remove(str(DEMO_TARGET / "app"))

    live = run_service(
        DemoSettings(
            service_name="recommendation",
            mode="live",
            latency_ms=20,
            cpu_iterations=1,
            checkout_url="",
            payment_url="",
            recommendation_url="",
            mock_internal_calls=True,
        ),
    )
    cached = run_service(
        DemoSettings(
            service_name="recommendation",
            mode="cached",
            latency_ms=20,
            cpu_iterations=1,
            checkout_url="",
            payment_url="",
            recommendation_url="",
            mock_internal_calls=True,
        ),
    )

    assert cached.latency_ms < live.latency_ms


def test_kustomize_overlay_declares_base() -> None:
    overlay = yaml.safe_load((PRODUCTION / "kustomization.yaml").read_text())
    assert overlay["resources"] == ["../../base"]
    if shutil.which("kubectl"):
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(PRODUCTION)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "kind: Deployment" in rendered.stdout


def test_production_overlay_declares_gke_image_substitution() -> None:
    overlay = yaml.safe_load((PRODUCTION / "kustomization.yaml").read_text())
    images = overlay["images"]

    assert images == [
        {
            "name": "kubecouncil-demo",
            "newName": "us-docker.pkg.dev/example-project/kubecouncil-demo/kubecouncil-demo",
            "newTag": "replace-me",
        }
    ]


def test_demo_readme_documents_gke_image_substitution() -> None:
    readme = (DEMO_TARGET / "README.md").read_text()

    assert "kustomize edit set image" in readme
    assert "kubecouncil-demo=$DEMO_IMAGE" in readme
    assert "us-docker.pkg.dev/PROJECT_ID/REPOSITORY/kubecouncil-demo:TAG" in readme


def test_rendered_resources_include_all_five_services() -> None:
    service_names = {
        resource["metadata"]["name"]
        for resource in base_resources()
        if resource["kind"] == "Service"
    }
    assert REQUIRED_SERVICES == service_names


def test_all_deployments_have_kubecouncil_annotations() -> None:
    for name, deployment in deployments().items():
        annotations = deployment["metadata"].get("annotations", {})
        assert REQUIRED_ANNOTATIONS <= annotations.keys(), name


def test_checkout_dependencies_are_declared() -> None:
    annotations = deployments()["checkout"]["metadata"]["annotations"]
    dependencies = set(annotations["kubecouncil.io/dependencies"].split(","))
    assert dependencies == {"payment", "recommendation"}


def test_source_configuration_is_close_to_quota() -> None:
    quota = next(resource for resource in base_resources() if resource["kind"] == "ResourceQuota")
    assert quota["spec"]["hard"]["requests.cpu"] == "3200m"

    total_cpu_millis = 0
    for deployment in deployments().values():
        replicas = deployment["spec"]["replicas"]
        request = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"][
            "cpu"
        ]
        total_cpu_millis += replicas * int(request.removesuffix("m"))

    assert total_cpu_millis == 3000
    assert total_cpu_millis / 3200 >= 0.9


def test_load_test_script_has_valid_javascript_syntax() -> None:
    subprocess.run(
        ["node", "--check", str(DEMO_TARGET / "load-tests" / "flash-sale.js")],
        check=True,
        capture_output=True,
        text=True,
    )
