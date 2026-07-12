from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.bootstrap.cli import build_inventory, validate_rendered_manifest
from app.bootstrap.inspector import (
    CommandResult,
    kubectl_authorization_is_denied,
    parse_cpu_millis,
    parse_duration_seconds,
    parse_memory_bytes,
    sanitize_command_output,
    version_at_least,
)
from app.bootstrap.models import (
    BootstrapObservation,
    DeploymentProfile,
    EnvironmentInventory,
    ObservationStatus,
    PreflightCheck,
    PreflightReport,
    load_deployment_profile,
)
from app.bootstrap.planner import (
    BootstrapApprovalError,
    BootstrapPlanner,
    RecordingCommandRunner,
    apply_bootstrap_plan,
    smoke_resource_can_be_deleted,
)
from app.bootstrap.smoke import BootstrapSmokeRunner

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "deploy/profiles/findydevops-dev.yaml"


def _profile() -> DeploymentProfile:
    return load_deployment_profile(PROFILE_PATH)


def _compatible_observations(profile: DeploymentProfile) -> tuple[BootstrapObservation, ...]:
    return tuple(
        BootstrapObservation(
            resource_id=requirement.resource_id,
            status=ObservationStatus.COMPATIBLE,
            summary="verified",
        )
        for requirement in BootstrapPlanner().requirements(profile)
    )


def test_checked_in_profile_is_explicit_and_credential_free() -> None:
    profile = _profile()

    assert profile.project_id == "findydevops"
    assert profile.cluster.name == "kubecouncil-dev"
    assert profile.cluster.mode == "reuse-only"
    assert profile.firestore.database_id == "(default)"
    assert profile.firestore.location == "asia-northeast1"
    assert profile.pubsub.alerts.topic == "kc-alert-signals"
    assert profile.pubsub.interventions.subscription == "kc-executor-interventions"
    assert profile.kubernetes.service_accounts.scenario_controller.namespace == (
        "kubecouncil-demo-control"
    )
    assert profile.images["backend"].digest.startswith("sha256:")
    assert profile.images["frontend"].platform == "linux/amd64"

    raw = PROFILE_PATH.read_text()
    assert "replace-me" not in raw
    assert "PROJECT_ID" not in raw
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in raw
    assert "PRIVATE KEY" not in raw


def test_profile_requires_an_explicit_firestore_location() -> None:
    raw = yaml.safe_load(PROFILE_PATH.read_text())
    raw["firestore"]["location"] = ""

    with pytest.raises(ValidationError, match="location"):
        DeploymentProfile.model_validate(raw)


def test_profile_rejects_credential_fields_and_mutable_image_references() -> None:
    raw = yaml.safe_load(PROFILE_PATH.read_text())
    raw["github"]["token"] = "not-a-real-token"

    with pytest.raises(ValueError, match="credential-like field"):
        DeploymentProfile.from_untrusted(raw)

    raw = yaml.safe_load(PROFILE_PATH.read_text())
    raw["images"]["backend"]["digest"] = "latest"
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        DeploymentProfile.model_validate(raw)


def test_complete_environment_produces_an_empty_idempotent_plan() -> None:
    profile = _profile()

    plan = BootstrapPlanner().plan(profile, _compatible_observations(profile))

    assert plan.actions == ()
    assert plan.incompatible == ()
    assert set(plan.reused) == {
        requirement.resource_id for requirement in BootstrapPlanner().requirements(profile)
    }
    assert len(plan.plan_hash) == 64


def test_missing_demo_identity_is_planned_without_recreating_the_cluster() -> None:
    profile = _profile()
    observations = list(_compatible_observations(profile))
    observations = [
        item
        for item in observations
        if item.resource_id
        not in {
            "namespace/kubecouncil-demo-control",
            "ksa/kubecouncil-demo-control/scenario-controller",
            "workload-identity/scenario-controller",
        }
    ]
    observations.extend(
        BootstrapObservation(
            resource_id=resource_id,
            status=ObservationStatus.MISSING,
            summary="not found",
        )
        for resource_id in (
            "namespace/kubecouncil-demo-control",
            "ksa/kubecouncil-demo-control/scenario-controller",
            "workload-identity/scenario-controller",
        )
    )

    plan = BootstrapPlanner().plan(profile, tuple(observations))

    assert [action.resource_id for action in plan.actions] == [
        "namespace/kubecouncil-demo-control",
        "ksa/kubecouncil-demo-control/scenario-controller",
        "workload-identity/scenario-controller",
    ]
    assert all(action.approval == "identity" for action in plan.actions)
    assert all(
        "container clusters create" not in " ".join(action.command)
        for action in plan.actions
    )


def test_reuse_only_cluster_fails_closed_when_missing_or_incompatible() -> None:
    profile = _profile()
    observations = [
        item
        for item in _compatible_observations(profile)
        if item.resource_id != "cluster/kubecouncil-dev"
    ]
    observations.append(
        BootstrapObservation(
            resource_id="cluster/kubecouncil-dev",
            status=ObservationStatus.MISSING,
            summary="not found",
        )
    )

    plan = BootstrapPlanner().plan(profile, tuple(observations))

    assert plan.actions == ()
    assert plan.incompatible == ("cluster/kubecouncil-dev: reuse-only cluster is missing",)


def test_apply_requires_the_exact_plan_hash_and_each_approval() -> None:
    profile = _profile()
    observations = [
        item
        for item in _compatible_observations(profile)
        if item.resource_id != "namespace/kubecouncil-demo-control"
    ]
    observations.append(
        BootstrapObservation(
            resource_id="namespace/kubecouncil-demo-control",
            status=ObservationStatus.MISSING,
            summary="not found",
        )
    )
    plan = BootstrapPlanner().plan(profile, tuple(observations))
    runner = RecordingCommandRunner()

    with pytest.raises(BootstrapApprovalError, match="plan hash"):
        apply_bootstrap_plan(
            plan,
            approved_plan_hash="wrong",
            approvals=frozenset({"identity"}),
            runner=runner,
        )
    with pytest.raises(BootstrapApprovalError, match="identity"):
        apply_bootstrap_plan(
            plan,
            approved_plan_hash=plan.plan_hash,
            approvals=frozenset(),
            runner=runner,
        )

    report = apply_bootstrap_plan(
        plan,
        approved_plan_hash=plan.plan_hash,
        approvals=frozenset({"identity"}),
        runner=runner,
    )

    assert report.applied == ("namespace/kubecouncil-demo-control",)
    assert runner.commands == [plan.actions[0].command]


def test_partial_failure_is_recovered_by_reinspection_and_replanning() -> None:
    profile = _profile()
    missing_ids = (
        "namespace/kubecouncil-demo-control",
        "ksa/kubecouncil-demo-control/scenario-controller",
    )
    initial = [
        item
        for item in _compatible_observations(profile)
        if item.resource_id not in missing_ids
    ]
    initial.extend(
        BootstrapObservation(
            resource_id=resource_id,
            status=ObservationStatus.MISSING,
            summary="not found",
        )
        for resource_id in missing_ids
    )
    first_plan = BootstrapPlanner().plan(profile, tuple(initial))

    class _FailAfterNamespace(RecordingCommandRunner):
        def run(self, command: tuple[str, ...]) -> None:
            super().run(command)
            if len(self.commands) == 2:
                raise RuntimeError("simulated partial failure")

    with pytest.raises(RuntimeError, match="partial failure"):
        apply_bootstrap_plan(
            first_plan,
            approved_plan_hash=first_plan.plan_hash,
            approvals=frozenset({"identity"}),
            runner=_FailAfterNamespace(),
        )

    recovered = [
        item
        for item in _compatible_observations(profile)
        if item.resource_id != missing_ids[1]
    ]
    recovered.append(
        BootstrapObservation(
            resource_id=missing_ids[1],
            status=ObservationStatus.MISSING,
            summary="not found",
        )
    )
    second_plan = BootstrapPlanner().plan(profile, tuple(recovered))

    assert [action.resource_id for action in second_plan.actions] == [missing_ids[1]]
    assert second_plan.plan_hash != first_plan.plan_hash


def test_role_matrix_keeps_privileges_separated() -> None:
    profile = _profile()

    assert set(profile.identities.investigator.project_roles) == {
        "roles/aiplatform.user",
        "roles/datastore.user",
        "roles/logging.viewer",
        "roles/monitoring.viewer",
        "roles/serviceusage.serviceUsageConsumer",
    }
    assert set(profile.identities.executor.project_roles) == {"roles/datastore.user"}
    assert profile.identities.scenario_controller.project_roles == ()
    assert "roles/aiplatform.user" not in profile.identities.executor.project_roles
    assert "roles/logging.viewer" not in profile.identities.executor.project_roles


def test_smoke_cleanup_guard_requires_all_ownership_labels() -> None:
    assert smoke_resource_can_be_deleted(
        {
            "kubecouncil.io/bootstrap-smoke": "true",
            "kubecouncil.io/environment": "findydevops-dev",
        },
        environment="findydevops-dev",
    )
    assert not smoke_resource_can_be_deleted(
        {"kubecouncil.io/bootstrap-smoke": "true"},
        environment="findydevops-dev",
    )


def test_preflight_helpers_parse_gke_quantities_and_redact_sensitive_output() -> None:
    assert parse_cpu_millis("3920m") == 3920
    assert parse_cpu_millis("4") == 4000
    assert parse_memory_bytes("1024Mi") == 1024 * 1024**2
    assert parse_memory_bytes("13591700Ki") == 13591700 * 1024
    assert parse_duration_seconds("7d") == 604800
    assert parse_duration_seconds("600s") == 600
    assert version_at_least("1.35.5-gke.1241004", "1.30")
    assert not version_at_least("1.29.9-gke.1", "1.30")
    assert "ghp_" not in sanitize_command_output("failure for ghp_examplecredential")
    assert ".config/gcloud" not in sanitize_command_output(
        "failed to read /Users/person/.config/gcloud/credentials.db"
    )


def test_kubectl_authorization_denial_accepts_expected_nonzero_exit() -> None:
    denied = CommandResult(("kubectl",), 1, "no\n", "")
    command_failure = CommandResult(("kubectl",), 1, "", "connection refused")

    assert kubectl_authorization_is_denied(denied)
    assert not kubectl_authorization_is_denied(command_failure)


def test_inventory_is_sanitized_and_records_completed_iap_gate() -> None:
    profile = _profile()
    report = PreflightReport(
        profile_id=profile.profile_id,
        generated_at="2026-07-12T00:00:00Z",
        checks=(PreflightCheck(check_id="cluster", passed=True, summary="ready"),),
        observations=_compatible_observations(profile),
    )

    inventory = build_inventory(profile, report)

    assert inventory.image_references["backend"].endswith(profile.images["backend"].digest)
    assert inventory.required_manual_inputs == ()
    assert all("token" not in command.lower() for command in inventory.commands)


def test_checked_in_inventory_matches_the_profile_contract() -> None:
    inventory = EnvironmentInventory.model_validate(
        yaml.safe_load((ROOT / "deploy/inventory/findydevops-dev.yaml").read_text())
    )

    assert inventory.project_id == _profile().project_id
    assert inventory.firestore_location == _profile().firestore.location
    assert set(inventory.image_references) == {"backend", "frontend"}


def test_rendered_manifest_scanner_rejects_placeholders_and_legacy_authority() -> None:
    validate_rendered_manifest("apiVersion: v1\nkind: ConfigMap\n")
    with pytest.raises(ValueError, match="PROJECT_ID"):
        validate_rendered_manifest("value: PROJECT_ID")
    with pytest.raises(ValueError, match="kubecouncil-rehearsal-manager"):
        validate_rendered_manifest("name: kubecouncil-rehearsal-manager")


def test_incident_response_bootstrap_manifests_render_without_legacy_authority() -> None:
    completed = subprocess.run(  # noqa: S603
        (
            "kubectl",
            "kustomize",
            str(ROOT / "manifests/incident-response/bootstrap"),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = completed.stdout
    validate_rendered_manifest(rendered)

    assert "name: investigator" in rendered
    assert "name: executor" in rendered
    assert "name: scenario-controller" in rendered
    assert "kind: ClusterRole" not in rendered
    assert "kind: Secret" not in rendered
    assert "kubecouncil.io/enrolled" not in rendered


def test_bootstrap_artifacts_contain_no_secrets_or_unresolved_values() -> None:
    paths = (
        *tuple((ROOT / "deploy").rglob("*.yaml")),
        *tuple((ROOT / "manifests/incident-response").rglob("*.yaml")),
    )
    forbidden = (
        "us-docker.pkg.dev/PROJECT_ID",
        "replace-me",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "BEGIN PRIVATE KEY",
        "GITHUB_TOKEN=",
        "ghp_",
        "github_pat_",
    )

    for path in paths:
        value = path.read_text()
        assert not [term for term in forbidden if term in value], path

    workflow = (ROOT / ".github/workflows/verify-deploy.yml").read_text()
    assert "workload_identity_provider" in workflow
    assert "service_account:" in workflow
    assert "credentials_json" not in workflow
    assert "service_account_key" not in workflow


class _FakeSmokeProbe:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise PermissionError("provider details must not enter the report")

    def vertex(self) -> None:
        self._record("vertex")

    def firestore_round_trip(self, marker: str) -> None:
        assert marker == "smoke-test"
        self._record("firestore")

    def pubsub_round_trip(self, marker: str) -> None:
        assert marker == "smoke-test"
        self._record("pubsub")

    def logging_read(self) -> None:
        self._record("logging-read")

    def monitoring_read(self) -> None:
        self._record("monitoring-read")


def test_smoke_runner_reports_capabilities_without_provider_payloads() -> None:
    probe = _FakeSmokeProbe(fail="pubsub")

    report = BootstrapSmokeRunner(probe).run(smoke_id="smoke-test")

    assert probe.calls == [
        "vertex",
        "firestore",
        "pubsub",
        "logging-read",
        "monitoring-read",
    ]
    assert not report.passed
    failed = next(check for check in report.checks if not check.passed)
    assert failed.name == "pubsub"
    assert failed.error_type == "PermissionError"
    assert "provider details" not in report.model_dump_json()
    assert not smoke_resource_can_be_deleted(
        {
            "kubecouncil.io/bootstrap-smoke": "false",
            "kubecouncil.io/environment": "findydevops-dev",
        },
        environment="findydevops-dev",
    )
