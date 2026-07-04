from pathlib import Path

from fastapi.testclient import TestClient

from app.api.repositories import get_repository_provider, get_run_store
from app.domain.fakes import InMemoryRunStore
from app.main import app
from app.repositories.github import GitHubRepositoryProvider


def run_git(repository: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repository(path: Path) -> Path:
    path.mkdir()
    run_git(path, "init", "--initial-branch", "main")
    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "KubeCouncil Test")
    deployment = path / "deploy" / "overlays" / "production"
    deployment.mkdir(parents=True)
    (deployment / "kustomization.yaml").write_text("apiVersion: kustomize.config.k8s.io/v1beta1\n")
    run_git(path, "add", ".")
    run_git(path, "commit", "-m", "initial")
    return path


def test_connect_repository_api_clones_and_stores_snapshot(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    store = InMemoryRunStore()
    provider = GitHubRepositoryProvider(tmp_path / "workspaces")
    app.dependency_overrides[get_repository_provider] = lambda: provider
    app.dependency_overrides[get_run_store] = lambda: store
    client = TestClient(app)

    try:
        response = client.post(
            "/api/repositories/connect",
            json={
                "repository_url": repository.as_uri(),
                "ref": "main",
                "deployment_path": "deploy/overlays/production",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["run_id"].startswith("run-")
    assert body["deployment_path"] == "deploy/overlays/production"
    assert Path(body["workspace_path"]).is_dir()
    assert store.get(body["run_id"], "repository_snapshot") is not None


def test_connect_repository_api_rejects_invalid_remote() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/repositories/connect",
        json={
            "repository_url": "https://gitlab.com/example/repo",
            "ref": "main",
            "deployment_path": "deploy",
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_repository_url"
    assert "github.com" in detail["message"]


def test_connect_repository_api_returns_structured_missing_path_error(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    provider = GitHubRepositoryProvider(tmp_path / "workspaces")
    app.dependency_overrides[get_repository_provider] = lambda: provider
    client = TestClient(app)

    try:
        response = client.post(
            "/api/repositories/connect",
            json={
                "repository_url": repository.as_uri(),
                "ref": "main",
                "deployment_path": "missing",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "missing_deployment_path"
    assert "does not exist" in detail["message"]
