from collections.abc import Sequence
from typing import Any

from app.agents.council import CouncilPlanValidator
from app.domain.interfaces import ExperimentAuditor, KubernetesClient, LoadTestRunner
from app.domain.models import (
    CouncilAction,
    CouncilPlan,
    CouncilPlanStatus,
    CouncilWorkloadSnapshot,
    ExperimentAudit,
    ExperimentReport,
    ExperimentStatus,
    LoadTestResult,
    LoadTestStatus,
    RehearsalState,
    ResourceRequests,
    ScenarioSpec,
    ValidationResult,
    ValidationStatus,
)


class CouncilPlanExecutionError(RuntimeError):
    """Raised when a validated council plan cannot be executed or verified."""


class CouncilPlanExecutor:
    """Applies validated council plans to a rehearsal namespace and audits the result."""

    def __init__(
        self,
        kubernetes: KubernetesClient,
        load_tests: LoadTestRunner,
        auditor: ExperimentAuditor,
        validator: CouncilPlanValidator | None = None,
    ) -> None:
        self._kubernetes = kubernetes
        self._load_tests = load_tests
        self._auditor = auditor
        self._validator = validator or CouncilPlanValidator()

    def apply_and_verify(
        self,
        rehearsal: RehearsalState,
        plan: CouncilPlan,
        scenario: ScenarioSpec,
        baseline: LoadTestResult,
        pressure_before: LoadTestResult,
    ) -> tuple[ExperimentReport, CouncilWorkloadSnapshot]:
        self._ensure_plan_is_executable(rehearsal, plan)
        validated = self._validator.validate(
            plan,
            rehearsal.namespace,
            rehearsal.plan.services,
            ResourceRequests(
                cpu_millis=rehearsal.plan.resource_quota_cpu_millis,
                memory_mib=rehearsal.plan.resource_quota_memory_mib,
            ),
        )
        if validated.status != CouncilPlanStatus.VALID:
            raise CouncilPlanExecutionError("; ".join(validated.validation.errors))

        snapshot = self._kubernetes.snapshot_workloads(
            rehearsal.namespace,
            rehearsal.plan.services,
        )
        applied: list[CouncilAction] = []
        try:
            for action in validated.actions:
                self._kubernetes.apply_council_action(action)
                applied.append(action)
        except Exception as exc:
            self._kubernetes.rollback_workloads(snapshot)
            return _report(
                rehearsal.run_id,
                validated.plan_id,
                ExperimentStatus.UNSUCCESSFUL,
                baseline,
                pressure_before,
                pressure_before.model_copy(update={"phase": "post_change"}),
                applied,
                (f"action application failed: {exc}",),
            ), snapshot

        pressure_after = self._load_tests.run(rehearsal.namespace, scenario, "post_change")
        audit = self._auditor.audit(
            _audit_payload(validated, baseline, pressure_before, pressure_after, applied)
        )
        status, errors = _evaluate_experiment(
            scenario,
            pressure_before,
            pressure_after,
            audit,
        )
        if status == ExperimentStatus.UNSUCCESSFUL:
            self._kubernetes.rollback_workloads(snapshot)

        return _report(
            rehearsal.run_id,
            validated.plan_id,
            status,
            baseline,
            pressure_before,
            pressure_after,
            applied,
            errors,
        ), snapshot

    def rollback(self, snapshot: CouncilWorkloadSnapshot) -> ValidationResult:
        self._kubernetes.rollback_workloads(snapshot)
        return ValidationResult(status=ValidationStatus.PASSED)

    def _ensure_plan_is_executable(
        self,
        rehearsal: RehearsalState,
        plan: CouncilPlan,
    ) -> None:
        if plan.run_id != rehearsal.run_id:
            raise CouncilPlanExecutionError("plan run_id does not match rehearsal")
        if plan.namespace != rehearsal.namespace:
            raise CouncilPlanExecutionError("plan namespace does not match rehearsal")
        if plan.status != CouncilPlanStatus.VALID:
            raise CouncilPlanExecutionError(f"plan is not valid: {plan.status}")
        if plan.validation.status != ValidationStatus.PASSED:
            raise CouncilPlanExecutionError("; ".join(plan.validation.errors))


def _evaluate_experiment(
    scenario: ScenarioSpec,
    pressure_before: LoadTestResult,
    pressure_after: LoadTestResult,
    audit: ExperimentAudit,
) -> tuple[ExperimentStatus, tuple[str, ...]]:
    errors: list[str] = []
    if pressure_after.success_rate < pressure_before.success_rate:
        errors.append("success rate decreased after applying the plan")
    if pressure_after.success_rate < scenario.objective.success_rate_minimum:
        errors.append("critical journey success rate is below the scenario objective")
    if pressure_after.p95_latency_ms > scenario.objective.p95_latency_ms_maximum:
        errors.append("critical journey p95 latency is above the scenario objective")
    if pressure_after.status == LoadTestStatus.FAILED:
        errors.extend(pressure_after.errors or ("post-change pressure test failed",))
    if audit.severe_regressions:
        errors.extend(f"auditor severe regression: {item}" for item in audit.severe_regressions)
    if audit.recommendation == "reject":
        errors.append(f"auditor rejected the experiment: {audit.summary}")

    if errors:
        return ExperimentStatus.UNSUCCESSFUL, tuple(errors)
    if audit.recommendation == "inconclusive":
        return ExperimentStatus.INCONCLUSIVE, (f"auditor inconclusive: {audit.summary}",)
    return ExperimentStatus.SUCCESSFUL, ()


def _report(
    run_id: str,
    plan_id: str,
    status: ExperimentStatus,
    baseline: LoadTestResult,
    pressure_before: LoadTestResult,
    pressure_after: LoadTestResult,
    applied_actions: Sequence[CouncilAction],
    errors: Sequence[str],
) -> ExperimentReport:
    validation_status = ValidationStatus.PASSED if not errors else ValidationStatus.FAILED
    return ExperimentReport(
        run_id=run_id,
        plan_id=plan_id,
        status=status,
        baseline=baseline,
        pressure_before=pressure_before,
        pressure_after=pressure_after,
        validation=ValidationResult(status=validation_status, errors=tuple(errors)),
        applied_actions=tuple(applied_actions),
        rollback_guidance="restore the recorded workload snapshot for this rehearsal namespace",
    )


def _audit_payload(
    plan: CouncilPlan,
    baseline: LoadTestResult,
    pressure_before: LoadTestResult,
    pressure_after: LoadTestResult,
    applied: Sequence[CouncilAction],
) -> dict[str, Any]:
    return {
        "plan": plan.model_dump(mode="json"),
        "baseline": baseline.model_dump(mode="json"),
        "pressure_before": pressure_before.model_dump(mode="json"),
        "pressure_after": pressure_after.model_dump(mode="json"),
        "applied_actions": [action.model_dump(mode="json") for action in applied],
    }
