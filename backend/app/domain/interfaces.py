from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from app.domain.models import (
    CouncilPlan,
    DeploymentSource,
    ExperimentReport,
    LoadTestResult,
    PullRequestResult,
    RehearsalPlan,
    RehearsalResource,
    RepositoryConnection,
    RepositorySnapshot,
    ResourceRequests,
    ScenarioSpec,
    ServiceProfile,
    ValidationResult,
)


class RepositoryProvider(Protocol):
    """Accesses source repositories without leaking GitHub details into domain code."""

    def connect(self, connection: RepositoryConnection, run_id: str) -> RepositorySnapshot:
        ...

    def cleanup(self, run_id: str) -> None:
        ...


class ManifestRenderer(Protocol):
    """Renders a deployment source into structured resources and service profiles."""

    def render(self, snapshot: RepositorySnapshot) -> DeploymentSource:
        ...

    def service_profiles(self, source: DeploymentSource) -> Sequence[ServiceProfile]:
        ...


class KubernetesClient(Protocol):
    """Performs namespace-scoped rehearsal operations."""

    def create_rehearsal(self, plan: RehearsalPlan) -> tuple[RehearsalResource, ...]:
        ...

    def validate_rehearsal(self, plan: RehearsalPlan) -> ValidationResult:
        ...

    def delete_rehearsal(self, namespace: str) -> None:
        ...


class RunStore(Protocol):
    """Stores run-scoped state for workflow recovery and API reads."""

    def put(self, run_id: str, key: str, value: object) -> None:
        ...

    def get(self, run_id: str, key: str) -> object | None:
        ...


class LoadTestRunner(Protocol):
    """Runs a scenario against a rehearsal namespace."""

    def run(self, namespace: str, scenario: ScenarioSpec, phase: str) -> LoadTestResult:
        ...


class CouncilRunner(Protocol):
    """Produces a structured council plan from profiles and measured pressure."""

    def run(
        self,
        namespace: str,
        services: Sequence[ServiceProfile],
        scenario: ScenarioSpec,
        pressure_result: LoadTestResult,
        *,
        run_id: str = "unknown-run",
        resource_quota: ResourceRequests | None = None,
    ) -> CouncilPlan:
        ...


class PullRequestProvider(Protocol):
    """Publishes validated repository changes as a draft pull request."""

    def open_draft_pull_request(
        self,
        snapshot: RepositorySnapshot,
        report: ExperimentReport,
        changed_files: Mapping[Path, str],
    ) -> PullRequestResult:
        ...
