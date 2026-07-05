from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.interfaces import (
    CouncilRunner,
    KubernetesClient,
    LoadTestRunner,
    ManifestRenderer,
    PullRequestProvider,
    RepositoryProvider,
    RunStore,
)
from app.domain.models import (
    CouncilAction,
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
    ValidationStatus,
)


class FakeRepositoryProvider(RepositoryProvider):
    def __init__(self) -> None:
        self.snapshots: dict[str, RepositorySnapshot] = {}
        self.cleaned_runs: set[str] = set()

    def connect(self, connection: RepositoryConnection, run_id: str) -> RepositorySnapshot:
        snapshot = RepositorySnapshot(
            run_id=run_id,
            repository_url=connection.repository_url,
            ref=connection.ref,
            commit_sha="abcdef1234567890",
            workspace_path=f"/tmp/kubecouncil/{run_id}",
            deployment_path=connection.deployment_path,
            captured_at=datetime.now(UTC),
        )
        self.snapshots[run_id] = snapshot
        return snapshot

    def cleanup(self, run_id: str) -> None:
        self.cleaned_runs.add(run_id)
        self.snapshots.pop(run_id, None)


class FakeManifestRenderer(ManifestRenderer):
    def __init__(self, profiles: Sequence[ServiceProfile] = ()) -> None:
        self._profiles = tuple(profiles)

    def render(self, snapshot: RepositorySnapshot) -> DeploymentSource:
        return DeploymentSource(
            repository=snapshot,
            kustomization_path=f"{snapshot.deployment_path}/kustomization.yaml",
            rendered_resource_count=len(self._profiles),
        )

    def service_profiles(self, source: DeploymentSource) -> Sequence[ServiceProfile]:
        return self._profiles


class FakeKubernetesClient(KubernetesClient):
    def __init__(self) -> None:
        self.created: dict[str, RehearsalPlan] = {}
        self.deleted: list[str] = []
        self.validated: list[str] = []
        self.fail_validation = False

    def create_rehearsal(self, plan: RehearsalPlan) -> tuple[RehearsalResource, ...]:
        self.created[plan.namespace] = plan
        return tuple(
            RehearsalResource(
                api_version=resource.api_version,
                kind=resource.kind,
                name=resource.name,
                namespace=plan.namespace,
            )
            for resource in plan.rendered_resources
        )

    def validate_rehearsal(self, plan: RehearsalPlan) -> ValidationResult:
        self.validated.append(plan.namespace)
        if self.fail_validation:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                errors=("fake validation failure",),
            )
        return ValidationResult(status=ValidationStatus.PASSED)

    def delete_rehearsal(self, namespace: str) -> None:
        self.deleted.append(namespace)
        self.created.pop(namespace, None)


class InMemoryRunStore(RunStore):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], object] = {}

    def put(self, run_id: str, key: str, value: object) -> None:
        self._items[(run_id, key)] = value

    def get(self, run_id: str, key: str) -> object | None:
        return self._items.get((run_id, key))


class FakeLoadTestRunner(LoadTestRunner):
    def __init__(self, result: LoadTestResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def run(self, namespace: str, scenario: ScenarioSpec, phase: str) -> LoadTestResult:
        self.calls.append((namespace, scenario.name, phase))
        return self.result


class FakeCouncilRunner(CouncilRunner):
    def __init__(self, actions: Sequence[CouncilAction] = ()) -> None:
        self.actions = tuple(actions)

    def run(
        self,
        namespace: str,
        services: Sequence[ServiceProfile],
        scenario: ScenarioSpec,
        pressure_result: LoadTestResult,
        *,
        run_id: str = "fake-run",
        resource_quota: ResourceRequests | None = None,
    ) -> CouncilPlan:
        return CouncilPlan(
            plan_id="fake-plan",
            run_id=run_id,
            namespace=namespace,
            actions=self.actions,
            validation=ValidationResult(status=ValidationStatus.PASSED),
        )


class FakePullRequestProvider(PullRequestProvider):
    def __init__(self) -> None:
        self.requests: list[tuple[RepositorySnapshot, ExperimentReport, Mapping[Path, str]]] = []

    def open_draft_pull_request(
        self,
        snapshot: RepositorySnapshot,
        report: ExperimentReport,
        changed_files: Mapping[Path, str],
    ) -> PullRequestResult:
        self.requests.append((snapshot, report, changed_files))
        return PullRequestResult(
            run_id=report.run_id,
            branch_name=f"kubecouncil/rehearsal-{report.run_id}",
            commit_sha="1234567890abcdef",
            pr_url="https://github.com/example/repo/pull/1",
            draft=True,
            changed_files=tuple(str(path) for path in changed_files),
        )


def fake_store_value(store: RunStore, run_id: str, key: str) -> Any:
    return store.get(run_id, key)
