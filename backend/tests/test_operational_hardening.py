import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from app.kubernetes.cleanup import CommandResult, RehearsalNamespaceCleaner
from app.main import app
from app.observability import REQUEST_ID_HEADER


class RecordingCleanupRunner:
    def __init__(self, namespace_payload: dict[str, object]) -> None:
        self.namespace_payload = namespace_payload
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        self.calls.append(tuple(arguments))
        if "get" in arguments:
            return CommandResult(returncode=0, stdout=json.dumps(self.namespace_payload), stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")


def test_health_and_readiness_expose_operational_status() -> None:
    client = TestClient(app)

    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "kubecouncil-backend"}
    assert ready.status_code == 200
    assert ready.json()["checks"]["api"] == "ok"


def test_request_id_middleware_returns_existing_or_generated_request_id() -> None:
    client = TestClient(app)

    explicit = client.get("/health", headers={REQUEST_ID_HEADER: "demo-request"})
    generated = client.get("/health")

    assert explicit.headers[REQUEST_ID_HEADER] == "demo-request"
    assert generated.headers[REQUEST_ID_HEADER]


def test_rehearsal_cleanup_deletes_only_expired_rehearsal_namespaces() -> None:
    now = datetime(2026, 7, 5, tzinfo=UTC)
    runner = RecordingCleanupRunner(
        {
            "items": [
                namespace("kc-rehearsal-old", now - timedelta(minutes=1), rehearsal=True),
                namespace("kc-rehearsal-new", now + timedelta(minutes=1), rehearsal=True),
                namespace("production", now - timedelta(days=1), rehearsal=True),
                namespace("shop-demo", now - timedelta(days=1), rehearsal=False),
            ]
        }
    )
    cleaner = RehearsalNamespaceCleaner(command_runner=runner)

    result = cleaner.delete_expired(now)

    assert result.inspected == 4
    assert result.deleted == ("kc-rehearsal-old",)
    assert runner.calls[-1] == (
        "kubectl",
        "delete",
        "namespace",
        "kc-rehearsal-old",
        "--ignore-not-found=true",
    )


def test_deployment_manifests_do_not_embed_secret_values() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest_paths = tuple((root / "manifests" / "kubecouncil" / "base").glob("*.yaml"))

    for path in manifest_paths:
        documents = tuple(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        for document in documents:
            if not isinstance(document, dict):
                continue
            assert document.get("kind") != "Secret"
            assert "stringData" not in document
            assert "data" not in document or document.get("kind") == "ConfigMap"


def namespace(name: str, expires_at: datetime, *, rehearsal: bool) -> dict[str, object]:
    labels = {"kubecouncil.io/rehearsal": "true"} if rehearsal else {}
    return {
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {"kubecouncil.io/expires-at": expires_at.isoformat()},
        }
    }
