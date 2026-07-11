"""Strict, provider-independent contracts for the incident-response workflow."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

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


class EvidenceQueryKind(StrEnum):
    WORKLOAD_STATE = "workload_state"
    POD_EVENTS = "pod_events"
    POD_LOGS = "pod_logs"
    METRICS = "metrics"
    CHANGE_HISTORY = "change_history"
    ALERT_POLICY = "alert_policy"


class PolicyStatus(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    DRY_RUN_FAILED = "dry_run_failed"


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
        if not self.executable and self.allowed_actions:
            raise ValueError("non-executable workloads cannot allow remediation actions")
        if self.executable and not self.allowed_actions:
            raise ValueError("executable workloads must declare allowed remediation actions")
        return self


class CriticalJourney(KubeCouncilModel):
    name: str = Field(min_length=1)
    success_rate_minimum: float = Field(ge=0, le=1)
    p95_latency_ms_maximum: int = Field(gt=0)
    minimum_request_count: int = Field(ge=100)


class EvidenceBudget(KubeCouncilModel):
    maximum_queries_per_specialist: int = Field(ge=0, le=2)
    maximum_log_lines: int = Field(gt=0, le=500)
    maximum_metric_series: int = Field(gt=0, le=100)
    maximum_window_minutes: int = Field(gt=0, le=120)


class RecoveryCriteria(KubeCouncilModel):
    critical_journey_name: str = Field(min_length=1)
    required_stable_windows: int = Field(ge=2)
    stabilization_window_seconds: int = Field(ge=60)


class ApplicationProfile(KubeCouncilModel):
    application_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    namespace: str = Field(min_length=1, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    workloads: tuple[ManagedWorkload, ...] = Field(min_length=1)
    critical_journeys: tuple[CriticalJourney, ...] = Field(min_length=1)
    evidence_budget: EvidenceBudget
    recovery_criteria: RecoveryCriteria

    @model_validator(mode="after")
    def references_are_consistent(self) -> "ApplicationProfile":
        workload_names = [workload.reference.name for workload in self.workloads]
        if len(workload_names) != len(set(workload_names)):
            raise ValueError("application profile workload names must be unique")
        if any(workload.reference.namespace != self.namespace for workload in self.workloads):
            raise ValueError("application profile workloads must use the enrolled namespace")
        journeys = {journey.name for journey in self.critical_journeys}
        if self.recovery_criteria.critical_journey_name not in journeys:
            raise ValueError("recovery criteria must reference a declared critical journey")
        return self


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
    observed_at: datetime
    scope: WorkloadReference
    redacted_excerpt: str = Field(min_length=1, max_length=10000)
    content_hash: str = Field(min_length=8)
    truncated: bool = False
    provider_reference: str = Field(min_length=1)


class EvidenceQuery(KubeCouncilModel):
    query_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    specialist: SpecialistRole
    kind: EvidenceQueryKind
    target: WorkloadReference
    requested_at: datetime
    query_round: int = Field(ge=1, le=2)


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


class ModelInvocation(KubeCouncilModel):
    invocation_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    role: SpecialistRole | Literal["coordinator"]
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    tool_count: int = Field(ge=0)
    output_valid: bool
    failure_reason: str | None = None


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


class ManualGuidance(KubeCouncilModel):
    incident_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    guidance: str = Field(min_length=1)
    outcome: Literal[InvestigationOutcome.NO_SAFE_ACTION, InvestigationOutcome.NEEDS_MORE_EVIDENCE]


class PolicyDecision(KubeCouncilModel):
    incident_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    status: PolicyStatus
    checks: tuple[str, ...] = Field(min_length=1)
    dry_run_diff: str | None = None


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


class InvestigationRecord(KubeCouncilModel):
    incident: Incident
    application_profile: ApplicationProfile
    evidence_window: EvidenceWindow
    evidence: tuple[EvidenceObservation, ...] = ()
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
            [item.incident_id for item in self.evidence]
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

    def append_audit_event(self, incident_id: str, event: AuditEvent) -> InvestigationRecord: ...

    def compare_and_set(
        self,
        incident_id: str,
        expected_version: int,
        replacement: Incident,
    ) -> InvestigationRecord: ...
