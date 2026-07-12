"""Narrow real-provider adapters used only by the deployed incident-response profile."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

import yaml
from pydantic import ValidationError

from app.domain.incidents import (
    ActionConvergenceStatus,
    AlertSignal,
    ApplicationProfile,
    ApplicationProfileLoadResult,
    DeploymentConvergenceResult,
    DeploymentPatch,
    DeploymentPolicyState,
    DeploymentRevision,
    DryRunResult,
    EnrollmentSnapshot,
    EvidenceProviderRequest,
    EvidenceQueryAdapter,
    EvidenceQueryKind,
    EvidenceSource,
    EvidenceWindow,
    NamespaceEnrollmentState,
    ProfileValidationIssue,
    RawEvidenceObservation,
    RoleBindingEnrollmentState,
    WorkloadEnrollmentState,
    WorkloadReference,
)
from app.services.evidence_gateway import (
    CloudLoggingEvidenceAdapter,
    CloudMonitoringEvidenceAdapter,
    KubernetesEvidenceAdapter,
)


class _HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]: ...


class _AuthorizedSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        timeout: float,
    ) -> _HttpResponse: ...

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> _HttpResponse: ...


class FileApplicationProfileProvider:
    """Loads one mounted ConfigMap document and retains exact typed validation failures."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def list_profiles(self) -> tuple[ApplicationProfileLoadResult, ...]:
        source = f"file://{self._path}"
        try:
            document = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("Application Profile YAML must contain one mapping")
            profile = ApplicationProfile.model_validate(document)
        except (OSError, ValueError, ValidationError) as error:
            if isinstance(error, ValidationError):
                issues = tuple(
                    ProfileValidationIssue(
                        location=".".join(str(part) for part in item["loc"]),
                        message=item["msg"],
                    )
                    for item in error.errors()
                )
                application_id = (
                    document.get("application_id")
                    if "document" in locals() and isinstance(document, dict)
                    else None
                )
            else:
                issues = (ProfileValidationIssue(location="profile", message=str(error)),)
                application_id = None
            return (
                ApplicationProfileLoadResult(
                    source=source,
                    application_id=(application_id if isinstance(application_id, str) else None),
                    errors=issues,
                ),
            )
        return (
            ApplicationProfileLoadResult(
                source=source,
                application_id=profile.application_id,
                profile=profile,
            ),
        )


class KubernetesEnrollmentProvider:
    """Reads only namespace, Deployment, RoleBinding, and admission-binding prerequisites."""

    def __init__(
        self,
        *,
        core_api: Any,
        apps_api: Any,
        rbac_api: Any,
        admission_api: Any,
        admission_policy_binding: str,
        inspect_protected_dependencies: bool = True,
    ) -> None:
        self._core = core_api
        self._apps = apps_api
        self._rbac = rbac_api
        self._admission = admission_api
        self._admission_policy_binding = admission_policy_binding
        self._inspect_protected_dependencies = inspect_protected_dependencies

    def inspect(self, profile: ApplicationProfile) -> EnrollmentSnapshot:
        namespaces = tuple(self._namespace_state(namespace) for namespace in profile.namespaces)
        workloads = tuple(
            self._workload_state(workload.reference)
            if workload.executable or self._inspect_protected_dependencies
            else WorkloadEnrollmentState(reference=workload.reference, exists=True)
            for workload in profile.workloads
        )
        bindings: list[RoleBindingEnrollmentState] = []
        for namespace in profile.namespaces:
            items = self._rbac.list_namespaced_role_binding(namespace).items
            for identity, role in (
                (profile.investigator_identity, profile.investigator_role),
                (profile.executor_identity, profile.executor_role),
            ):
                subject_namespace, subject_name = _service_account_identity(identity)
                exists = any(
                    item.role_ref.kind == "Role"
                    and item.role_ref.name == role
                    and any(
                        subject.kind == "ServiceAccount"
                        and subject.name == subject_name
                        and subject.namespace == subject_namespace
                        for subject in (item.subjects or ())
                    )
                    for item in items
                )
                bindings.append(
                    RoleBindingEnrollmentState(
                        namespace=namespace,
                        subject=identity,
                        role=role,
                        exists=exists,
                    )
                )
        try:
            self._admission.read_validating_admission_policy_binding(
                self._admission_policy_binding
            )
            admission_ready = True
        except Exception as error:
            if not _is_not_found(error):
                raise
            admission_ready = False
        return EnrollmentSnapshot(
            namespaces=namespaces,
            workloads=workloads,
            role_bindings=tuple(bindings),
            admission_policy_binding=admission_ready,
        )

    def _namespace_state(self, namespace: str) -> NamespaceEnrollmentState:
        try:
            value = self._core.read_namespace(namespace)
            return NamespaceEnrollmentState(
                namespace=namespace,
                exists=True,
                labels=dict(value.metadata.labels or {}),
            )
        except Exception as error:
            if not _is_not_found(error):
                raise
            return NamespaceEnrollmentState(namespace=namespace, exists=False)

    def _workload_state(self, target: WorkloadReference) -> WorkloadEnrollmentState:
        try:
            value = self._apps.read_namespaced_deployment(target.name, target.namespace)
            return WorkloadEnrollmentState(
                reference=target,
                exists=True,
                labels=dict(value.metadata.labels or {}),
            )
        except Exception as error:
            if not _is_not_found(error):
                raise
            return WorkloadEnrollmentState(reference=target, exists=False)


class KubernetesIncidentProvider:
    """Purpose-built read and Deployment mutation adapter; no general resource operation exists."""

    def __init__(
        self,
        *,
        core_api: Any,
        apps_api: Any,
        api_client: Any,
        dry_run_apps_api: Any | None = None,
    ) -> None:
        self._core = core_api
        self._apps = apps_api
        self._dry_run_apps = dry_run_apps_api or apps_api
        self._api_client = api_client
        self._rollback_revision_overrides: dict[WorkloadReference, int] = {}

    def read_workload_state(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_items: int,
        deadline_seconds: float,
    ) -> str:
        deployment = self._apps.read_namespaced_deployment(
            workload, namespace, _request_timeout=deadline_seconds
        )
        pods = self._pods(namespace, workload)[:maximum_items]
        payload = {
            "deployment": {
                "name": deployment.metadata.name,
                "resource_version": deployment.metadata.resource_version,
                "generation": deployment.metadata.generation,
                "observed_generation": deployment.status.observed_generation,
                "replicas": deployment.spec.replicas,
                "updated_replicas": deployment.status.updated_replicas or 0,
                "available_replicas": deployment.status.available_replicas or 0,
                "unavailable_replicas": deployment.status.unavailable_replicas or 0,
                "revision": (deployment.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision", "unknown"
                ),
                "conditions": [
                    {
                        "type": condition.type,
                        "status": condition.status,
                        "reason": condition.reason,
                    }
                    for condition in (deployment.status.conditions or ())
                ],
            },
            "pods": [_safe_pod_status(pod) for pod in pods],
        }
        return json.dumps(payload, sort_keys=True)

    def read_pod_events(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_items: int,
        deadline_seconds: float,
    ) -> str:
        pod_names = {pod.metadata.name for pod in self._pods(namespace, workload)}
        events = self._core.list_namespaced_event(
            namespace, _request_timeout=deadline_seconds
        ).items
        safe = [
            {
                "reason": event.reason,
                "type": event.type,
                "count": event.count,
                "object": event.involved_object.name,
                "message": event.message,
                "observed_at": str(event.last_timestamp or event.event_time or ""),
            }
            for event in events
            if event.involved_object.name in pod_names
        ][-maximum_items:]
        return json.dumps(safe, sort_keys=True)

    def read_pod_logs(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_lines: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> str:
        lines: list[str] = []
        since_seconds = max(1, round((ended_at - started_at).total_seconds()))
        for pod in self._pods(namespace, workload):
            containers = [container.name for container in (pod.spec.containers or ())]
            for container in containers:
                value = self._core.read_namespaced_pod_log(
                    pod.metadata.name,
                    namespace,
                    container=container,
                    timestamps=True,
                    tail_lines=maximum_lines,
                    since_seconds=since_seconds,
                    _request_timeout=deadline_seconds,
                )
                lines.extend(
                    f"{pod.metadata.name}/{container}: {line}"
                    for line in value.splitlines()
                )
                if len(lines) >= maximum_lines:
                    return "\n".join(lines[:maximum_lines])
        return "\n".join(lines[:maximum_lines]) or "No bounded pod log lines were returned."

    def read_change_history(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_items: int,
        deadline_seconds: float,
    ) -> str:
        revisions = self._revisions(
            WorkloadReference(namespace=namespace, name=workload),
            deadline_seconds=deadline_seconds,
        )
        payload = [
            {
                "revision": revision.revision,
                "restorable": revision.restorable,
                "implicated": revision.implicated,
                "pod_template": _evidence_safe_template(revision.pod_template),
            }
            for revision in revisions[-maximum_items:]
        ]
        return json.dumps(payload, sort_keys=True)

    def inspect_deployment(self, target: WorkloadReference) -> DeploymentPolicyState | None:
        try:
            deployment = self._apps.read_namespaced_deployment(target.name, target.namespace)
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        revisions = self._revisions(target, deadline_seconds=10)
        raw_revision = int(
            (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "1")
        )
        current_revision = self._rollback_revision_overrides.get(target, raw_revision)
        if current_revision != raw_revision:
            revisions = tuple(
                revision.model_copy(update={"implicated": revision.revision == current_revision})
                for revision in revisions
            )
        return DeploymentPolicyState(
            target=target,
            resource_version=str(deployment.metadata.resource_version),
            generation=int(deployment.metadata.generation or 1),
            replicas=int(deployment.spec.replicas or 0),
            current_revision=current_revision,
            available_revisions=revisions,
            active_intervention=False,
            replica_quota_headroom=0,
        )

    def dry_run_deployment_patch(self, patch: DeploymentPatch) -> DryRunResult:
        try:
            self._dry_run_apps.patch_namespaced_deployment(
                patch.target.name,
                patch.target.namespace,
                patch.body,
                dry_run="All",
                field_manager="kubecouncil-executor",
                _content_type="application/strategic-merge-patch+json",
            )
        except Exception as error:
            return DryRunResult(accepted=False, error=_safe_provider_error(error))
        state = self.inspect_deployment(patch.target)
        if state is None:
            return DryRunResult(accepted=False, error="Deployment disappeared after dry-run")
        if patch.action_type == "scale_deployment":
            desired = cast(dict[str, object], patch.body["spec"]).get("replicas")
            diff = f"Deployment/{patch.target.name}: replicas {state.replicas} -> {desired}"
        elif patch.action_type == "rollback_deployment":
            revision = self._revision_for_template(state, patch)
            diff = (
                f"Deployment/{patch.target.name}: active revision {state.current_revision} "
                f"-> approved revision {revision}"
            )
        else:
            diff = f"Deployment/{patch.target.name}: controlled restart token added"
        return DryRunResult(accepted=True, diff=diff)

    def apply_deployment_patch(self, patch: DeploymentPatch) -> DeploymentPolicyState:
        current = self.inspect_deployment(patch.target)
        if current is None or current.resource_version != patch.resource_version:
            raise ValueError("optimistic concurrency resourceVersion mismatch")
        rollback_revision = (
            self._revision_for_template(current, patch)
            if patch.action_type == "rollback_deployment"
            else None
        )
        self._apps.patch_namespaced_deployment(
            patch.target.name,
            patch.target.namespace,
            patch.body,
            field_manager="kubecouncil-executor",
            _content_type="application/strategic-merge-patch+json",
        )
        if rollback_revision is not None:
            self._rollback_revision_overrides[patch.target] = rollback_revision
        updated = self.inspect_deployment(patch.target)
        if updated is None:
            raise ValueError("Deployment disappeared after mutation")
        return updated

    def verify_deployment_convergence(
        self,
        patch: DeploymentPatch,
        applied_state: DeploymentPolicyState,
    ) -> DeploymentConvergenceResult:
        deadline = time.monotonic() + 180
        latest: DeploymentPolicyState | None = applied_state
        while time.monotonic() < deadline:
            latest = self.inspect_deployment(patch.target)
            deployment = self._apps.read_namespaced_deployment(
                patch.target.name, patch.target.namespace
            )
            desired = int(deployment.spec.replicas or 0)
            if (
                deployment.status.observed_generation == deployment.metadata.generation
                and int(deployment.status.updated_replicas or 0) == desired
                and int(deployment.status.available_replicas or 0) == desired
                and int(deployment.status.unavailable_replicas or 0) == 0
            ):
                return DeploymentConvergenceResult(
                    status=ActionConvergenceStatus.SUCCEEDED,
                    reason="Deployment reached observed generation and full availability.",
                    observed_state=latest,
                )
            progressing = next(
                (
                    condition
                    for condition in (deployment.status.conditions or ())
                    if condition.type == "Progressing"
                ),
                None,
            )
            if progressing is not None and progressing.reason == "ProgressDeadlineExceeded":
                return DeploymentConvergenceResult(
                    status=ActionConvergenceStatus.FAILED,
                    reason="Deployment exceeded its rollout progress deadline.",
                    observed_state=latest,
                )
            time.sleep(2)
        return DeploymentConvergenceResult(
            status=ActionConvergenceStatus.AMBIGUOUS,
            reason="Deployment convergence deadline elapsed without a provable result.",
            observed_state=latest,
        )

    def _pods(self, namespace: str, workload: str) -> list[Any]:
        return list(
            self._core.list_namespaced_pod(
                namespace, label_selector=f"app={workload}"
            ).items
        )

    def _revisions(
        self, target: WorkloadReference, *, deadline_seconds: float
    ) -> tuple[DeploymentRevision, ...]:
        replicasets = self._apps.list_namespaced_replica_set(
            target.namespace,
            label_selector=f"app={target.name}",
            _request_timeout=deadline_seconds,
        ).items
        values: list[DeploymentRevision] = []
        current = 1
        try:
            deployment = self._apps.read_namespaced_deployment(
                target.name, target.namespace, _request_timeout=deadline_seconds
            )
            current = int(
                (deployment.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision", "1"
                )
            )
        except Exception:
            pass
        for item in replicasets:
            annotation = (item.metadata.annotations or {}).get(
                "deployment.kubernetes.io/revision"
            )
            if annotation is None:
                continue
            template = cast(
                dict[str, object], self._api_client.sanitize_for_serialization(item.spec.template)
            )
            _remove_controller_hash(template)
            values.append(
                DeploymentRevision(
                    revision=int(annotation),
                    pod_template=template,
                    restorable=not _template_uses_secret(template),
                    implicated=int(annotation) == current,
                )
            )
        return tuple(sorted(values, key=lambda value: value.revision))

    @staticmethod
    def _revision_for_template(state: DeploymentPolicyState, patch: DeploymentPatch) -> int:
        spec = patch.body.get("spec")
        template = spec.get("template") if isinstance(spec, dict) else None
        if not isinstance(template, dict):
            raise ValueError("rollback patch is missing its approved pod template")
        for revision in state.available_revisions:
            if revision.pod_template == template:
                return revision.revision
        raise ValueError("rollback patch template no longer matches an available revision")


class GoogleCloudLoggingReader:
    def __init__(self, session: _AuthorizedSession, *, project_id: str) -> None:
        self._session = session
        self._project_id = project_id

    def query_workload_logs(
        self,
        *,
        namespace: str,
        workload: str,
        maximum_lines: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> tuple[str, ...]:
        escaped_workload = workload.replace('"', "")
        body: dict[str, object] = {
            "resourceNames": [f"projects/{self._project_id}"],
            "filter": (
                'resource.type="k8s_container" AND '
                f'resource.labels.namespace_name="{namespace}" AND '
                f'resource.labels.pod_name=~"^{escaped_workload}-" AND '
                f'timestamp>="{started_at.isoformat()}" AND '
                f'timestamp<="{ended_at.isoformat()}"'
            ),
            "orderBy": "timestamp desc",
            "pageSize": maximum_lines,
        }
        response = self._session.post(
            "https://logging.googleapis.com/v2/entries:list",
            json=body,
            timeout=deadline_seconds,
        )
        response.raise_for_status()
        entries = response.json().get("entries", [])
        lines = []
        for entry in entries[:maximum_lines] if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            payload = entry.get(
                "textPayload", entry.get("jsonPayload", entry.get("protoPayload"))
            )
            lines.append(
                json.dumps(payload, sort_keys=True)
                if not isinstance(payload, str)
                else payload
            )
        return tuple(lines) or ("No bounded Cloud Logging entries were returned.",)


class GoogleCloudMonitoringReader:
    def __init__(self, session: _AuthorizedSession, *, project_id: str) -> None:
        self._session = session
        self._project_id = project_id

    def query_time_series(
        self,
        *,
        query: str,
        namespace: str,
        workload: str,
        maximum_series: int,
        started_at: datetime,
        ended_at: datetime,
        deadline_seconds: float,
    ) -> tuple[str, ...]:
        url = (
            f"https://monitoring.googleapis.com/v1/projects/{quote(self._project_id)}/"
            "location/global/prometheus/api/v1/query_range"
        )
        response = self._session.get(
            url,
            params={
                "query": query,
                "start": started_at.isoformat(),
                "end": ended_at.isoformat(),
                "step": "30s",
                "timeout": f"{int(deadline_seconds)}s",
            },
            timeout=deadline_seconds,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        results = data.get("result", []) if isinstance(data, dict) else []
        series = tuple(
            json.dumps(item, sort_keys=True)
            for item in (results[:maximum_series] if isinstance(results, list) else [])
        )
        return series or (json.dumps({"status": "no_series_returned"}),)

    def lookup_alert_policy(self, *, identifier: str, deadline_seconds: float) -> str:
        response = self._session.get(
            f"https://monitoring.googleapis.com/v3/projects/{quote(self._project_id)}/alertPolicies",
            params={"filter": f'display_name="{identifier}"', "pageSize": 10},
            timeout=deadline_seconds,
        )
        response.raise_for_status()
        policies = response.json().get("alertPolicies", [])
        return json.dumps(policies if isinstance(policies, list) else [], sort_keys=True)


class LiveInitialEvidenceProvider:
    """Builds the immutable initial window through the same scoped adapters as follow-ups."""

    def __init__(
        self,
        *,
        kubernetes: KubernetesEvidenceAdapter,
        logging: CloudLoggingEvidenceAdapter,
        monitoring: CloudMonitoringEvidenceAdapter,
        deadline_seconds: float = 10,
    ) -> None:
        self._adapters: dict[EvidenceSource, EvidenceQueryAdapter] = {
            EvidenceSource.KUBERNETES: kubernetes,
            EvidenceSource.CLOUD_LOGGING: logging,
            EvidenceSource.CLOUD_MONITORING: monitoring,
        }
        self._deadline_seconds = deadline_seconds

    def collect_initial(
        self,
        profile: ApplicationProfile,
        signal: AlertSignal,
        window: EvidenceWindow,
    ) -> tuple[RawEvidenceObservation, ...]:
        target = WorkloadReference(namespace=signal.namespace, name=signal.workload_name)
        observations: list[RawEvidenceObservation] = []
        for index, mapping in enumerate(profile.evidence_mappings):
            if mapping.scope != target:
                continue
            maximum_items = (
                profile.evidence_budget.maximum_log_lines
                if mapping.kind is EvidenceQueryKind.POD_LOGS
                else profile.evidence_budget.maximum_metric_series
                if mapping.kind is EvidenceQueryKind.METRICS
                else 50
            )
            request = EvidenceProviderRequest(
                query_id=f"initial-{signal.signal_id}-{index}",
                incident_id=f"pending-{signal.signal_id}",
                source=mapping.source,
                kind=mapping.kind,
                scope=mapping.scope,
                mapping_identifier=mapping.identifier,
                query_template=mapping.query_template,
                started_at=window.started_at,
                ended_at=window.ended_at,
                maximum_items=maximum_items,
                deadline_seconds=self._deadline_seconds,
            )
            adapter = self._adapters.get(mapping.source)
            if adapter is None:
                raise RuntimeError(f"required evidence adapter is unavailable: {mapping.source}")
            observations.append(adapter.query(request))
        if not observations:
            raise RuntimeError("Application Profile has no evidence mappings for the alert target")
        return tuple(observations)


def create_authorized_session() -> _AuthorizedSession:
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as error:
        raise RuntimeError(
            "google-auth is required for deployed observability providers"
        ) from error
    credentials, _ = cast(Any, google.auth.default)(
        scopes=(
            "https://www.googleapis.com/auth/logging.read",
            "https://www.googleapis.com/auth/monitoring.read",
        )
    )
    return cast(_AuthorizedSession, cast(Any, AuthorizedSession)(credentials))


def create_kubernetes_clients() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from kubernetes import client, config
    except ImportError as error:
        raise RuntimeError("kubernetes client is required in deployed mode") from error
    config.load_incluster_config()
    api_client = client.ApiClient()
    return (
        client.CoreV1Api(api_client),
        client.AppsV1Api(api_client),
        client.RbacAuthorizationV1Api(api_client),
        client.AdmissionregistrationV1Api(api_client),
        api_client,
    )


def create_impersonated_apps_client(identity: str) -> Any:
    """Create a client whose authority is constrained by RBAC and admission policy."""

    try:
        from kubernetes import client
    except ImportError as error:
        raise RuntimeError("kubernetes client is required in deployed mode") from error
    api_client = client.ApiClient()
    api_client.default_headers["Impersonate-User"] = identity
    return client.AppsV1Api(api_client)


def _service_account_identity(identity: str) -> tuple[str, str]:
    parts = identity.split(":")
    if len(parts) != 3 or parts[0] != "serviceaccount":
        raise ValueError("Application Profile identity must use serviceaccount:<namespace>:<name>")
    return parts[1], parts[2]


def _is_not_found(error: Exception) -> bool:
    return getattr(error, "status", None) == 404


def _safe_provider_error(error: Exception) -> str:
    reason = getattr(error, "reason", None)
    return str(reason)[:500] if reason else type(error).__name__


def _remove_controller_hash(template: dict[str, object]) -> None:
    metadata = template.get("metadata")
    if not isinstance(metadata, dict):
        return
    labels = metadata.get("labels")
    if isinstance(labels, dict):
        labels.pop("pod-template-hash", None)


def _template_uses_secret(template: dict[str, object]) -> bool:
    spec = template.get("spec")
    containers = spec.get("containers", []) if isinstance(spec, dict) else []
    if not isinstance(containers, list):
        return True
    return any(
        isinstance(container, dict)
        and (
            container.get("envFrom")
            or any(
                isinstance(item, dict)
                and isinstance(item.get("valueFrom"), dict)
                and "secretKeyRef" in cast(dict[str, object], item["valueFrom"])
                for item in (
                    container.get("env", [])
                    if isinstance(container.get("env"), list)
                    else []
                )
            )
        )
        for container in containers
    )


def _evidence_safe_template(template: dict[str, object]) -> dict[str, object]:
    safe = json.loads(json.dumps(template))
    spec = safe.get("spec")
    containers = spec.get("containers", []) if isinstance(spec, dict) else []
    for container in containers if isinstance(containers, list) else []:
        if isinstance(container, dict):
            container.pop("env", None)
            container.pop("envFrom", None)
    return cast(dict[str, object], safe)


def _safe_pod_status(pod: Any) -> dict[str, object]:
    statuses = []
    for status in pod.status.container_statuses or ():
        last_state = status.last_state
        terminated = getattr(last_state, "terminated", None)
        waiting = getattr(status.state, "waiting", None)
        statuses.append(
            {
                "name": status.name,
                "ready": status.ready,
                "restart_count": status.restart_count,
                "last_termination_reason": getattr(terminated, "reason", None),
                "waiting_reason": getattr(waiting, "reason", None),
            }
        )
    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "conditions": [
            {"type": condition.type, "status": condition.status, "reason": condition.reason}
            for condition in (pod.status.conditions or ())
        ],
        "containers": statuses,
    }


__all__ = [
    "FileApplicationProfileProvider",
    "GoogleCloudLoggingReader",
    "GoogleCloudMonitoringReader",
    "KubernetesEnrollmentProvider",
    "KubernetesIncidentProvider",
    "LiveInitialEvidenceProvider",
    "create_authorized_session",
    "create_kubernetes_clients",
]
