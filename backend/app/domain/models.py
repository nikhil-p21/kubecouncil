from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class KubeCouncilModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompatibilitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class RehearsalStatus(StrEnum):
    PLANNED = "planned"
    DEPLOYED = "deployed"
    FAILED = "failed"
    DELETED = "deleted"


class LoadTestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class LoadTestFailureType(StrEnum):
    OBJECTIVE = "objective"
    INFRASTRUCTURE = "infrastructure"
    MALFORMED_OUTPUT = "malformed_output"


class ExperimentStatus(StrEnum):
    SUCCESSFUL = "successful"
    UNSUCCESSFUL = "unsuccessful"
    INCONCLUSIVE = "inconclusive"


class CouncilPlanStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    INFEASIBLE = "infeasible"


CouncilActionType = Literal[
    "scale_deployment",
    "set_hpa_bounds",
    "set_resource_requests",
    "set_config_mode",
    "suspend_optional_deployment",
    "restore_deployment",
]


def _validate_rehearsal_namespace(namespace: str) -> str:
    if not namespace.startswith("kc-rehearsal-"):
        raise ValueError("namespace must begin with kc-rehearsal-")
    return namespace


class RepositoryConnection(KubeCouncilModel):
    repository_url: AnyUrl
    ref: str = Field(min_length=1)
    deployment_path: str = Field(min_length=1)
    auth_token_name: str | None = None
    auth_token: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("deployment_path")
    @classmethod
    def reject_path_traversal(cls, value: str) -> str:
        parts = value.replace("\\", "/").split("/")
        if value.startswith("/") or ".." in parts:
            raise ValueError("deployment_path must be a relative path inside the repository")
        return value


class RepositorySnapshot(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    repository_url: AnyUrl
    ref: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7)
    workspace_path: str = Field(min_length=1)
    deployment_path: str = Field(min_length=1)
    captured_at: datetime


class CompatibilityIssue(KubeCouncilModel):
    severity: CompatibilitySeverity
    resource_kind: str = Field(min_length=1)
    resource_name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source: str = Field(min_length=1)


class ManifestResource(KubeCouncilModel):
    api_version: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    namespace: str | None = None
    source: str = Field(min_length=1)
    content: dict[str, Any]


class DeploymentSource(KubeCouncilModel):
    repository: RepositorySnapshot
    kustomization_path: str = Field(min_length=1)
    rendered_resource_count: int = Field(ge=0)
    rendered_resources: tuple[ManifestResource, ...] = ()
    compatibility_issues: tuple[CompatibilityIssue, ...] = ()


class DependencyEdge(KubeCouncilModel):
    from_service: str = Field(min_length=1)
    to_service: str = Field(min_length=1)
    required: bool = True


class ResourceRequests(KubeCouncilModel):
    cpu_millis: int = Field(ge=0)
    memory_mib: int = Field(ge=0)


class HpaBounds(KubeCouncilModel):
    min_replicas: int = Field(ge=0)
    max_replicas: int = Field(ge=1)

    @model_validator(mode="after")
    def max_covers_min(self) -> "HpaBounds":
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be greater than or equal to min_replicas")
        return self


class ServiceProfile(KubeCouncilModel):
    name: str = Field(min_length=1)
    image: str = Field(min_length=1)
    current_replicas: int = Field(ge=0)
    min_replicas: int = Field(ge=0)
    max_replicas: int = Field(ge=1)
    resource_requests: ResourceRequests
    criticality: Literal["critical", "important", "optional"]
    dependencies: tuple[str, ...] = ()
    degradation_modes: tuple[str, ...] = ()
    optional: bool = False
    config_maps: tuple[str, ...] = ()
    hpa: HpaBounds | None = None
    namespace: str | None = None
    sources: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def replica_bounds_are_consistent(self) -> "ServiceProfile":
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be greater than or equal to min_replicas")
        if self.current_replicas < self.min_replicas:
            raise ValueError("current_replicas must not be below min_replicas")
        if self.current_replicas > self.max_replicas:
            raise ValueError("current_replicas must not exceed max_replicas")
        return self


class RehearsalPlan(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    namespace: str
    source: DeploymentSource
    services: tuple[ServiceProfile, ...]
    compatibility_issues: tuple[CompatibilityIssue, ...] = ()
    resource_quota_cpu_millis: int = Field(gt=0)
    resource_quota_memory_mib: int = Field(gt=0)
    overlay_path: str | None = None
    rendered_resources: tuple[ManifestResource, ...] = ()
    safety_substitutions: tuple[str, ...] = ()

    @field_validator("namespace")
    @classmethod
    def namespace_is_rehearsal_only(cls, value: str) -> str:
        return _validate_rehearsal_namespace(value)


class ValidationResult(KubeCouncilModel):
    status: ValidationStatus
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class RehearsalResource(KubeCouncilModel):
    api_version: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    namespace: str

    @field_validator("namespace")
    @classmethod
    def namespace_is_rehearsal_only(cls, value: str) -> str:
        return _validate_rehearsal_namespace(value)


class RehearsalState(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    namespace: str
    status: RehearsalStatus
    plan: RehearsalPlan
    resources: tuple[RehearsalResource, ...] = ()
    readiness: ValidationResult
    message: str | None = None

    @field_validator("namespace")
    @classmethod
    def namespace_is_rehearsal_only(cls, value: str) -> str:
        return _validate_rehearsal_namespace(value)


class AnalysisResult(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    source: DeploymentSource
    services: tuple[ServiceProfile, ...]
    compatibility_issues: tuple[CompatibilityIssue, ...] = ()
    dependency_edges: tuple[DependencyEdge, ...] = ()


class ScenarioObjective(KubeCouncilModel):
    success_rate_minimum: float = Field(ge=0, le=1)
    p95_latency_ms_maximum: int = Field(gt=0)


class ScenarioSpec(KubeCouncilModel):
    name: str = Field(min_length=1)
    baseline_virtual_users: int = Field(gt=0)
    pressure_virtual_users: int = Field(gt=0)
    duration_seconds: int = Field(gt=0)
    objective: ScenarioObjective


class LoadTestResult(KubeCouncilModel):
    scenario_name: str = Field(min_length=1)
    phase: Literal["baseline", "pressure", "post_change"]
    request_count: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    p95_latency_ms: float = Field(ge=0)
    errors: tuple[str, ...] = ()
    status: LoadTestStatus | None = None
    failure_type: LoadTestFailureType | None = None


class ScenarioResults(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    scenario: ScenarioSpec
    baseline: LoadTestResult | None = None
    pressure: LoadTestResult | None = None
    post_change: LoadTestResult | None = None


class CouncilAction(KubeCouncilModel):
    action_type: CouncilActionType
    target_service: str = Field(min_length=1)
    target_namespace: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)

    @field_validator("target_namespace")
    @classmethod
    def target_is_rehearsal_namespace(cls, value: str) -> str:
        return _validate_rehearsal_namespace(value)


class ScaleDeploymentParameters(KubeCouncilModel):
    replicas: int = Field(ge=0)


class SetHpaBoundsParameters(KubeCouncilModel):
    min_replicas: int = Field(ge=0)
    max_replicas: int = Field(ge=1)

    @model_validator(mode="after")
    def max_covers_min(self) -> "SetHpaBoundsParameters":
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be greater than or equal to min_replicas")
        return self


class SetResourceRequestsParameters(KubeCouncilModel):
    cpu_millis: int = Field(ge=0)
    memory_mib: int = Field(ge=0)


class SetConfigModeParameters(KubeCouncilModel):
    mode: str = Field(min_length=1)


class SuspendOptionalDeploymentParameters(KubeCouncilModel):
    pass


class RestoreDeploymentParameters(KubeCouncilModel):
    pass


class ServiceProposal(KubeCouncilModel):
    service_name: str = Field(min_length=1)
    proposed_actions: tuple[CouncilAction, ...]
    rationale: str = Field(min_length=1)


class CouncilPlan(KubeCouncilModel):
    plan_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    namespace: str
    actions: tuple[CouncilAction, ...]
    validation: ValidationResult
    status: CouncilPlanStatus = CouncilPlanStatus.VALID
    representative_proposals: tuple[ServiceProposal, ...] = ()
    repair_attempted: bool = False
    infeasible_reason: str | None = None

    @field_validator("namespace")
    @classmethod
    def namespace_is_rehearsal_only(cls, value: str) -> str:
        return _validate_rehearsal_namespace(value)


class ExperimentReport(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    status: ExperimentStatus
    baseline: LoadTestResult
    pressure_before: LoadTestResult
    pressure_after: LoadTestResult
    validation: ValidationResult
    applied_actions: tuple[CouncilAction, ...]
    rollback_guidance: str = Field(min_length=1)


class ExperimentAudit(KubeCouncilModel):
    summary: str = Field(min_length=1)
    severe_regressions: tuple[str, ...] = ()
    recommendation: Literal["approve", "reject", "inconclusive"]


class ServiceRuntimeState(KubeCouncilModel):
    service_name: str = Field(min_length=1)
    replicas: int = Field(ge=0)
    hpa: HpaBounds | None = None
    resource_requests: ResourceRequests
    config_values: dict[str, str] = Field(default_factory=dict)


class CouncilWorkloadSnapshot(KubeCouncilModel):
    namespace: str
    services: tuple[ServiceRuntimeState, ...]

    @field_validator("namespace")
    @classmethod
    def namespace_is_rehearsal_only(cls, value: str) -> str:
        return _validate_rehearsal_namespace(value)


class PullRequestResult(KubeCouncilModel):
    run_id: str = Field(min_length=1)
    branch_name: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7)
    pr_url: AnyUrl
    draft: bool
    changed_files: tuple[str, ...]

    @field_validator("draft")
    @classmethod
    def pull_requests_are_always_draft(cls, value: bool) -> bool:
        if not value:
            raise ValueError("pull requests must be opened as drafts")
        return value
