"""Strict, provider-independent contracts for the incident-response workflow."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import Field, computed_field, model_validator

from app.domain.models import KubeCouncilModel


class WorkloadCriticality(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    OPTIONAL = "optional"


class IncidentLifecycle(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    MITIGATING = "mitigating"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    CLOSED = "closed"


class InvestigationOutcome(StrEnum):
    NOT_STARTED = "not_started"
    PROPOSAL_READY = "proposal_ready"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"
    NO_SAFE_ACTION = "no_safe_action"
    INCONCLUSIVE = "inconclusive"


class InterventionOutcome(StrEnum):
    NOT_STARTED = "not_started"
    MONITORING = "monitoring"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    SAFE_HALTED = "safe_halted"


class EvidenceSource(StrEnum):
    KUBERNETES = "kubernetes"
    CLOUD_LOGGING = "cloud_logging"
    CLOUD_MONITORING = "cloud_monitoring"
    MANUAL = "manual"


class SpecialistRole(StrEnum):
    HEALTH = "health"
    LOGS = "logs"
    METRICS = "metrics"
    CHANGE = "change"


class SpecialistRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class EvidenceQueryKind(StrEnum):
    WORKLOAD_STATE = "workload_state"
    POD_EVENTS = "pod_events"
    POD_LOGS = "pod_logs"
    METRICS = "metrics"
    CHANGE_HISTORY = "change_history"
    ALERT_POLICY = "alert_policy"


class EnrollmentCheckCode(StrEnum):
    PROFILE_VALID = "profile_valid"
    NAMESPACE_SELECTED = "namespace_selected"
    NAMESPACE_ENROLLED_LABEL = "namespace_enrolled_label"
    INVESTIGATOR_ROLE_BINDING = "investigator_role_binding"
    EXECUTOR_ROLE_BINDING = "executor_role_binding"
    WORKLOAD_SELECTED = "workload_selected"
    MANAGED_WORKLOAD_LABEL = "managed_workload_label"
    ADMISSION_POLICY_BINDING = "admission_policy_binding"


class PolicyStatus(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    DRY_RUN_FAILED = "dry_run_failed"


class PolicyCheckCode(StrEnum):
    PROPOSAL_SCOPED = "proposal_scoped"
    EVIDENCE_CURRENT = "evidence_current"
    ENROLLMENT_READY = "enrollment_ready"
    TARGET_ENROLLED = "target_enrolled"
    TARGET_EXECUTABLE = "target_executable"
    ACTION_ALLOWED = "action_allowed"
    LIVE_STATE_AVAILABLE = "live_state_available"
    NO_ACTIVE_INTERVENTION = "no_active_intervention"
    REVISION_AVAILABLE = "revision_available"
    RESTORATION_SAFE = "restoration_safe"
    REPLICA_BOUNDS = "replica_bounds"
    REPLICA_QUOTA = "replica_quota"
    PATCH_SHAPE = "patch_shape"
    SERVER_DRY_RUN = "server_dry_run"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class InterventionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SAFE_HALTED = "safe_halted"


class WorkloadReference(KubeCouncilModel):
    namespace: str = Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    name: str = Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    kind: Literal["Deployment"] = "Deployment"


class ReplicaBounds(KubeCouncilModel):
    minimum: int = Field(ge=0)
    maximum: int = Field(ge=1)

    @model_validator(mode="after")
    def has_valid_range(self) -> "ReplicaBounds":
        if self.minimum > self.maximum:
            raise ValueError("maximum replicas must be greater than or equal to minimum replicas")
        return self


class ManagedWorkload(KubeCouncilModel):
    reference: WorkloadReference
    criticality: WorkloadCriticality
    replica_bounds: ReplicaBounds
    executable: bool
    protected_dependency: bool = False
    allowed_actions: tuple[
        Literal["rollback_deployment", "scale_deployment", "restart_deployment"], ...
    ] = ()
    dependencies: tuple[str, ...] = ()

    @model_validator(mode="after")
    def authority_is_explicit(self) -> "ManagedWorkload":
        if self.protected_dependency and self.executable:
            raise ValueError("protected dependencies cannot be executable")
        if not self.executable and not self.protected_dependency:
            raise ValueError("non-executable workloads must be protected dependencies")
        if not self.executable and self.allowed_actions:
            raise ValueError("non-executable workloads cannot allow remediation actions")
        if self.executable and not self.allowed_actions:
            raise ValueError("executable workloads must declare allowed remediation actions")
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("workload allowed actions must be unique")
        return self


class SyntheticProbe(KubeCouncilModel):
    name: str = Field(min_length=1)
    target: str = Field(min_length=1)
    repetitions: int = Field(ge=1, le=10)


class CriticalJourney(KubeCouncilModel):
    name: str = Field(min_length=1)
    success_rate_minimum: float = Field(ge=0, le=1)
    p95_latency_ms_maximum: int = Field(gt=0)
    minimum_request_count: int = Field(ge=100)
    synthetic_probe: SyntheticProbe | None = None


class EvidenceBudget(KubeCouncilModel):
    maximum_queries_per_specialist: int = Field(ge=0, le=2)
    maximum_log_lines: int = Field(gt=0, le=500)
    maximum_metric_series: int = Field(gt=0, le=100)
    maximum_window_minutes: int = Field(gt=0, le=120)


class EvidenceMapping(KubeCouncilModel):
    """A profile-owned identity for an allowlisted evidence source and query."""

    source: EvidenceSource
    kind: EvidenceQueryKind
    scope: WorkloadReference
    identifier: str = Field(min_length=1, max_length=500)
    query_template: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def has_a_bounded_metric_query(self) -> "EvidenceMapping":
        if self.kind == EvidenceQueryKind.METRICS and self.query_template is None:
            raise ValueError("metric evidence mappings must declare a query template")
        return self


class ObservabilityLink(KubeCouncilModel):
    """A safe deep link to a provider-owned observability view."""

    label: str = Field(min_length=1, max_length=100)
    source: EvidenceSource
    url: str = Field(min_length=1, max_length=2000, pattern=r"^https://")


class RecoveryCriteria(KubeCouncilModel):
    critical_journey_name: str = Field(min_length=1)
    required_stable_windows: int = Field(ge=2)
    stabilization_window_seconds: int = Field(ge=60)
    allow_synthetic_availability_fallback: bool = True
    latency_requires_application_traffic: Literal[True] = True


class ApplicationProfile(KubeCouncilModel):
    application_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    namespace: str = Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    namespaces: tuple[
        Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")], ...
    ] = Field(min_length=1)
    investigator_identity: str = Field(min_length=1)
    investigator_role: str = Field(min_length=1)
    executor_identity: str = Field(min_length=1)
    executor_role: str = Field(min_length=1)
    workloads: tuple[ManagedWorkload, ...] = Field(min_length=1)
    critical_journeys: tuple[CriticalJourney, ...] = Field(min_length=1)
    evidence_mappings: tuple[EvidenceMapping, ...] = Field(min_length=1)
    observability_links: tuple[ObservabilityLink, ...] = ()
    evidence_budget: EvidenceBudget
    recovery_criteria: RecoveryCriteria

    @model_validator(mode="after")
    def references_are_consistent(self) -> "ApplicationProfile":
        if self.investigator_identity == self.executor_identity:
            raise ValueError("Investigator and Executor identities must be distinct")
        workload_names = [workload.reference.name for workload in self.workloads]
        if len(workload_names) != len(set(workload_names)):
            raise ValueError("application profile workload names must be unique")
        if len(self.namespaces) != len(set(self.namespaces)):
            raise ValueError("application profile namespaces must be unique")
        if self.namespace not in self.namespaces:
            raise ValueError("application profile namespace must be in the namespace allowlist")
        if any(workload.reference.namespace not in self.namespaces for workload in self.workloads):
            raise ValueError("application profile workloads must use an enrolled namespace")
        workload_name_set = set(workload_names)
        for workload in self.workloads:
            if workload.reference.name in workload.dependencies:
                raise ValueError("application profile workloads cannot depend on themselves")
            if any(dependency not in workload_name_set for dependency in workload.dependencies):
                raise ValueError("workload dependencies must reference a declared workload")
        journeys = {journey.name: journey for journey in self.critical_journeys}
        if len(journeys) != len(self.critical_journeys):
            raise ValueError("application profile critical journey names must be unique")
        recovery_journey = journeys.get(self.recovery_criteria.critical_journey_name)
        if recovery_journey is None:
            raise ValueError("recovery criteria must reference a declared critical journey")
        if (
            self.recovery_criteria.allow_synthetic_availability_fallback
            and recovery_journey.synthetic_probe is None
        ):
            raise ValueError(
                "synthetic availability fallback requires a critical journey synthetic probe"
            )
        mapping_keys = {
            (mapping.source, mapping.kind, mapping.scope, mapping.identifier)
            for mapping in self.evidence_mappings
        }
        if len(mapping_keys) != len(self.evidence_mappings):
            raise ValueError("application profile evidence mappings must be unique")
        if len({mapping.identifier for mapping in self.evidence_mappings}) != len(
            self.evidence_mappings
        ):
            raise ValueError("application profile evidence mapping identifiers must be unique")
        if any(
            mapping.scope not in {workload.reference for workload in self.workloads}
            for mapping in self.evidence_mappings
        ):
            raise ValueError("evidence mappings must target a declared workload")
        link_keys = {(link.source, link.url) for link in self.observability_links}
        if len(link_keys) != len(self.observability_links):
            raise ValueError("application profile observability links must be unique")
        return self


class ProfileValidationIssue(KubeCouncilModel):
    location: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ApplicationProfileLoadResult(KubeCouncilModel):
    """A profile-load outcome safe to expose at startup or reload time."""

    source: str = Field(min_length=1)
    application_id: str | None = None
    profile: ApplicationProfile | None = None
    errors: tuple[ProfileValidationIssue, ...] = ()

    @model_validator(mode="after")
    def is_complete(self) -> "ApplicationProfileLoadResult":
        if self.profile is None and not self.errors:
            raise ValueError(
                "invalid application profile loads must include exact validation errors"
            )
        if self.profile is not None and self.errors:
            raise ValueError("valid application profile loads cannot include validation errors")
        if self.profile is not None and self.application_id != self.profile.application_id:
            raise ValueError("profile load application identity must match the loaded profile")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def valid(self) -> bool:
        return self.profile is not None


class EnrollmentCheck(KubeCouncilModel):
    code: EnrollmentCheckCode
    passed: bool
    message: str = Field(min_length=1)
    workload: WorkloadReference | None = None


class EnrollmentReadiness(KubeCouncilModel):
    application_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    ready: bool
    checks: tuple[EnrollmentCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def is_truthful(self) -> "EnrollmentReadiness":
        if self.ready != all(check.passed for check in self.checks):
            raise ValueError("enrollment readiness must equal the result of all checks")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_checks(self) -> tuple[EnrollmentCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)


class NamespaceEnrollmentState(KubeCouncilModel):
    namespace: str = Field(min_length=1)
    exists: bool
    labels: dict[str, str] = Field(default_factory=dict)


class WorkloadEnrollmentState(KubeCouncilModel):
    reference: WorkloadReference
    exists: bool
    labels: dict[str, str] = Field(default_factory=dict)


class RoleBindingEnrollmentState(KubeCouncilModel):
    namespace: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    role: str = Field(min_length=1)
    exists: bool


class EnrollmentSnapshot(KubeCouncilModel):
    """Typed, read-only prerequisite facts supplied by a Kubernetes adapter."""

    namespaces: tuple[NamespaceEnrollmentState, ...] = ()
    workloads: tuple[WorkloadEnrollmentState, ...] = ()
    role_bindings: tuple[RoleBindingEnrollmentState, ...] = ()
    admission_policy_binding: bool = False

    @model_validator(mode="after")
    def contains_unique_resources(self) -> "EnrollmentSnapshot":
        namespace_names = [state.namespace for state in self.namespaces]
        if len(namespace_names) != len(set(namespace_names)):
            raise ValueError("enrollment snapshot namespaces must be unique")
        references = [state.reference for state in self.workloads]
        if len(references) != len(set(references)):
            raise ValueError("enrollment snapshot workloads must be unique")
        role_bindings = [
            (state.namespace, state.subject, state.role) for state in self.role_bindings
        ]
        if len(role_bindings) != len(set(role_bindings)):
            raise ValueError("enrollment snapshot role bindings must be unique")
        return self


class ApplicationHealth(KubeCouncilModel):
    status: Literal["unknown"] = "unknown"
    message: str = "Health evidence has not been connected yet."


class ManagedApplication(KubeCouncilModel):
    application_profile: ApplicationProfile | None = None
    profile_load: ApplicationProfileLoadResult
    enrollment: EnrollmentReadiness
    health: ApplicationHealth = Field(default_factory=ApplicationHealth)
    incident_count: int = Field(ge=0)


class AlertSignal(KubeCouncilModel):
    signal_id: str = Field(min_length=1)
    application_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    workload_name: str = Field(min_length=1)
    workload_namespace: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    provider_incident_id: str | None = None

    @model_validator(mode="after")
    def is_scoped_and_time_bounded(self) -> "AlertSignal":
        if self.workload_namespace != self.namespace:
            raise ValueError("alert workload must use the same namespace as its application")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("alert windows require both start and end timestamps")
        if self.window_start is not None and self.window_end is not None:
            if self.window_end <= self.window_start:
                raise ValueError("alert window end must be after start")
            if self.window_end - self.window_start > timedelta(hours=2):
                raise ValueError("alert window cannot exceed the evidence budget maximum")
        return self


class AlertSignalEvidence(KubeCouncilModel):
    """Append-only provider state retained as evidence, never as lifecycle authority."""

    notification_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    signal: AlertSignal
    provider_state: Literal["open", "closed"]
    received_at: datetime


class Incident(KubeCouncilModel):
    incident_id: str = Field(min_length=1)
    application_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    opened_at: datetime
    lifecycle: IncidentLifecycle = IncidentLifecycle.OPEN
    investigation_outcome: InvestigationOutcome = InvestigationOutcome.NOT_STARTED
    intervention_outcome: InterventionOutcome = InterventionOutcome.NOT_STARTED
    version: int = Field(ge=0, default=0)
    summary: str = Field(min_length=1, max_length=1000)


class EvidenceWindow(KubeCouncilModel):
    window_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    captured_at: datetime

    @model_validator(mode="after")
    def is_time_ordered(self) -> "EvidenceWindow":
        if self.ended_at <= self.started_at:
            raise ValueError("evidence window end must be after start")
        if self.captured_at < self.ended_at:
            raise ValueError("evidence window must be captured after its end")
        return self


_LIFECYCLE_TRANSITIONS: dict[IncidentLifecycle, frozenset[IncidentLifecycle]] = {
    IncidentLifecycle.OPEN: frozenset({IncidentLifecycle.INVESTIGATING, IncidentLifecycle.CLOSED}),
    IncidentLifecycle.INVESTIGATING: frozenset(
        {
            IncidentLifecycle.AWAITING_APPROVAL,
            IncidentLifecycle.MONITORING,
            IncidentLifecycle.CLOSED,
        }
    ),
    IncidentLifecycle.AWAITING_APPROVAL: frozenset(
        {IncidentLifecycle.INVESTIGATING, IncidentLifecycle.MITIGATING, IncidentLifecycle.CLOSED}
    ),
    IncidentLifecycle.MITIGATING: frozenset(
        {IncidentLifecycle.MONITORING, IncidentLifecycle.CLOSED}
    ),
    IncidentLifecycle.MONITORING: frozenset(
        {IncidentLifecycle.MITIGATING, IncidentLifecycle.RESOLVED, IncidentLifecycle.CLOSED}
    ),
    IncidentLifecycle.RESOLVED: frozenset({IncidentLifecycle.CLOSED}),
    IncidentLifecycle.CLOSED: frozenset(),
}

_INVESTIGATION_OUTCOME_TRANSITIONS: dict[InvestigationOutcome, frozenset[InvestigationOutcome]] = {
    InvestigationOutcome.NOT_STARTED: frozenset(
        {
            InvestigationOutcome.PROPOSAL_READY,
            InvestigationOutcome.NEEDS_MORE_EVIDENCE,
            InvestigationOutcome.NO_SAFE_ACTION,
            InvestigationOutcome.INCONCLUSIVE,
        }
    ),
    InvestigationOutcome.NEEDS_MORE_EVIDENCE: frozenset(
        {
            InvestigationOutcome.PROPOSAL_READY,
            InvestigationOutcome.NO_SAFE_ACTION,
            InvestigationOutcome.INCONCLUSIVE,
        }
    ),
    InvestigationOutcome.PROPOSAL_READY: frozenset(),
    InvestigationOutcome.NO_SAFE_ACTION: frozenset(),
    InvestigationOutcome.INCONCLUSIVE: frozenset(),
}

_INTERVENTION_OUTCOME_TRANSITIONS: dict[InterventionOutcome, frozenset[InterventionOutcome]] = {
    InterventionOutcome.NOT_STARTED: frozenset(
        {
            InterventionOutcome.MONITORING,
            InterventionOutcome.SUCCEEDED,
            InterventionOutcome.ROLLED_BACK,
            InterventionOutcome.FAILED,
            InterventionOutcome.SAFE_HALTED,
        }
    ),
    InterventionOutcome.MONITORING: frozenset(
        {
            InterventionOutcome.SUCCEEDED,
            InterventionOutcome.ROLLED_BACK,
            InterventionOutcome.FAILED,
            InterventionOutcome.SAFE_HALTED,
        }
    ),
    InterventionOutcome.SUCCEEDED: frozenset(),
    InterventionOutcome.ROLLED_BACK: frozenset(),
    InterventionOutcome.FAILED: frozenset(),
    InterventionOutcome.SAFE_HALTED: frozenset(),
}


def transition_incident(
    incident: Incident,
    *,
    lifecycle: IncidentLifecycle | None = None,
    investigation_outcome: InvestigationOutcome | None = None,
    intervention_outcome: InterventionOutcome | None = None,
) -> Incident:
    """Build a valid state transition without conflating lifecycle and outcomes."""

    next_lifecycle = lifecycle or incident.lifecycle
    allowed_next_lifecycles = _LIFECYCLE_TRANSITIONS[incident.lifecycle]
    if next_lifecycle != incident.lifecycle and next_lifecycle not in allowed_next_lifecycles:
        raise ValueError(
            f"invalid incident lifecycle transition: {incident.lifecycle} -> {next_lifecycle}"
        )
    next_investigation_outcome = investigation_outcome or incident.investigation_outcome
    if (
        next_investigation_outcome != incident.investigation_outcome
        and next_investigation_outcome
        not in _INVESTIGATION_OUTCOME_TRANSITIONS[incident.investigation_outcome]
    ):
        raise ValueError(
            "invalid investigation outcome transition: "
            f"{incident.investigation_outcome} -> {next_investigation_outcome}"
        )
    next_intervention_outcome = intervention_outcome or incident.intervention_outcome
    if (
        next_intervention_outcome != incident.intervention_outcome
        and next_intervention_outcome
        not in _INTERVENTION_OUTCOME_TRANSITIONS[incident.intervention_outcome]
    ):
        raise ValueError(
            "invalid intervention outcome transition: "
            f"{incident.intervention_outcome} -> {next_intervention_outcome}"
        )
    return incident.model_copy(
        update={
            "lifecycle": next_lifecycle,
            "investigation_outcome": next_investigation_outcome,
            "intervention_outcome": next_intervention_outcome,
        }
    )


class EvidenceObservation(KubeCouncilModel):
    evidence_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    source: EvidenceSource
    query: EvidenceQueryKind
    query_reference: str = Field(min_length=1)
    evidence_query_id: str | None = Field(default=None, min_length=1)
    evidence_window_id: str = Field(min_length=1)
    observed_at: datetime
    scope: WorkloadReference
    redacted_excerpt: str = Field(min_length=1, max_length=10000)
    content_hash: str = Field(min_length=8)
    truncated: bool = False
    provider_reference: str = Field(min_length=1)


class RawEvidenceObservation(KubeCouncilModel):
    """A bounded provider response that exists only before deterministic redaction."""

    source: EvidenceSource
    kind: EvidenceQueryKind
    scope: WorkloadReference
    content: str = Field(default="", max_length=100_000)
    provider_reference: str = Field(min_length=1)
    observed_at: datetime | None = None
    item_count: int = Field(default=1, ge=1)
    metric_series: tuple[str, ...] = ()

    @model_validator(mode="after")
    def uses_bounded_metric_series(self) -> "RawEvidenceObservation":
        if self.kind == EvidenceQueryKind.METRICS:
            if not self.metric_series:
                raise ValueError("metric evidence must provide structured metric series")
            if self.content:
                raise ValueError("metric evidence cannot include an unstructured content payload")
            if self.item_count != len(self.metric_series):
                raise ValueError(
                    "metric evidence item count must match its structured metric series"
                )
        elif not self.content:
            raise ValueError("non-metric evidence requires content")
        elif self.metric_series:
            raise ValueError("only metric evidence can include metric series")
        return self


class EvidenceRetrievalFailure(KubeCouncilModel):
    """Safe, user-visible metadata for evidence that was deliberately not retained."""

    failure_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    source: EvidenceSource | None = None
    query: EvidenceQueryKind | None = None
    scope: WorkloadReference | None = None
    occurred_at: datetime
    message: str = Field(min_length=1, max_length=1000)


class EvidenceQuery(KubeCouncilModel):
    query_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    specialist: SpecialistRole
    kind: EvidenceQueryKind
    target: WorkloadReference
    requested_at: datetime
    query_round: int = Field(ge=1, le=2)


class EvidenceProviderRequest(KubeCouncilModel):
    """Fully resolved server-side request passed to one read-only provider adapter."""

    query_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    source: EvidenceSource
    kind: EvidenceQueryKind
    scope: WorkloadReference
    mapping_identifier: str = Field(min_length=1, max_length=500)
    query_template: str | None = Field(default=None, min_length=1, max_length=2000)
    started_at: datetime
    ended_at: datetime
    maximum_items: int = Field(ge=1, le=500)
    deadline_seconds: float = Field(gt=0, le=30)

    @model_validator(mode="after")
    def is_bounded_and_provider_owned(self) -> "EvidenceProviderRequest":
        if self.ended_at <= self.started_at:
            raise ValueError("provider evidence request end must be after start")
        if self.ended_at - self.started_at > timedelta(hours=2):
            raise ValueError("provider evidence request cannot exceed two hours")
        if self.kind == EvidenceQueryKind.METRICS and self.query_template is None:
            raise ValueError("metric provider requests require a profile-owned query template")
        return self


class EvidenceCitation(KubeCouncilModel):
    evidence_id: str = Field(min_length=1)
    observation: str = Field(min_length=1, max_length=1000)


class SpecialistFinding(KubeCouncilModel):
    finding_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    specialist: SpecialistRole
    citations: tuple[EvidenceCitation, ...] = Field(min_length=1)
    candidate_explanations: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    contradictions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()


class SpecialistEvidenceQueryRequest(KubeCouncilModel):
    """Model-selected profile mapping; provider scope is always resolved server-side."""

    mapping_identifier: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)


class SpecialistModelOutput(KubeCouncilModel):
    finding: SpecialistFinding | None = None
    evidence_query: SpecialistEvidenceQueryRequest | None = None

    @model_validator(mode="after")
    def contains_exactly_one_next_step(self) -> "SpecialistModelOutput":
        if (self.finding is None) == (self.evidence_query is None):
            raise ValueError("specialist output must contain exactly one finding or evidence query")
        return self


class ModelResponse(KubeCouncilModel):
    """Provider-independent model response with auditable usage metadata."""

    output: dict[str, object]
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    thinking_level: Literal["minimal", "low", "medium", "high"]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class SpecialistRequest(KubeCouncilModel):
    """The complete and deliberately narrow model context for one Specialist turn."""

    incident_id: str = Field(min_length=1)
    role: SpecialistRole
    evidence: tuple[EvidenceObservation, ...] = Field(min_length=1)
    allowed_mapping_identifiers: tuple[str, ...] = ()
    completed_query_rounds: int = Field(ge=0, le=2)
    evidence_is_untrusted: Literal[True] = True


class SpecialistResult(KubeCouncilModel):
    role: SpecialistRole
    status: SpecialistRunStatus
    finding: SpecialistFinding | None = None
    failure_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def matches_status(self) -> "SpecialistResult":
        if self.status is SpecialistRunStatus.SUCCEEDED:
            if self.finding is None or self.failure_reason is not None:
                raise ValueError("successful Specialist result requires only a finding")
        elif self.finding is not None or self.failure_reason is None:
            raise ValueError("failed or timed-out Specialist result requires only a reason")
        return self


class ModelInvocation(KubeCouncilModel):
    invocation_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    role: SpecialistRole | Literal["coordinator"]
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    thinking_level: Literal["minimal", "low", "medium", "high"]
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    tool_count: int = Field(ge=0)
    output_valid: bool
    failure_reason: str | None = None

    @model_validator(mode="after")
    def records_validation_failure(self) -> "ModelInvocation":
        if self.output_valid and self.failure_reason is not None:
            raise ValueError("valid model output cannot include a failure reason")
        if not self.output_valid and self.failure_reason is None:
            raise ValueError("invalid model output requires a failure reason")
        return self


class RootCauseHypothesis(KubeCouncilModel):
    hypothesis_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    statement: str = Field(min_length=1)
    falsification_test: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    citations: tuple[EvidenceCitation, ...] = Field(min_length=1)


class RollbackDeploymentAction(KubeCouncilModel):
    action_type: Literal["rollback_deployment"] = "rollback_deployment"
    target: WorkloadReference
    revision: int = Field(ge=1)


class ScaleDeploymentAction(KubeCouncilModel):
    action_type: Literal["scale_deployment"] = "scale_deployment"
    target: WorkloadReference
    replicas: int = Field(ge=0)


class RestartDeploymentAction(KubeCouncilModel):
    action_type: Literal["restart_deployment"] = "restart_deployment"
    target: WorkloadReference
    restart_token: str = Field(min_length=1)


RemediationAction = Annotated[
    RollbackDeploymentAction | ScaleDeploymentAction | RestartDeploymentAction,
    Field(discriminator="action_type"),
]


class RemediationProposal(KubeCouncilModel):
    proposal_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    action: RemediationAction
    expected_impact: str = Field(min_length=1)
    recovery_criteria: RecoveryCriteria
    rollback_strategy: str = Field(min_length=1)
    evidence_hash: str = Field(min_length=8)
    known_risks: tuple[str, ...] = ()


class ManualGuidance(KubeCouncilModel):
    incident_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    guidance: str = Field(min_length=1)
    outcome: Literal[InvestigationOutcome.NO_SAFE_ACTION, InvestigationOutcome.NEEDS_MORE_EVIDENCE]


class CoordinatorRequest(KubeCouncilModel):
    """Structured Specialist results and declared policy facts available to the Coordinator."""

    incident_id: str = Field(min_length=1)
    incident_summary: str = Field(min_length=1)
    application_profile: ApplicationProfile
    evidence_hash: str = Field(min_length=8)
    specialists: tuple[SpecialistResult, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def includes_each_specialist_once(self) -> "CoordinatorRequest":
        roles = {result.role for result in self.specialists}
        if roles != set(SpecialistRole):
            raise ValueError("Coordinator input must include every Specialist exactly once")
        return self


class CoordinatorOutput(KubeCouncilModel):
    """One consolidated outcome; policy evaluation remains a later deterministic step."""

    outcome: Literal[
        InvestigationOutcome.PROPOSAL_READY,
        InvestigationOutcome.NEEDS_MORE_EVIDENCE,
        InvestigationOutcome.NO_SAFE_ACTION,
        InvestigationOutcome.INCONCLUSIVE,
    ]
    hypotheses: tuple[RootCauseHypothesis, ...] = ()
    proposal: RemediationProposal | None = None
    manual_guidance: ManualGuidance | None = None

    @model_validator(mode="after")
    def contains_exactly_one_supported_outcome(self) -> "CoordinatorOutput":
        ranks = [hypothesis.rank for hypothesis in self.hypotheses]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("Root Cause Hypotheses must have consecutive ranks starting at one")
        if self.outcome is InvestigationOutcome.PROPOSAL_READY:
            if self.proposal is None or self.manual_guidance is not None:
                raise ValueError("proposal_ready requires exactly one Remediation Proposal")
        elif self.outcome in {
            InvestigationOutcome.NEEDS_MORE_EVIDENCE,
            InvestigationOutcome.NO_SAFE_ACTION,
        }:
            if self.manual_guidance is None or self.proposal is not None:
                raise ValueError("Safe Refusal requires exactly one Manual Guidance outcome")
            if self.manual_guidance.outcome is not self.outcome:
                raise ValueError("Manual Guidance outcome must match the Coordinator outcome")
        elif self.proposal is not None or self.manual_guidance is not None:
            raise ValueError("inconclusive output cannot contain a proposal or Manual Guidance")
        if self.outcome is not InvestigationOutcome.INCONCLUSIVE and not self.hypotheses:
            raise ValueError("a conclusive Coordinator output requires ranked hypotheses")
        return self


class DeploymentRevision(KubeCouncilModel):
    revision: int = Field(ge=1)
    pod_template: dict[str, object]
    restorable: bool = True
    implicated: bool = False


class DeploymentPolicyState(KubeCouncilModel):
    target: WorkloadReference
    resource_version: str = Field(min_length=1)
    generation: int = Field(ge=1)
    replicas: int = Field(ge=0)
    current_revision: int = Field(ge=1)
    available_revisions: tuple[DeploymentRevision, ...] = Field(min_length=1)
    active_intervention: bool = False
    replica_quota_headroom: int = Field(ge=0)


class DeploymentPatch(KubeCouncilModel):
    action_type: Literal["rollback_deployment", "scale_deployment", "restart_deployment"]
    target: WorkloadReference
    resource_version: str = Field(min_length=1)
    body: dict[str, object]


class DryRunResult(KubeCouncilModel):
    accepted: bool
    diff: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def has_one_outcome(self) -> "DryRunResult":
        if self.accepted and (self.diff is None or self.error is not None):
            raise ValueError("accepted dry-run results require only a diff")
        if not self.accepted and (self.error is None or self.diff is not None):
            raise ValueError("rejected dry-run results require only an error")
        return self


class PolicyCheck(KubeCouncilModel):
    code: PolicyCheckCode
    passed: bool
    message: str = Field(min_length=1)


class PolicyDecision(KubeCouncilModel):
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    status: PolicyStatus
    checks: tuple[PolicyCheck, ...] = Field(min_length=1)
    evaluated_at: datetime
    workload_resource_version: str | None = None
    patch: DeploymentPatch | None = None
    dry_run_diff: str | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def matches_status(self) -> "PolicyDecision":
        failed = tuple(check for check in self.checks if not check.passed)
        if self.status is PolicyStatus.PASSED:
            if failed or self.patch is None or self.dry_run_diff is None:
                raise ValueError("passed policy requires all checks, a patch, and a dry-run diff")
            if self.rejection_reason is not None:
                raise ValueError("passed policy cannot include a rejection reason")
        else:
            if not failed or self.rejection_reason is None or self.dry_run_diff is not None:
                raise ValueError("rejected policy requires failed checks and a rejection reason")
        return self


class Approval(KubeCouncilModel):
    approval_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    responder_principal: str = Field(min_length=1)
    decision: ApprovalDecision
    decided_at: datetime
    expires_at: datetime
    proposal_hash: str = Field(min_length=8)
    evidence_hash: str = Field(min_length=8)
    workload_version: str = Field(min_length=1)
    policy_hash: str = Field(min_length=8)
    dry_run_hash: str = Field(min_length=8)
    recovery_criteria_hash: str = Field(min_length=8)
    failure_strategy_hash: str = Field(min_length=8)

    @model_validator(mode="after")
    def has_a_future_expiry(self) -> "Approval":
        if self.expires_at <= self.decided_at:
            raise ValueError("approval expiry must be after its decision time")
        if self.expires_at <= datetime.now(self.expires_at.tzinfo):
            raise ValueError("approval has expired and is stale")
        return self


class Intervention(KubeCouncilModel):
    intervention_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    approval_id: str = Field(min_length=1)
    target: WorkloadReference
    state: InterventionState
    requested_at: datetime
    idempotency_key: str = Field(min_length=8)


class RecoveryAssessment(KubeCouncilModel):
    incident_id: str = Field(min_length=1)
    intervention_id: str = Field(min_length=1)
    observed_at: datetime
    criteria_satisfied: bool
    request_count: int = Field(ge=0)
    sufficient_evidence: bool
    explanation: str = Field(min_length=1)


class AuditEvent(KubeCouncilModel):
    event_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: datetime
    actor: str = Field(min_length=1)
    details: dict[str, str] = Field(default_factory=dict)
    cursor: int = Field(default=0, ge=0)


class InvestigationRecord(KubeCouncilModel):
    incident: Incident
    application_profile: ApplicationProfile
    evidence_window: EvidenceWindow
    alert_signals: tuple[AlertSignalEvidence, ...] = ()
    evidence: tuple[EvidenceObservation, ...] = ()
    evidence_retrieval_failures: tuple[EvidenceRetrievalFailure, ...] = ()
    evidence_queries: tuple[EvidenceQuery, ...] = ()
    findings: tuple[SpecialistFinding, ...] = ()
    model_invocations: tuple[ModelInvocation, ...] = ()
    hypotheses: tuple[RootCauseHypothesis, ...] = ()
    proposal: RemediationProposal | None = None
    manual_guidance: ManualGuidance | None = None
    policy_decision: PolicyDecision | None = None
    approvals: tuple[Approval, ...] = ()
    interventions: tuple[Intervention, ...] = ()
    recovery_assessments: tuple[RecoveryAssessment, ...] = ()
    audit_events: tuple[AuditEvent, ...] = ()

    @model_validator(mode="after")
    def all_entries_belong_to_the_incident(self) -> "InvestigationRecord":
        incident_id = self.incident.incident_id
        if self.incident.application_id != self.application_profile.application_id:
            raise ValueError("incident must reference the record application profile")
        if self.evidence_window.incident_id != incident_id:
            raise ValueError("evidence window must belong to the same incident")
        enrolled_workloads = {workload.reference for workload in self.application_profile.workloads}
        scoped_entries = [item.scope for item in self.evidence] + [
            item.target for item in self.evidence_queries
        ]
        if self.proposal is not None:
            scoped_entries.append(self.proposal.action.target)
        scoped_entries.extend(item.target for item in self.interventions)
        if any(scope not in enrolled_workloads for scope in scoped_entries):
            raise ValueError(
                "investigation record scope is outside the enrolled application profile"
            )
        entry_incident_ids = (
            [item.incident_id for item in self.alert_signals]
            + [item.incident_id for item in self.evidence]
            + [item.incident_id for item in self.evidence_retrieval_failures]
            + [item.incident_id for item in self.evidence_queries]
            + [item.incident_id for item in self.findings]
            + [item.incident_id for item in self.model_invocations]
            + [item.incident_id for item in self.hypotheses]
            + [item.incident_id for item in self.approvals]
            + [item.incident_id for item in self.interventions]
            + [item.incident_id for item in self.recovery_assessments]
            + [item.incident_id for item in self.audit_events]
        )
        if self.proposal is not None:
            entry_incident_ids.append(self.proposal.incident_id)
        if self.manual_guidance is not None:
            entry_incident_ids.append(self.manual_guidance.incident_id)
        if any(entry_id != incident_id for entry_id in entry_incident_ids):
            raise ValueError("investigation record entries must belong to the same incident")
        evidence_ids = {item.evidence_id for item in self.evidence}
        evidence_query_ids = {item.query_id for item in self.evidence_queries}
        if any(
            item.evidence_query_id is not None and item.evidence_query_id not in evidence_query_ids
            for item in self.evidence
        ):
            raise ValueError(
                "follow-up evidence must reference a query in the investigation record"
            )
        citations = [citation.evidence_id for item in self.findings for citation in item.citations]
        citations.extend(
            citation.evidence_id for item in self.hypotheses for citation in item.citations
        )
        if any(citation not in evidence_ids for citation in citations):
            raise ValueError(
                "findings and hypotheses must cite evidence in the investigation record"
            )
        if self.proposal is not None and self.manual_guidance is not None:
            raise ValueError(
                "an investigation record cannot contain proposal and manual guidance together"
            )
        if self.policy_decision is not None:
            if self.proposal is None:
                raise ValueError("policy decisions require a remediation proposal")
            if self.policy_decision.incident_id != incident_id:
                raise ValueError("policy decision must belong to the same incident")
            if self.policy_decision.proposal_id != self.proposal.proposal_id:
                raise ValueError("policy decision must reference the record proposal")
        if self.proposal is not None and any(
            approval.proposal_id != self.proposal.proposal_id for approval in self.approvals
        ):
            raise ValueError("approvals must reference the record proposal")
        if self.approvals and (
            self.policy_decision is None
            or self.policy_decision.status is not PolicyStatus.PASSED
        ):
            raise ValueError("Approval cannot override a missing or rejected policy decision")
        approval_ids = {approval.approval_id for approval in self.approvals}
        intervention_ids = {intervention.intervention_id for intervention in self.interventions}
        if self.proposal is None and self.interventions:
            raise ValueError("interventions require a remediation proposal")
        if any(intervention.approval_id not in approval_ids for intervention in self.interventions):
            raise ValueError("interventions must reference an approval in the investigation record")
        if self.proposal is not None and any(
            intervention.proposal_id != self.proposal.proposal_id
            for intervention in self.interventions
        ):
            raise ValueError("interventions must reference the record proposal")
        if any(
            assessment.intervention_id not in intervention_ids
            for assessment in self.recovery_assessments
        ):
            raise ValueError("recovery assessments must reference a record intervention")
        return self


class IncidentStore(Protocol):
    """Durable incident boundary with append-only evidence and audit APIs."""

    def create(self, profile: ApplicationProfile, signal: AlertSignal) -> InvestigationRecord: ...

    def get(self, incident_id: str) -> InvestigationRecord | None: ...

    def list(self) -> tuple[InvestigationRecord, ...]: ...

    def append_evidence(
        self, incident_id: str, evidence: EvidenceObservation
    ) -> InvestigationRecord: ...

    def append_alert_signal(
        self, incident_id: str, signal: AlertSignalEvidence
    ) -> InvestigationRecord: ...

    def append_evidence_retrieval_failure(
        self, incident_id: str, failure: EvidenceRetrievalFailure
    ) -> InvestigationRecord: ...

    def append_evidence_query(
        self, incident_id: str, query: EvidenceQuery
    ) -> InvestigationRecord: ...

    def append_finding(
        self, incident_id: str, finding: SpecialistFinding
    ) -> InvestigationRecord: ...

    def append_model_invocation(
        self, incident_id: str, invocation: ModelInvocation
    ) -> InvestigationRecord: ...

    def complete_investigation(
        self, incident_id: str, output: CoordinatorOutput
    ) -> InvestigationRecord: ...

    def record_policy_decision(
        self, incident_id: str, decision: PolicyDecision
    ) -> InvestigationRecord: ...

    def append_audit_event(self, incident_id: str, event: AuditEvent) -> InvestigationRecord: ...

    def timeline(self, incident_id: str, *, after: int = 0) -> tuple[AuditEvent, ...]: ...

    def compare_and_set(
        self,
        incident_id: str,
        expected_version: int,
        replacement: Incident,
    ) -> InvestigationRecord: ...


class ApplicationProfileProvider(Protocol):
    """Loads profile documents without leaking ConfigMap SDK values into the domain."""

    def list_profiles(self) -> tuple[ApplicationProfileLoadResult, ...]: ...


class EnrollmentProvider(Protocol):
    """Reads only the Kubernetes prerequisites required to assess Enrollment."""

    def inspect(self, profile: ApplicationProfile) -> EnrollmentSnapshot: ...


class EvidenceProvider(Protocol):
    """Returns only bounded, allowlisted initial evidence for an enrolled application."""

    def collect_initial(
        self,
        profile: ApplicationProfile,
        signal: AlertSignal,
        window: EvidenceWindow,
    ) -> tuple[RawEvidenceObservation, ...]: ...


class EvidenceQueryAdapter(Protocol):
    """Executes one server-resolved, bounded, read-only provider request."""

    def query(self, request: EvidenceProviderRequest) -> RawEvidenceObservation: ...


class IncidentCouncilModel(Protocol):
    """Structured model boundary; implementations receive no mutation or provider tools."""

    async def analyze_specialist(self, request: SpecialistRequest) -> ModelResponse: ...

    async def coordinate(self, request: CoordinatorRequest) -> ModelResponse: ...


class PolicyKubernetesProvider(Protocol):
    """Narrow live-state and server-side dry-run boundary used by deterministic policy."""

    def inspect_deployment(self, target: WorkloadReference) -> DeploymentPolicyState | None: ...

    def dry_run_deployment_patch(self, patch: DeploymentPatch) -> DryRunResult: ...


class EvidenceRedactor(Protocol):
    """Removes sensitive values before evidence crosses any persistence or UI boundary."""

    def redact(self, content: str) -> str: ...
