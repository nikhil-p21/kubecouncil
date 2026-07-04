from pathlib import Path

import pytest

from app.domain.models import RepositoryConnection
from app.repositories.github import (
    GitHubRepositoryProvider,
    InvalidRepositoryUrlError,
    MissingDeploymentPathError,
    RepositoryCloneError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    (path / "README.md").write_text("main branch\n")
    run_git(path, "add", ".")
    run_git(path, "commit", "-m", "initial")
    return path


def provider(tmp_path: Path) -> GitHubRepositoryProvider:
    return GitHubRepositoryProvider(tmp_path / "workspaces")


def connection(repository: Path, ref: str = "main") -> RepositoryConnection:
    return RepositoryConnection(
        repository_url=repository.as_uri(),
        ref=ref,
        deployment_path="deploy/overlays/production",
    )


def test_clone_local_repository_resolves_metadata(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    repository_provider = provider(tmp_path)

    snapshot = repository_provider.connect(connection(repository), "run-1")

    assert snapshot.run_id == "run-1"
    assert snapshot.commit_sha == run_git(repository, "rev-parse", "HEAD")
    assert Path(snapshot.workspace_path).is_dir()
    assert Path(snapshot.workspace_path) == tmp_path / "workspaces" / "run-1"
    kustomization = Path(snapshot.workspace_path) / "deploy/overlays/production/kustomization.yaml"
    assert kustomization.is_file()


def test_clone_local_bare_repository(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    bare_repository = tmp_path / "source.git"
    run_git(tmp_path, "clone", "--bare", str(repository), str(bare_repository))

    snapshot = provider(tmp_path).connect(connection(bare_repository), "run-bare")

    assert snapshot.commit_sha == run_git(repository, "rev-parse", "HEAD")
    kustomization = Path(snapshot.workspace_path) / "deploy/overlays/production/kustomization.yaml"
    assert kustomization.is_file()


def test_demo_target_can_be_inspected_from_local_repository(tmp_path: Path) -> None:
    demo_connection = RepositoryConnection(
        repository_url=REPO_ROOT.as_uri(),
        ref="main",
        deployment_path="demo-target/deploy/overlays/production",
    )

    snapshot = provider(tmp_path).connect(demo_connection, "run-demo")

    assert snapshot.deployment_path == "demo-target/deploy/overlays/production"
    kustomization = Path(snapshot.workspace_path) / snapshot.deployment_path / "kustomization.yaml"
    assert kustomization.is_file()


def test_workspaces_are_isolated_by_run_id(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    repository_provider = provider(tmp_path)

    first = repository_provider.connect(connection(repository), "run-one")
    second = repository_provider.connect(connection(repository), "run-two")

    assert first.workspace_path != second.workspace_path
    assert Path(first.workspace_path).is_dir()
    assert Path(second.workspace_path).is_dir()


def test_clone_specified_branch(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    run_git(repository, "checkout", "-b", "feature")
    (repository / "README.md").write_text("feature branch\n")
    run_git(repository, "add", "README.md")
    run_git(repository, "commit", "-m", "feature")
    expected_sha = run_git(repository, "rev-parse", "HEAD")
    run_git(repository, "checkout", "main")

    snapshot = provider(tmp_path).connect(connection(repository, "feature"), "run-branch")

    assert snapshot.ref == "feature"
    assert snapshot.commit_sha == expected_sha
    assert (Path(snapshot.workspace_path) / "README.md").read_text() == "feature branch\n"


def test_reject_non_github_remote_in_github_mode(tmp_path: Path) -> None:
    repository_provider = provider(tmp_path)
    bad_connection = RepositoryConnection(
        repository_url="https://gitlab.com/example/repo",
        ref="main",
        deployment_path="deploy",
    )

    with pytest.raises(InvalidRepositoryUrlError, match="github.com"):
        repository_provider.connect(bad_connection, "run-bad")


def test_reject_path_traversal() -> None:
    with pytest.raises(ValueError, match="relative path"):
        RepositoryConnection(
            repository_url="https://github.com/example/repo",
            ref="main",
            deployment_path="../deploy",
        )


def test_missing_deployment_path_is_structured_error(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    bad_connection = RepositoryConnection(
        repository_url=repository.as_uri(),
        ref="main",
        deployment_path="missing",
    )

    with pytest.raises(MissingDeploymentPathError, match="does not exist"):
        provider(tmp_path).connect(bad_connection, "run-missing")


def test_missing_kustomization_is_structured_error(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    (repository / "deploy" / "plain").mkdir()
    (repository / "deploy" / "plain" / "README.md").write_text("missing kustomization\n")
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "add plain deploy path")
    bad_connection = RepositoryConnection(
        repository_url=repository.as_uri(),
        ref="main",
        deployment_path="deploy/plain",
    )

    with pytest.raises(MissingDeploymentPathError, match="kustomization.yaml"):
        provider(tmp_path).connect(bad_connection, "run-no-kustomization")


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "source")
    repository_provider = provider(tmp_path)
    snapshot = repository_provider.connect(connection(repository), "run-cleanup")

    repository_provider.cleanup("run-cleanup")
    repository_provider.cleanup("run-cleanup")

    assert not Path(snapshot.workspace_path).exists()


def test_clone_error_redacts_token(tmp_path: Path) -> None:
    repository_provider = provider(tmp_path)
    secret = "ghp_super_secret_token"
    bad_connection = RepositoryConnection(
        repository_url=(tmp_path / "missing").as_uri(),
        ref="main",
        deployment_path="deploy",
        auth_token=secret,
    )

    with pytest.raises(RepositoryCloneError) as exc_info:
        repository_provider.connect(bad_connection, "run-secret")

    assert secret not in str(exc_info.value)
