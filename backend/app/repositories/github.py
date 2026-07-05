from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from app.domain.interfaces import RepositoryProvider
from app.domain.models import RepositoryConnection, RepositorySnapshot


class RepositoryProviderError(RuntimeError):
    """Base error for repository-provider failures safe to return to API callers."""


class InvalidRepositoryUrlError(RepositoryProviderError):
    """Raised when a repository URL is unsupported or unsafe."""


class RepositoryCloneError(RepositoryProviderError):
    """Raised when Git cannot clone or inspect the requested repository."""


class MissingDeploymentPathError(RepositoryProviderError):
    """Raised when the configured deployment path or kustomization is missing."""


class GitHubRepositoryProvider(RepositoryProvider):
    """Clones GitHub or local development repositories into isolated run workspaces."""

    def __init__(self, workspace_root: Path, git_timeout_seconds: int = 120) -> None:
        self.workspace_root = workspace_root
        self._git_timeout_seconds = git_timeout_seconds
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def connect(self, connection: RepositoryConnection, run_id: str) -> RepositorySnapshot:
        repository_url = str(connection.repository_url)
        clone_url = self._clone_url(repository_url)
        workspace = self.workspace_root / run_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)

        env = os.environ.copy()
        with self._askpass_environment(connection, env):
            self._git(
                "clone",
                "--depth",
                "1",
                "--branch",
                connection.ref,
                clone_url,
                str(workspace),
                env=env,
                token=connection.auth_token.get_secret_value() if connection.auth_token else None,
            )

        commit_sha = self._git(
            "rev-parse",
            "HEAD",
            cwd=workspace,
            token=connection.auth_token.get_secret_value() if connection.auth_token else None,
        )
        self._deployment_path(workspace, connection.deployment_path)

        return RepositorySnapshot(
            run_id=run_id,
            repository_url=connection.repository_url,
            ref=connection.ref,
            commit_sha=commit_sha,
            workspace_path=str(workspace),
            deployment_path=connection.deployment_path,
            captured_at=datetime.now(UTC),
        )

    def cleanup(self, run_id: str) -> None:
        shutil.rmtree(self.workspace_root / run_id, ignore_errors=True)

    def _clone_url(self, repository_url: str) -> str:
        parsed = urlparse(repository_url)
        if parsed.scheme == "file":
            if not parsed.path:
                raise InvalidRepositoryUrlError("file repository URL must include a path")
            return repository_url

        if parsed.scheme in {"http", "https"}:
            if parsed.netloc.lower() != "github.com":
                raise InvalidRepositoryUrlError("repository URL must be hosted on github.com")
            if len(parsed.path.strip("/").split("/")) < 2:
                raise InvalidRepositoryUrlError("repository URL must include an owner and repo")
            return repository_url

        if parsed.scheme == "ssh":
            if parsed.netloc.lower() != "git@github.com":
                raise InvalidRepositoryUrlError("ssh repository URL must use git@github.com")
            return repository_url

        raise InvalidRepositoryUrlError("repository URL must be a GitHub or file URL")

    def _deployment_path(self, workspace: Path, deployment_path: str) -> Path:
        path = (workspace / deployment_path).resolve()
        if not path.is_relative_to(workspace.resolve()):
            raise MissingDeploymentPathError("deployment path must stay inside the workspace")
        if not path.is_dir():
            raise MissingDeploymentPathError("deployment path does not exist")
        if not (path / "kustomization.yaml").is_file():
            raise MissingDeploymentPathError("deployment path must contain kustomization.yaml")
        return path

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        token: str | None = None,
    ) -> str:
        command = ["git", *args]
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._git_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepositoryCloneError(
                f"git command timed out after {self._git_timeout_seconds}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = self._redact(exc.stderr, token)
            stdout = self._redact(exc.stdout, token)
            detail = stderr or stdout or "git command failed"
            raise RepositoryCloneError(detail) from exc
        return result.stdout.strip()

    def _askpass_environment(
        self,
        connection: RepositoryConnection,
        env: dict[str, str],
    ) -> TemporaryDirectory[str]:
        temp_dir = TemporaryDirectory()
        token = connection.auth_token.get_secret_value() if connection.auth_token else None
        if token:
            askpass = Path(temp_dir.name) / "askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
                '  *Password*) printf "%s\\n" "$KUBECOUNCIL_GIT_TOKEN" ;;\n'
                '  *) printf "\\n" ;;\n'
                "esac\n",
            )
            askpass.chmod(askpass.stat().st_mode | stat.S_IXUSR)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["KUBECOUNCIL_GIT_TOKEN"] = token
        return temp_dir

    def _redact(self, value: str, token: str | None) -> str:
        if not token:
            return value
        return value.replace(token, "<redacted>")
