"""Read-only inspection and sanitization for the live bootstrap environment."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.bootstrap.models import (
    BootstrapObservation,
    DeploymentProfile,
    ObservationStatus,
    PreflightCheck,
    PreflightReport,
)
from app.bootstrap.planner import BootstrapPlanner

_SENSITIVE_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"/[^\s]*/\.config/gcloud/[^\s]*"),
    re.compile(r"GOOGLE_APPLICATION_CREDENTIALS=[^\s]+"),
)


def sanitize_command_output(value: str) -> str:
    sanitized = value
    for pattern in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub("<redacted>", sanitized)
    return sanitized


def parse_cpu_millis(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def parse_memory_bytes(value: str) -> int:
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
    }
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return int(float(value.removesuffix(suffix)) * multiplier)
    return int(value)


def parse_duration_seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    suffix = value[-1:]
    if suffix in units:
        return int(value[:-1]) * units[suffix]
    return int(value)


def version_at_least(actual: str, minimum: str) -> bool:
    def numeric_prefix(value: str) -> tuple[int, ...]:
        matched = re.match(r"^[0-9]+(?:\.[0-9]+)*", value)
        if matched is None:
            raise ValueError("version has no numeric prefix")
        return tuple(int(part) for part in matched.group().split("."))

    try:
        actual_parts = numeric_prefix(actual)
        minimum_parts = numeric_prefix(minimum)
    except ValueError:
        return False
    width = max(len(actual_parts), len(minimum_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= minimum_parts + (0,) * (
        width - len(minimum_parts)
    )


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def kubectl_authorization_is_denied(result: CommandResult) -> bool:
    """Distinguish an expected `can-i` denial from an execution failure."""

    return result.returncode in {0, 1} and result.stdout.strip() == "no"


class CaptureCommandRunner(Protocol):
    def capture(self, command: tuple[str, ...], *, timeout: int = 30) -> CommandResult: ...


class SubprocessCommandRunner:
    def capture(self, command: tuple[str, ...], *, timeout: int = 30) -> CommandResult:
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=sanitize_command_output(completed.stdout),
            stderr=sanitize_command_output(completed.stderr),
        )

    def run(self, command: tuple[str, ...]) -> None:
        result = self.capture(command, timeout=600)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise RuntimeError(f"bootstrap command failed: {message}")


class LiveEnvironmentInspector:
    """Collects only the non-secret facts needed to build a deterministic plan."""

    def __init__(self, runner: CaptureCommandRunner | None = None) -> None:
        self._runner = runner or SubprocessCommandRunner()

    def inspect(self, profile: DeploymentProfile) -> PreflightReport:
        checks: list[PreflightCheck] = []
        observations: dict[str, BootstrapObservation] = {}
        self._inspect_tools(checks)
        self._inspect_account_and_billing(profile, checks)
        self._inspect_apis(profile, observations)
        self._inspect_cluster(profile, checks, observations)
        self._inspect_registry(profile, observations)
        self._inspect_firestore(profile, observations)
        self._inspect_pubsub(profile, observations)
        project_policy = self._inspect_identities(profile, checks, observations)
        self._inspect_resource_iam(profile, checks)
        self._inspect_kubernetes_identities(profile, observations)
        self._inspect_workload_identity(profile, observations)
        self._inspect_github(profile, observations)
        self._inspect_images(profile, observations)
        self._inspect_negative_iam(profile, checks, project_policy)
        checks.append(
            PreflightCheck(
                check_id="iap-principals",
                passed=bool(
                    profile.iap.viewer_principals and profile.iap.responder_principals
                ),
                summary=(
                    "Viewer and Responder principal lists are explicit."
                    if profile.iap.viewer_principals and profile.iap.responder_principals
                    else "Viewer and Responder principal lists must be supplied before KC-25."
                ),
            )
        )
        for requirement in BootstrapPlanner().requirements(profile):
            observations.setdefault(
                requirement.resource_id,
                BootstrapObservation(
                    resource_id=requirement.resource_id,
                    status=ObservationStatus.MISSING,
                    summary="not found",
                ),
            )
        return PreflightReport(
            profile_id=profile.profile_id,
            generated_at=datetime.now(UTC),
            checks=tuple(checks),
            observations=tuple(
                observations[requirement.resource_id]
                for requirement in BootstrapPlanner().requirements(profile)
            ),
        )

    def _inspect_tools(self, checks: list[PreflightCheck]) -> None:
        for tool in ("gcloud", "kubectl", "docker", "git"):
            present = shutil.which(tool) is not None
            checks.append(
                PreflightCheck(
                    check_id=f"tool-{tool}",
                    passed=present,
                    summary=f"{tool} is {'available' if present else 'missing'}.",
                )
            )

    def _inspect_account_and_billing(
        self, profile: DeploymentProfile, checks: list[PreflightCheck]
    ) -> None:
        project = self._text(("gcloud", "config", "get-value", "project"))
        checks.append(
            PreflightCheck(
                check_id="active-project",
                passed=project == profile.project_id,
                summary=(
                    "Active gcloud project matches the profile."
                    if project == profile.project_id
                    else "Active gcloud project does not match the profile."
                ),
            )
        )
        account = self._text(
            (
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
            )
        )
        checks.append(
            PreflightCheck(
                check_id="active-account",
                passed=bool(account),
                summary=(
                    "An active gcloud account is configured."
                    if account
                    else "No active gcloud account is configured."
                ),
            )
        )
        billing = self._json(
            (
                "gcloud",
                "billing",
                "projects",
                "describe",
                profile.project_id,
                "--format=json",
            )
        )
        billing_enabled = isinstance(billing, dict) and billing.get("billingEnabled") is True
        checks.append(
            PreflightCheck(
                check_id="billing",
                passed=billing_enabled,
                summary=(
                    "Billing is enabled for the project."
                    if billing_enabled
                    else "Billing is disabled or could not be verified."
                ),
            )
        )

    def _inspect_apis(
        self,
        profile: DeploymentProfile,
        observations: dict[str, BootstrapObservation],
    ) -> None:
        result = self._json(
            (
                "gcloud",
                "services",
                "list",
                "--enabled",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        api_items = result if isinstance(result, list) else []
        enabled = {
            str(item.get("config", {}).get("name"))
            for item in api_items
            if isinstance(item, dict)
        }
        for api in profile.required_apis:
            observations[f"api/{api}"] = BootstrapObservation(
                resource_id=f"api/{api}",
                status=(
                    ObservationStatus.COMPATIBLE
                    if api in enabled
                    else ObservationStatus.MISSING
                ),
                summary="enabled" if api in enabled else "disabled",
            )

    def _inspect_cluster(
        self,
        profile: DeploymentProfile,
        checks: list[PreflightCheck],
        observations: dict[str, BootstrapObservation],
    ) -> None:
        cluster = self._json(
            (
                "gcloud",
                "container",
                "clusters",
                "describe",
                profile.cluster.name,
                f"--zone={profile.zone}",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        resource_id = f"cluster/{profile.cluster.name}"
        if not isinstance(cluster, dict):
            observations[resource_id] = BootstrapObservation(
                resource_id=resource_id,
                status=ObservationStatus.MISSING,
                summary="cluster not found",
            )
            return
        workload_pool = str(cluster.get("workloadIdentityConfig", {}).get("workloadPool", ""))
        compatible = (
            cluster.get("status") == "RUNNING"
            and cluster.get("location") == profile.zone
            and workload_pool == profile.cluster.workload_pool
            and version_at_least(
                str(cluster.get("currentMasterVersion", "")),
                profile.cluster.minimum_version,
            )
        )
        observations[resource_id] = BootstrapObservation(
            resource_id=resource_id,
            status=(
                ObservationStatus.COMPATIBLE
                if compatible
                else ObservationStatus.INCOMPATIBLE
            ),
            summary=(
                "running with the expected location and Workload Identity"
                if compatible
                else "cluster location, status, or Workload Identity is incompatible"
            ),
            details={
                "version": str(cluster.get("currentMasterVersion", "unknown")),
                "workload_identity": bool(workload_pool),
            },
        )
        context = self._text(("kubectl", "config", "current-context"))
        checks.append(
            PreflightCheck(
                check_id="kubernetes-context",
                passed=context == profile.cluster.expected_context,
                summary=(
                    "Kubernetes context matches the profile."
                    if context == profile.cluster.expected_context
                    else "Kubernetes context does not match the profile."
                ),
            )
        )
        api_resources = self._text(
            ("kubectl", "api-resources", "--api-group=admissionregistration.k8s.io", "-o", "name")
        )
        admission_ready = (
            "validatingadmissionpolicies" in api_resources
            and "validatingadmissionpolicybindings" in api_resources
        )
        checks.append(
            PreflightCheck(
                check_id="validating-admission-policy",
                passed=admission_ready,
                summary=(
                    "ValidatingAdmissionPolicy APIs are available."
                    if admission_ready
                    else "Required admission APIs are unavailable."
                ),
            )
        )
        nodes = self._json(("kubectl", "get", "nodes", "-o", "json"))
        cpu = 0
        memory = 0
        architectures: set[str] = set()
        if isinstance(nodes, dict):
            for node in nodes.get("items", []):
                if not isinstance(node, dict):
                    continue
                status = node.get("status", {})
                node_info = status.get("nodeInfo", {})
                allocatable = status.get("allocatable", {})
                architectures.add(str(node_info.get("architecture", "")))
                cpu += parse_cpu_millis(str(allocatable.get("cpu", "0")))
                memory += parse_memory_bytes(str(allocatable.get("memory", "0")))
        capacity_ready = (
            architectures == {profile.cluster.architecture}
            and cpu >= profile.cluster.minimum_allocatable_cpu_millis
            and memory >= profile.cluster.minimum_allocatable_memory_bytes
        )
        checks.append(
            PreflightCheck(
                check_id="cluster-capacity",
                passed=capacity_ready,
                summary=(
                    "Cluster architecture and allocatable capacity meet the profile."
                    if capacity_ready
                    else "Cluster architecture or allocatable capacity is insufficient."
                ),
            )
        )

    def _inspect_registry(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        registry = self._json(
            (
                "gcloud",
                "artifacts",
                "repositories",
                "describe",
                profile.artifact_registry.repository,
                f"--location={profile.region}",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        resource_id = f"registry/{profile.artifact_registry.repository}"
        if not isinstance(registry, dict):
            status = ObservationStatus.MISSING
            summary = "repository not found"
        else:
            compatible = registry.get("format") == profile.artifact_registry.format
            status = ObservationStatus.COMPATIBLE if compatible else ObservationStatus.INCOMPATIBLE
            summary = "compatible Docker repository" if compatible else "repository format differs"
        observations[resource_id] = BootstrapObservation(
            resource_id=resource_id, status=status, summary=summary
        )

    def _inspect_firestore(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        databases = self._json(
            (
                "gcloud",
                "firestore",
                "databases",
                "list",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        database_items = databases if isinstance(databases, list) else []
        database = next(
            (
                item
                for item in database_items
                if isinstance(item, dict)
                and str(item.get("name", "")).endswith(
                    f"/databases/{profile.firestore.database_id}"
                )
            ),
            None,
        )
        resource_id = f"firestore/{profile.firestore.database_id}"
        if not isinstance(database, dict):
            status = ObservationStatus.MISSING
            summary = "database not found"
        else:
            compatible = (
                database.get("locationId") == profile.firestore.location
                and database.get("type") == profile.firestore.mode
                and database.get("deleteProtectionState") == "DELETE_PROTECTION_ENABLED"
            )
            status = ObservationStatus.COMPATIBLE if compatible else ObservationStatus.INCOMPATIBLE
            summary = (
                "Native database has the approved immutable location"
                if compatible
                else "database mode, location, or delete protection differs"
            )
        observations[resource_id] = BootstrapObservation(
            resource_id=resource_id, status=status, summary=summary
        )

    def _inspect_pubsub(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        topics = self._json(
            (
                "gcloud",
                "pubsub",
                "topics",
                "list",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        topic_items = topics if isinstance(topics, list) else []
        topic_names = {
            str(item.get("name", "")).rsplit("/", maxsplit=1)[-1]
            for item in topic_items
            if isinstance(item, dict)
        }
        subscriptions = self._json(
            (
                "gcloud",
                "pubsub",
                "subscriptions",
                "list",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        subscription_items = subscriptions if isinstance(subscriptions, list) else []
        subscription_map = {
            str(item.get("name", "")).rsplit("/", maxsplit=1)[-1]: item
            for item in subscription_items
            if isinstance(item, dict)
        }
        for stream in (profile.pubsub.alerts, profile.pubsub.interventions):
            for topic in (stream.topic, stream.dead_letter_topic):
                observations[f"pubsub-topic/{topic}"] = BootstrapObservation(
                    resource_id=f"pubsub-topic/{topic}",
                    status=(
                        ObservationStatus.COMPATIBLE
                        if topic in topic_names
                        else ObservationStatus.MISSING
                    ),
                    summary="topic exists" if topic in topic_names else "topic not found",
                )
            for name in (stream.subscription, stream.dead_letter_subscription):
                subscription = subscription_map.get(name)
                is_reader = name == stream.dead_letter_subscription
                expected_topic = stream.dead_letter_topic if is_reader else stream.topic
                compatible = False
                if isinstance(subscription, dict):
                    actual_topic = str(subscription.get("topic", "")).rsplit(
                        "/", maxsplit=1
                    )[-1]
                    retention = parse_duration_seconds(
                        str(subscription.get("messageRetentionDuration", "0"))
                    )
                    compatible = (
                        int(subscription.get("ackDeadlineSeconds", 0))
                        == (10 if is_reader else stream.ack_deadline_seconds)
                        and actual_topic == expected_topic
                        and retention == parse_duration_seconds(stream.retention)
                        and not subscription.get("expirationPolicy")
                    )
                if isinstance(subscription, dict) and not is_reader:
                    dead_letter = str(
                        subscription.get("deadLetterPolicy", {}).get("deadLetterTopic", "")
                    ).rsplit("/", maxsplit=1)[-1]
                    retry = subscription.get("retryPolicy", {})
                    compatible = compatible and (
                        dead_letter == stream.dead_letter_topic
                        and int(
                            subscription.get("deadLetterPolicy", {}).get(
                                "maxDeliveryAttempts", 0
                            )
                        )
                        == stream.maximum_delivery_attempts
                        and parse_duration_seconds(str(retry.get("minimumBackoff", "0")))
                        == parse_duration_seconds(stream.retry_minimum)
                        and parse_duration_seconds(str(retry.get("maximumBackoff", "0")))
                        == parse_duration_seconds(stream.retry_maximum)
                    )
                observations[f"pubsub-subscription/{name}"] = BootstrapObservation(
                    resource_id=f"pubsub-subscription/{name}",
                    status=(
                        ObservationStatus.COMPATIBLE
                        if compatible
                        else (
                            ObservationStatus.INCOMPATIBLE
                            if subscription is not None
                            else ObservationStatus.MISSING
                        )
                    ),
                    summary=(
                        "subscription settings are compatible"
                        if compatible
                        else "subscription missing or settings differ"
                    ),
                )

    def _inspect_identities(
        self,
        profile: DeploymentProfile,
        checks: list[PreflightCheck],
        observations: dict[str, BootstrapObservation],
    ) -> dict[str, set[str]]:
        accounts = self._json(
            (
                "gcloud",
                "iam",
                "service-accounts",
                "list",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        account_items = accounts if isinstance(accounts, list) else []
        account_map = {
            str(item.get("email")): item
            for item in account_items
            if isinstance(item, dict)
        }
        policy = self._json(
            (
                "gcloud",
                "projects",
                "get-iam-policy",
                profile.project_id,
                "--format=json",
            )
        )
        role_map: dict[str, set[str]] = {}
        if isinstance(policy, dict):
            for binding in policy.get("bindings", []):
                if not isinstance(binding, dict):
                    continue
                role = str(binding.get("role", ""))
                for member in binding.get("members", []):
                    role_map.setdefault(str(member), set()).add(role)
        for identity in (
            profile.identities.investigator,
            profile.identities.executor,
            profile.identities.scenario_controller,
            profile.identities.github_deployer,
        ):
            account = account_map.get(identity.email)
            compatible = isinstance(account, dict) and account.get("disabled") is not True
            observations[f"gsa/{identity.name}"] = BootstrapObservation(
                resource_id=f"gsa/{identity.name}",
                status=(
                    ObservationStatus.COMPATIBLE
                    if compatible
                    else ObservationStatus.MISSING
                ),
                summary="active service account" if compatible else "service account missing",
            )
            roles = role_map.get(f"serviceAccount:{identity.email}", set())
            for role in identity.project_roles:
                observations[f"iam-role/{identity.name}/{role}"] = BootstrapObservation(
                    resource_id=f"iam-role/{identity.name}/{role}",
                    status=(
                        ObservationStatus.COMPATIBLE
                        if role in roles
                        else ObservationStatus.MISSING
                    ),
                    summary="role binding exists" if role in roles else "role binding missing",
                )
        checks.append(
            PreflightCheck(
                check_id="distinct-google-service-accounts",
                passed=all(
                    isinstance(account_map.get(identity.email), dict)
                    for identity in (
                        profile.identities.investigator,
                        profile.identities.executor,
                        profile.identities.scenario_controller,
                    )
                ),
                summary="Distinct incident-response Google service accounts were inspected.",
            )
        )
        return role_map

    def _inspect_kubernetes_identities(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        for namespace in (
            profile.kubernetes.system_namespace,
            profile.kubernetes.demo_control_namespace,
        ):
            result = self._json(("kubectl", "get", "namespace", namespace, "-o", "json"))
            observations[f"namespace/{namespace}"] = BootstrapObservation(
                resource_id=f"namespace/{namespace}",
                status=(
                    ObservationStatus.COMPATIBLE
                    if isinstance(result, dict)
                    else ObservationStatus.MISSING
                ),
                summary="namespace exists" if isinstance(result, dict) else "namespace not found",
            )
        for account in (
            profile.kubernetes.service_accounts.investigator,
            profile.kubernetes.service_accounts.executor,
            profile.kubernetes.service_accounts.scenario_controller,
        ):
            result = self._json(
                (
                    "kubectl",
                    "get",
                    "serviceaccount",
                    account.name,
                    f"--namespace={account.namespace}",
                    "-o",
                    "json",
                )
            )
            annotation = (
                result.get("metadata", {})
                .get("annotations", {})
                .get("iam.gke.io/gcp-service-account")
                if isinstance(result, dict)
                else None
            )
            status = (
                ObservationStatus.COMPATIBLE
                if annotation == account.google_service_account
                else (
                    ObservationStatus.INCOMPATIBLE
                    if isinstance(result, dict)
                    else ObservationStatus.MISSING
                )
            )
            observations[f"ksa/{account.namespace}/{account.name}"] = BootstrapObservation(
                resource_id=f"ksa/{account.namespace}/{account.name}",
                status=status,
                summary=(
                    "KSA annotation matches its GSA"
                    if status == ObservationStatus.COMPATIBLE
                    else "KSA missing or Workload Identity annotation differs"
                ),
            )

    def _inspect_resource_iam(
        self, profile: DeploymentProfile, checks: list[PreflightCheck]
    ) -> None:
        alert_policy = self._json(
            (
                "gcloud",
                "pubsub",
                "subscriptions",
                "get-iam-policy",
                profile.pubsub.alerts.subscription,
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        intervention_subscription_policy = self._json(
            (
                "gcloud",
                "pubsub",
                "subscriptions",
                "get-iam-policy",
                profile.pubsub.interventions.subscription,
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        intervention_topic_policy = self._json(
            (
                "gcloud",
                "pubsub",
                "topics",
                "get-iam-policy",
                profile.pubsub.interventions.topic,
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        registry_policy = self._json(
            (
                "gcloud",
                "artifacts",
                "repositories",
                "get-iam-policy",
                profile.artifact_registry.repository,
                f"--location={profile.region}",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        investigator = f"serviceAccount:{profile.identities.investigator.email}"
        executor = f"serviceAccount:{profile.identities.executor.email}"
        scenario = f"serviceAccount:{profile.identities.scenario_controller.email}"
        deployer = f"serviceAccount:{profile.identities.github_deployer.email}"
        investigator_correct = (
            self._policy_has_member(alert_policy, "roles/pubsub.subscriber", investigator)
            and self._policy_has_member(
                intervention_topic_policy, "roles/pubsub.publisher", investigator
            )
            and not self._policy_has_any_member(
                intervention_subscription_policy, investigator
            )
        )
        executor_correct = (
            self._policy_has_member(
                intervention_subscription_policy, "roles/pubsub.subscriber", executor
            )
            and not self._policy_has_any_member(alert_policy, executor)
            and not self._policy_has_any_member(intervention_topic_policy, executor)
        )
        scenario_isolated = not any(
            self._policy_has_any_member(policy, scenario)
            for policy in (
                alert_policy,
                intervention_subscription_policy,
                intervention_topic_policy,
            )
        )
        deployer_writer = self._policy_has_member(
            registry_policy, "roles/artifactregistry.writer", deployer
        )
        investigator_patch_check = self._runner.capture(
            (
                "kubectl",
                "auth",
                "can-i",
                "patch",
                "deployments",
                f"--namespace={profile.kubernetes.application_namespace}",
                (
                    "--as=system:serviceaccount:"
                    f"{profile.kubernetes.system_namespace}:"
                    f"{profile.kubernetes.service_accounts.investigator.name}"
                ),
            )
        )
        checks.extend(
            (
                PreflightCheck(
                    check_id="pubsub-investigator-separation",
                    passed=investigator_correct,
                    summary=(
                        "Investigator consumes alerts and publishes, but cannot consume, "
                        "Intervention requests."
                    ),
                ),
                PreflightCheck(
                    check_id="pubsub-executor-separation",
                    passed=executor_correct,
                    summary="Executor consumes only Intervention requests.",
                ),
                PreflightCheck(
                    check_id="scenario-controller-cloud-isolation",
                    passed=scenario_isolated,
                    summary="Scenario Controller has no alert or Intervention queue authority.",
                ),
                PreflightCheck(
                    check_id="artifact-registry-deployer",
                    passed=deployer_writer,
                    summary="GitHub deployer has repository-scoped Artifact Registry writer.",
                ),
                PreflightCheck(
                    check_id="investigator-kubernetes-write-denied",
                    passed=kubectl_authorization_is_denied(investigator_patch_check),
                    summary="Investigator cannot patch application Deployments.",
                ),
            )
        )

    def _inspect_workload_identity(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        for identity_name in ("investigator", "executor", "scenario_controller"):
            identity = getattr(profile.identities, identity_name)
            account = getattr(profile.kubernetes.service_accounts, identity_name)
            policy = self._json(
                (
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "get-iam-policy",
                    identity.email,
                    f"--project={profile.project_id}",
                    "--format=json",
                )
            )
            expected = (
                f"serviceAccount:{profile.project_id}.svc.id.goog"
                f"[{account.namespace}/{account.name}]"
            )
            bound = self._policy_has_member(policy, "roles/iam.workloadIdentityUser", expected)
            resource_id = f"workload-identity/{identity_name.replace('_', '-')}"
            observations[resource_id] = BootstrapObservation(
                resource_id=resource_id,
                status=(
                    ObservationStatus.COMPATIBLE if bound else ObservationStatus.MISSING
                ),
                summary="Workload Identity binding exists" if bound else "binding missing",
            )

    def _inspect_github(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        pool = self._json(
            (
                "gcloud",
                "iam",
                "workload-identity-pools",
                "describe",
                profile.github.pool,
                "--location=global",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        observations[f"github-pool/{profile.github.pool}"] = BootstrapObservation(
            resource_id=f"github-pool/{profile.github.pool}",
            status=(
                ObservationStatus.COMPATIBLE
                if isinstance(pool, dict) and pool.get("state") == "ACTIVE"
                else ObservationStatus.MISSING
            ),
            summary="active pool" if isinstance(pool, dict) else "pool not found",
        )
        provider = self._json(
            (
                "gcloud",
                "iam",
                "workload-identity-pools",
                "providers",
                "describe",
                profile.github.provider,
                f"--workload-identity-pool={profile.github.pool}",
                "--location=global",
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        expected_condition = f"assertion.repository=='{profile.github.repository}'"
        provider_compatible = (
            isinstance(provider, dict)
            and provider.get("state") == "ACTIVE"
            and provider.get("attributeCondition") == expected_condition
        )
        observations[f"github-provider/{profile.github.provider}"] = BootstrapObservation(
            resource_id=f"github-provider/{profile.github.provider}",
            status=(
                ObservationStatus.COMPATIBLE
                if provider_compatible
                else (
                    ObservationStatus.INCOMPATIBLE
                    if isinstance(provider, dict)
                    else ObservationStatus.MISSING
                )
            ),
            summary=(
                "active repository-restricted provider"
                if provider_compatible
                else "provider missing or repository condition differs"
            ),
        )
        deployer_policy = self._json(
            (
                "gcloud",
                "iam",
                "service-accounts",
                "get-iam-policy",
                profile.identities.github_deployer.email,
                f"--project={profile.project_id}",
                "--format=json",
            )
        )
        expected_fragment = (
            f"/workloadIdentityPools/{profile.github.pool}/attribute.repository/"
            f"{profile.github.repository}"
        )
        binding = self._policy_has_member_fragment(
            deployer_policy, "roles/iam.workloadIdentityUser", expected_fragment
        )
        observations["github-binding/deployer"] = BootstrapObservation(
            resource_id="github-binding/deployer",
            status=ObservationStatus.COMPATIBLE if binding else ObservationStatus.MISSING,
            summary="repository impersonation binding exists" if binding else "binding missing",
        )

    def _inspect_images(
        self, profile: DeploymentProfile, observations: dict[str, BootstrapObservation]
    ) -> None:
        for name, image in profile.images.items():
            if not image.available:
                continue
            result = self._json(
                (
                    "gcloud",
                    "artifacts",
                    "docker",
                    "images",
                    "describe",
                    f"{image.repository}:{image.tag}",
                    f"--project={profile.project_id}",
                    "--format=json",
                )
            )
            digest = None
            if isinstance(result, dict):
                digest = str(result.get("image_summary", {}).get("digest", "")) or str(
                    result.get("digest", "")
                )
            compatible = digest == image.digest
            observations[f"image/{name}"] = BootstrapObservation(
                resource_id=f"image/{name}",
                status=(
                    ObservationStatus.COMPATIBLE
                    if compatible
                    else (
                        ObservationStatus.INCOMPATIBLE
                        if isinstance(result, dict)
                        else ObservationStatus.MISSING
                    )
                ),
                summary=(
                    "tag resolves to the recorded immutable digest"
                    if compatible
                    else "image tag missing or digest differs"
                ),
            )

    def _inspect_negative_iam(
        self,
        profile: DeploymentProfile,
        checks: list[PreflightCheck],
        role_map: Mapping[str, set[str]],
    ) -> None:
        investigator_roles = role_map.get(
            f"serviceAccount:{profile.identities.investigator.email}", set()
        )
        executor_roles = role_map.get(
            f"serviceAccount:{profile.identities.executor.email}", set()
        )
        scenario_roles = role_map.get(
            f"serviceAccount:{profile.identities.scenario_controller.email}", set()
        )
        forbidden_investigator = {
            "roles/container.admin",
            "roles/container.developer",
            "roles/owner",
            "roles/editor",
        }
        forbidden_executor = {
            "roles/aiplatform.user",
            "roles/logging.viewer",
            "roles/monitoring.viewer",
            "roles/owner",
            "roles/editor",
        }
        checks.extend(
            (
                PreflightCheck(
                    check_id="negative-iam-investigator",
                    passed=not forbidden_investigator.intersection(investigator_roles),
                    summary="Investigator has no broad project or GKE write role.",
                ),
                PreflightCheck(
                    check_id="negative-iam-executor",
                    passed=not forbidden_executor.intersection(executor_roles),
                    summary="Executor has no model or broad observability role.",
                ),
                PreflightCheck(
                    check_id="negative-iam-scenario-controller",
                    passed=not scenario_roles,
                    summary="Scenario Controller has no project-level cloud role.",
                ),
            )
        )

    def _json(self, command: tuple[str, ...]) -> Any | None:
        result = self._runner.capture(command)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def _text(self, command: tuple[str, ...]) -> str:
        result = self._runner.capture(command)
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _policy_has_member(policy: Any, role: str, member: str) -> bool:
        return any(
            isinstance(binding, dict)
            and binding.get("role") == role
            and member in binding.get("members", [])
            for binding in policy.get("bindings", [])
        ) if isinstance(policy, dict) else False

    @staticmethod
    def _policy_has_any_member(policy: Any, member: str) -> bool:
        return any(
            isinstance(binding, dict) and member in binding.get("members", [])
            for binding in policy.get("bindings", [])
        ) if isinstance(policy, dict) else False

    @classmethod
    def _policy_has_member_fragment(cls, policy: Any, role: str, fragment: str) -> bool:
        return any(
            isinstance(binding, dict)
            and binding.get("role") == role
            and any(fragment in str(member) for member in binding.get("members", []))
            for binding in policy.get("bindings", [])
        ) if isinstance(policy, dict) else False
