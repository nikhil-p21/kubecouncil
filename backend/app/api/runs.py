from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.agents.council import AdkGeminiCouncilAgentClient, GeminiAdkCouncilRunner
from app.api.repositories import get_run_store
from app.domain.interfaces import (
    CouncilRunner,
    ExperimentAuditor,
    KubernetesClient,
    LoadTestRunner,
    ManifestRenderer,
    RunStore,
)
from app.domain.models import (
    AnalysisResult,
    CouncilPlan,
    CouncilWorkloadSnapshot,
    DependencyEdge,
    ExperimentReport,
    LoadTestResult,
    RehearsalState,
    RehearsalStatus,
    RepositorySnapshot,
    ResourceRequests,
    ScenarioResults,
    ValidationResult,
    ValidationStatus,
)
from app.kubernetes.client import KubectlKubernetesClient, KubernetesOperationError
from app.kubernetes.kustomize import (
    KustomizeManifestRenderer,
    ManifestParseError,
    ManifestRenderError,
)
from app.rehearsal.executor import CouncilPlanExecutionError, CouncilPlanExecutor
from app.rehearsal.planner import RehearsalPlanner, RehearsalPlanningError
from app.scenarios.k6 import (
    FLASH_SALE_SCENARIO,
    KubectlK6LoadTestRunner,
    LoadTestInfrastructureError,
    LoadTestOutputError,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])

_manifest_renderer = KustomizeManifestRenderer()
_rehearsal_planner = RehearsalPlanner()
_kubernetes_client = KubectlKubernetesClient()
_load_test_runner = KubectlK6LoadTestRunner()
_council_runner = GeminiAdkCouncilRunner()
_experiment_auditor = AdkGeminiCouncilAgentClient()


def get_manifest_renderer() -> ManifestRenderer:
    return _manifest_renderer


def get_rehearsal_planner() -> RehearsalPlanner:
    return _rehearsal_planner


def get_kubernetes_client() -> KubernetesClient:
    return _kubernetes_client


def get_load_test_runner() -> LoadTestRunner:
    return _load_test_runner


def get_council_runner() -> CouncilRunner:
    return _council_runner


def get_experiment_auditor() -> ExperimentAuditor:
    return _experiment_auditor


def error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.post(
    "/{run_id}/analyse",
    response_model=AnalysisResult,
    status_code=status.HTTP_200_OK,
)
def analyse_run(
    run_id: str,
    renderer: Annotated[ManifestRenderer, Depends(get_manifest_renderer)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> AnalysisResult:
    stored_snapshot = store.get(run_id, "repository_snapshot")
    if not isinstance(stored_snapshot, RepositorySnapshot):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail("repository_snapshot_not_found", "repository is not connected"),
        )

    try:
        source = renderer.render(stored_snapshot)
        services = tuple(renderer.service_profiles(source))
    except ManifestRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("manifest_render_failed", str(exc)),
        ) from exc
    except ManifestParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("manifest_parse_failed", str(exc)),
        ) from exc

    result = AnalysisResult(
        run_id=run_id,
        source=source,
        services=services,
        compatibility_issues=source.compatibility_issues,
        dependency_edges=tuple(
            DependencyEdge(from_service=service.name, to_service=dependency)
            for service in services
            for dependency in service.dependencies
        ),
    )
    store.put(run_id, "deployment_source", source)
    store.put(run_id, "service_profiles", services)
    store.put(run_id, "compatibility_issues", source.compatibility_issues)
    store.put(run_id, "analysis_result", result)
    return result


@router.post(
    "/{run_id}/rehearsal",
    response_model=RehearsalState,
    status_code=status.HTTP_201_CREATED,
)
def create_rehearsal(
    run_id: str,
    planner: Annotated[RehearsalPlanner, Depends(get_rehearsal_planner)],
    kubernetes: Annotated[KubernetesClient, Depends(get_kubernetes_client)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> RehearsalState:
    stored_analysis = store.get(run_id, "analysis_result")
    if not isinstance(stored_analysis, AnalysisResult):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail("analysis_not_found", "run has not been analysed"),
        )

    try:
        plan = planner.build_plan(stored_analysis)
    except RehearsalPlanningError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("rehearsal_planning_failed", str(exc)),
        ) from exc

    validation = kubernetes.validate_rehearsal(plan)
    if validation.status == ValidationStatus.FAILED:
        state = RehearsalState(
            run_id=run_id,
            namespace=plan.namespace,
            status=RehearsalStatus.FAILED,
            plan=plan,
            resources=(),
            readiness=validation,
            message="rehearsal validation failed",
        )
        store.put(run_id, "rehearsal_plan", plan)
        store.put(run_id, "rehearsal_state", state)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "rehearsal_validation_failed", "errors": list(validation.errors)},
        )

    try:
        resources = kubernetes.create_rehearsal(plan)
    except KubernetesOperationError as exc:
        state = RehearsalState(
            run_id=run_id,
            namespace=plan.namespace,
            status=RehearsalStatus.FAILED,
            plan=plan,
            resources=(),
            readiness=ValidationResult(status=ValidationStatus.FAILED, errors=(str(exc),)),
            message="rehearsal deployment failed",
        )
        store.put(run_id, "rehearsal_plan", plan)
        store.put(run_id, "rehearsal_state", state)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("rehearsal_deployment_failed", str(exc)),
        ) from exc

    state = RehearsalState(
        run_id=run_id,
        namespace=plan.namespace,
        status=RehearsalStatus.DEPLOYED,
        plan=plan,
        resources=resources,
        readiness=ValidationResult(status=ValidationStatus.PASSED),
    )
    store.put(run_id, "rehearsal_plan", plan)
    store.put(run_id, "rehearsal_state", state)
    return state


@router.get(
    "/{run_id}/rehearsal",
    response_model=RehearsalState,
    status_code=status.HTTP_200_OK,
)
def get_rehearsal(
    run_id: str,
    store: Annotated[RunStore, Depends(get_run_store)],
) -> RehearsalState:
    stored_state = store.get(run_id, "rehearsal_state")
    if not isinstance(stored_state, RehearsalState):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail("rehearsal_not_found", "rehearsal has not been created"),
        )
    return stored_state


@router.delete(
    "/{run_id}/rehearsal",
    response_model=RehearsalState,
    status_code=status.HTTP_200_OK,
)
def delete_rehearsal(
    run_id: str,
    kubernetes: Annotated[KubernetesClient, Depends(get_kubernetes_client)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> RehearsalState:
    stored_state = store.get(run_id, "rehearsal_state")
    if not isinstance(stored_state, RehearsalState):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail("rehearsal_not_found", "rehearsal has not been created"),
        )
    try:
        kubernetes.delete_rehearsal(stored_state.namespace)
    except KubernetesOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("rehearsal_cleanup_failed", str(exc)),
        ) from exc

    deleted = RehearsalState(
        run_id=run_id,
        namespace=stored_state.namespace,
        status=RehearsalStatus.DELETED,
        plan=stored_state.plan,
        resources=stored_state.resources,
        readiness=stored_state.readiness,
        message="rehearsal namespace deleted",
    )
    store.put(run_id, "rehearsal_state", deleted)
    return deleted


@router.post(
    "/{run_id}/baseline",
    response_model=LoadTestResult,
    status_code=status.HTTP_200_OK,
)
def run_baseline(
    run_id: str,
    runner: Annotated[LoadTestRunner, Depends(get_load_test_runner)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> LoadTestResult:
    return _run_scenario_phase(run_id, "baseline", runner, store)


@router.post(
    "/{run_id}/pressure",
    response_model=LoadTestResult,
    status_code=status.HTTP_200_OK,
)
def run_pressure(
    run_id: str,
    runner: Annotated[LoadTestRunner, Depends(get_load_test_runner)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> LoadTestResult:
    return _run_scenario_phase(run_id, "pressure", runner, store)


@router.get(
    "/{run_id}/results",
    response_model=ScenarioResults,
    status_code=status.HTTP_200_OK,
)
def get_results(
    run_id: str,
    store: Annotated[RunStore, Depends(get_run_store)],
) -> ScenarioResults:
    stored_results = store.get(run_id, "scenario_results")
    if isinstance(stored_results, ScenarioResults):
        return stored_results
    return ScenarioResults(run_id=run_id, scenario=FLASH_SALE_SCENARIO)


@router.post(
    "/{run_id}/council",
    response_model=CouncilPlan,
    status_code=status.HTTP_200_OK,
)
def run_council(
    run_id: str,
    runner: Annotated[CouncilRunner, Depends(get_council_runner)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> CouncilPlan:
    rehearsal = _deployed_rehearsal(run_id, store)
    pressure = _stored_load_result(run_id, "pressure_result", store)
    plan = runner.run(
        rehearsal.namespace,
        rehearsal.plan.services,
        FLASH_SALE_SCENARIO,
        pressure,
        run_id=run_id,
        resource_quota=ResourceRequests(
            cpu_millis=rehearsal.plan.resource_quota_cpu_millis,
            memory_mib=rehearsal.plan.resource_quota_memory_mib,
        ),
    )
    store.put(run_id, "council_plan", plan)
    store.put(run_id, f"council_plan:{plan.plan_id}", plan)
    return plan


@router.post(
    "/{run_id}/plans/{plan_id}/apply",
    response_model=ExperimentReport,
    status_code=status.HTTP_200_OK,
)
def apply_plan(
    run_id: str,
    plan_id: str,
    kubernetes: Annotated[KubernetesClient, Depends(get_kubernetes_client)],
    load_tests: Annotated[LoadTestRunner, Depends(get_load_test_runner)],
    auditor: Annotated[ExperimentAuditor, Depends(get_experiment_auditor)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> ExperimentReport:
    rehearsal = _deployed_rehearsal(run_id, store)
    plan = _stored_plan(run_id, plan_id, store)
    baseline = _stored_load_result(run_id, "baseline_result", store)
    pressure = _stored_load_result(run_id, "pressure_result", store)
    executor = CouncilPlanExecutor(kubernetes, load_tests, auditor)
    try:
        report, snapshot = executor.apply_and_verify(
            rehearsal,
            plan,
            FLASH_SALE_SCENARIO,
            baseline,
            pressure,
        )
    except CouncilPlanExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("council_plan_not_executable", str(exc)),
        ) from exc

    _persist_experiment(run_id, report, snapshot, store)
    return report


@router.post(
    "/{run_id}/verify",
    response_model=ExperimentReport,
    status_code=status.HTTP_200_OK,
)
def verify_run(
    run_id: str,
    store: Annotated[RunStore, Depends(get_run_store)],
) -> ExperimentReport:
    stored_report = store.get(run_id, "experiment_report")
    if not isinstance(stored_report, ExperimentReport):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("experiment_not_applied", "no applied plan is available to verify"),
        )
    return stored_report


@router.post(
    "/{run_id}/rollback",
    response_model=ValidationResult,
    status_code=status.HTTP_200_OK,
)
def rollback_run(
    run_id: str,
    kubernetes: Annotated[KubernetesClient, Depends(get_kubernetes_client)],
    store: Annotated[RunStore, Depends(get_run_store)],
) -> ValidationResult:
    snapshot = store.get(run_id, "council_workload_snapshot")
    if not isinstance(snapshot, CouncilWorkloadSnapshot):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("rollback_snapshot_not_found", "no council snapshot is available"),
        )
    try:
        validation = CouncilPlanExecutor(
            kubernetes,
            _load_test_runner,
            _experiment_auditor,
        ).rollback(snapshot)
    except KubernetesOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("rollback_failed", str(exc)),
        ) from exc
    store.put(run_id, "rollback_result", validation)
    return validation


def _run_scenario_phase(
    run_id: str,
    phase: str,
    runner: LoadTestRunner,
    store: RunStore,
) -> LoadTestResult:
    stored_state = store.get(run_id, "rehearsal_state")
    if (
        not isinstance(stored_state, RehearsalState)
        or stored_state.status != RehearsalStatus.DEPLOYED
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("rehearsal_not_deployed", "run does not have a deployed rehearsal"),
        )

    try:
        result = runner.run(stored_state.namespace, FLASH_SALE_SCENARIO, phase)
    except LoadTestOutputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("load_test_output_malformed", str(exc)),
        ) from exc
    except LoadTestInfrastructureError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail("load_test_infrastructure_failed", str(exc)),
        ) from exc

    existing = store.get(run_id, "scenario_results")
    results = (
        existing
        if isinstance(existing, ScenarioResults)
        else ScenarioResults(run_id=run_id, scenario=FLASH_SALE_SCENARIO)
    )
    if phase == "baseline":
        results = results.model_copy(update={"baseline": result})
        store.put(run_id, "baseline_result", result)
    elif phase == "pressure":
        results = results.model_copy(update={"pressure": result})
        store.put(run_id, "pressure_result", result)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("unsupported_load_test_phase", f"unsupported phase: {phase}"),
        )
    store.put(run_id, "scenario", FLASH_SALE_SCENARIO)
    store.put(run_id, "scenario_results", results)
    return result


def _deployed_rehearsal(run_id: str, store: RunStore) -> RehearsalState:
    stored_state = store.get(run_id, "rehearsal_state")
    if (
        not isinstance(stored_state, RehearsalState)
        or stored_state.status != RehearsalStatus.DEPLOYED
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("rehearsal_not_deployed", "run does not have a deployed rehearsal"),
        )
    return stored_state


def _stored_load_result(run_id: str, key: str, store: RunStore) -> LoadTestResult:
    result = store.get(run_id, key)
    if not isinstance(result, LoadTestResult):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail("load_test_result_not_found", f"{key} is not available"),
        )
    return result


def _stored_plan(run_id: str, plan_id: str, store: RunStore) -> CouncilPlan:
    stored_plan = store.get(run_id, f"council_plan:{plan_id}")
    if isinstance(stored_plan, CouncilPlan):
        return stored_plan
    fallback = store.get(run_id, "council_plan")
    if isinstance(fallback, CouncilPlan) and fallback.plan_id == plan_id:
        return fallback
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_detail("council_plan_not_found", "council plan was not found"),
    )


def _persist_experiment(
    run_id: str,
    report: ExperimentReport,
    snapshot: CouncilWorkloadSnapshot,
    store: RunStore,
) -> None:
    store.put(run_id, "council_workload_snapshot", snapshot)
    store.put(run_id, "experiment_report", report)
    existing = store.get(run_id, "scenario_results")
    if isinstance(existing, ScenarioResults):
        store.put(
            run_id,
            "scenario_results",
            existing.model_copy(update={"post_change": report.pressure_after}),
        )
