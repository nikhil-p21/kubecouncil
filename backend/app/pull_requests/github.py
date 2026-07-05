from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.domain.interfaces import PullRequestProvider
from app.domain.models import ExperimentReport, PullRequestResult, RepositorySnapshot
from app.scenarios.k6 import FLASH_SALE_SCENARIO


class PullRequestProviderError(RuntimeError):
    """Base error for draft pull-request publication failures."""


class PullRequestBranchExistsError(PullRequestProviderError):
    """Raised when publishing would overwrite an existing branch."""


class GitHubPullRequestClient(Protocol):
    """Creates draft GitHub pull requests without exposing HTTP details to the provider."""

    def create_draft_pull_request(
        self,
        repository_url: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        ...


class UrlLibGitHubPullRequestClient:
    """Minimal GitHub REST API client using a token from the environment."""

    def __init__(self, token_env: str = "GITHUB_TOKEN") -> None:
        self._token_env = token_env

    def create_draft_pull_request(
        self,
        repository_url: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        token = os.environ.get(self._token_env)
        if not token:
            raise PullRequestProviderError(f"{self._token_env} is required to open GitHub PRs")
        owner, repo = _github_owner_repo(repository_url)
        request = Request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            data=json.dumps(
                {
                    "title": title,
                    "head": head,
                    "base": base,
                    "body": body,
                    "draft": True,
                }
            ).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode())
        html_url = payload.get("html_url")
        if not isinstance(html_url, str) or not html_url:
            raise PullRequestProviderError("GitHub response did not include a pull request URL")
        return html_url


class GitPullRequestProvider(PullRequestProvider):
    """Commits validated source changes and opens a draft PR when using GitHub."""

    def __init__(
        self,
        github: GitHubPullRequestClient | None = None,
        *,
        push: bool = True,
    ) -> None:
        self._github = github or UrlLibGitHubPullRequestClient()
        self._push = push

    def open_draft_pull_request(
        self,
        snapshot: RepositorySnapshot,
        report: ExperimentReport,
        changed_files: Mapping[Path, str],
    ) -> PullRequestResult:
        workspace = Path(snapshot.workspace_path)
        branch_name = f"kubecouncil/rehearsal-{report.run_id}"
        self._ensure_branch_available(workspace, branch_name)

        self._git(workspace, "checkout", "-b", branch_name)
        self._git(workspace, "add", *tuple(path.as_posix() for path in changed_files))
        self._git(
            workspace,
            "-c",
            "user.name=KubeCouncil",
            "-c",
            "user.email=kubecouncil@example.invalid",
            "commit",
            "-m",
            f"chore: apply kubecouncil rehearsal {report.run_id}",
        )
        commit_sha = self._git(workspace, "rev-parse", "HEAD")

        body = build_pull_request_body(snapshot, report, changed_files)
        title = f"KubeCouncil rehearsal {report.run_id}"
        repository_url = str(snapshot.repository_url)
        if _is_github_repository(repository_url):
            if self._push:
                self._git(workspace, "push", "origin", branch_name)
            pr_url = self._github.create_draft_pull_request(
                repository_url,
                branch_name,
                snapshot.ref,
                title,
                body,
            )
        else:
            pr_url = f"{workspace.as_uri()}#draft-pr/{branch_name}"

        return PullRequestResult(
            run_id=report.run_id,
            branch_name=branch_name,
            commit_sha=commit_sha,
            pr_url=pr_url,
            draft=True,
            changed_files=tuple(path.as_posix() for path in changed_files),
        )

    def _ensure_branch_available(self, workspace: Path, branch_name: str) -> None:
        local = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=workspace,
            check=False,
        )
        if local.returncode == 0:
            raise PullRequestBranchExistsError(f"branch already exists: {branch_name}")

        has_origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        if has_origin.returncode != 0:
            return

        remote = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch_name],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        if remote.returncode == 0:
            raise PullRequestBranchExistsError(f"remote branch already exists: {branch_name}")
        if remote.returncode not in {0, 2}:
            detail = remote.stderr.strip() or "could not inspect remote branches"
            raise PullRequestProviderError(detail)

    def _git(self, workspace: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or "git command failed"
            raise PullRequestProviderError(detail) from exc
        return result.stdout.strip()


def build_pull_request_body(
    snapshot: RepositorySnapshot,
    report: ExperimentReport,
    changed_files: Mapping[Path, str],
) -> str:
    scenario = FLASH_SALE_SCENARIO
    changed = "\n".join(f"- `{path.as_posix()}`" for path in changed_files)
    actions = "\n".join(
        f"- `{action.action_type}` on `{action.target_service}`: {action.reason}"
        for action in report.applied_actions
    )
    validation = "\n".join(f"- {error}" for error in report.validation.errors) or "- passed"
    warnings = "\n".join(f"- {warning}" for warning in report.validation.warnings) or "- none"
    return "\n".join(
        (
            "This draft PR was generated from a KubeCouncil rehearsal and requires human review.",
            "",
            "## Scenario objective",
            f"- Scenario: `{scenario.name}`",
            f"- Success rate minimum: {scenario.objective.success_rate_minimum:.2f}",
            f"- P95 latency maximum: {scenario.objective.p95_latency_ms_maximum} ms",
            "",
            "## Source",
            f"- Source commit: `{snapshot.commit_sha}`",
            f"- Rehearsal namespace: `{_namespace_from_report(report)}`",
            "",
            "## Metrics",
            f"- Baseline: {report.baseline.success_rate:.3f} success, "
            f"{report.baseline.p95_latency_ms:.0f} ms p95",
            f"- Pressure before: {report.pressure_before.success_rate:.3f} success, "
            f"{report.pressure_before.p95_latency_ms:.0f} ms p95",
            f"- Pressure after: {report.pressure_after.success_rate:.3f} success, "
            f"{report.pressure_after.p95_latency_ms:.0f} ms p95",
            "",
            "## Council agreement",
            actions or "- no applied actions recorded",
            "",
            "## Changed files",
            changed or "- none",
            "",
            "## Rationale",
            actions or "- no repository-applicable action rationale was recorded",
            "",
            "## Validation results",
            validation,
            "",
            "## Risks",
            warnings,
            "- Production impact still requires human review before merge.",
            "",
            "## Rollback guidance",
            report.rollback_guidance,
        )
    )


def _namespace_from_report(report: ExperimentReport) -> str:
    for action in report.applied_actions:
        return action.target_namespace
    return "unknown"


def _is_github_repository(repository_url: str) -> bool:
    parsed = urlparse(repository_url)
    return parsed.netloc.lower() == "github.com" or parsed.netloc.lower() == "git@github.com"


def _github_owner_repo(repository_url: str) -> tuple[str, str]:
    parsed = urlparse(repository_url)
    parts: Sequence[str]
    if parsed.scheme == "ssh":
        parts = parsed.path.strip("/").split("/")
    else:
        parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise PullRequestProviderError("GitHub repository URL must include owner and repo")
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo
