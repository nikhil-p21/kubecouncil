"""Strict non-secret configuration and report models for the environment bootstrap."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from app.domain.models import KubeCouncilModel

_CREDENTIAL_FIELD_PARTS = (
    "access_token",
    "client_secret",
    "credential",
    "oauth_secret",
    "password",
    "private_key",
    "secret_key",
    "service_account_key",
    "token",
)


def _reject_credential_fields(value: object, *, path: str = "profile") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _CREDENTIAL_FIELD_PARTS):
                raise ValueError(f"credential-like field is forbidden: {path}.{key}")
            _reject_credential_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credential_fields(child, path=f"{path}[{index}]")


class FirestoreConfig(KubeCouncilModel):
    database_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    mode: Literal["FIRESTORE_NATIVE"] = "FIRESTORE_NATIVE"
    delete_protection: Literal[True] = True


class ClusterConfig(KubeCouncilModel):
    name: str = Field(min_length=1)
    mode: Literal["reuse-only", "create-if-missing"]
    expected_context: str = Field(min_length=1)
    minimum_version: str = Field(min_length=1)
    architecture: Literal["amd64"] = "amd64"
    minimum_allocatable_cpu_millis: int = Field(ge=1000)
    minimum_allocatable_memory_bytes: int = Field(ge=1_073_741_824)
    workload_pool: str = Field(min_length=1)


class ArtifactRegistryConfig(KubeCouncilModel):
    repository: str = Field(min_length=1)
    format: Literal["DOCKER"] = "DOCKER"
    prefix: str = Field(min_length=1)


class SubscriptionConfig(KubeCouncilModel):
    topic: str = Field(min_length=1)
    subscription: str = Field(min_length=1)
    ack_deadline_seconds: int = Field(ge=10, le=600)
    retention: str = Field(pattern=r"^[1-9][0-9]*d$")
    retry_minimum: str = Field(pattern=r"^[1-9][0-9]*s$")
    retry_maximum: str = Field(pattern=r"^[1-9][0-9]*s$")
    dead_letter_topic: str = Field(min_length=1)
    dead_letter_subscription: str = Field(min_length=1)
    maximum_delivery_attempts: int = Field(ge=5, le=100)


class PubSubConfig(KubeCouncilModel):
    alerts: SubscriptionConfig
    interventions: SubscriptionConfig


class KubernetesServiceAccount(KubeCouncilModel):
    name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    google_service_account: str = Field(
        pattern=r"^[a-z][a-z0-9-]+@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com$"
    )


class KubernetesServiceAccounts(KubeCouncilModel):
    investigator: KubernetesServiceAccount
    executor: KubernetesServiceAccount
    scenario_controller: KubernetesServiceAccount


class KubernetesConfig(KubeCouncilModel):
    system_namespace: str = Field(min_length=1)
    application_namespace: str = Field(min_length=1)
    demo_control_namespace: str = Field(min_length=1)
    service_accounts: KubernetesServiceAccounts

    @model_validator(mode="after")
    def identities_use_expected_namespaces(self) -> KubernetesConfig:
        if self.service_accounts.investigator.namespace != self.system_namespace:
            raise ValueError("Investigator KSA must use the system namespace")
        if self.service_accounts.executor.namespace != self.system_namespace:
            raise ValueError("Executor KSA must use the system namespace")
        if self.service_accounts.scenario_controller.namespace != self.demo_control_namespace:
            raise ValueError("Scenario Controller KSA must use the demo-control namespace")
        return self


class CloudIdentity(KubeCouncilModel):
    name: str = Field(min_length=1)
    email: str = Field(
        pattern=r"^[a-z][a-z0-9-]+@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com$"
    )
    project_roles: tuple[str, ...] = ()


class IdentityConfig(KubeCouncilModel):
    investigator: CloudIdentity
    executor: CloudIdentity
    scenario_controller: CloudIdentity
    github_deployer: CloudIdentity

    @model_validator(mode="after")
    def identities_are_distinct_and_separated(self) -> IdentityConfig:
        emails = {
            self.investigator.email,
            self.executor.email,
            self.scenario_controller.email,
            self.github_deployer.email,
        }
        if len(emails) != 4:
            raise ValueError("bootstrap Google service accounts must be distinct")
        if self.scenario_controller.project_roles:
            raise ValueError("Scenario Controller must not receive project-level cloud roles")
        forbidden_executor = {
            "roles/aiplatform.user",
            "roles/logging.viewer",
            "roles/monitoring.viewer",
        }
        if forbidden_executor.intersection(self.executor.project_roles):
            raise ValueError("Executor must not receive model or broad observability roles")
        return self


class ImageConfig(KubeCouncilModel):
    repository: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tag: str = Field(min_length=1, pattern=r"^[0-9a-f]{12}(?:-live[0-9]+)?$")
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/amd64"] = "linux/amd64"
    build_context: str | None = None
    available: bool = True

    @model_validator(mode="after")
    def available_images_are_immutable(self) -> ImageConfig:
        if self.available and (self.digest is None or self.build_context is None):
            raise ValueError("available images require a digest and build context")
        if not self.available and self.digest is not None:
            raise ValueError("unavailable images cannot declare a published digest")
        return self

    @property
    def immutable_reference(self) -> str | None:
        return f"{self.repository}@{self.digest}" if self.digest else None


class GitHubFederationConfig(KubeCouncilModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pool: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_resource: str = Field(min_length=1)


class IAPConfig(KubeCouncilModel):
    principal_source: Literal["operator-input"] = "operator-input"
    viewer_principals: tuple[str, ...] = ()
    responder_principals: tuple[str, ...] = ()

    @model_validator(mode="after")
    def configured_principals_are_explicit(self) -> IAPConfig:
        principals = (*self.viewer_principals, *self.responder_principals)
        if any(":" not in principal for principal in principals):
            raise ValueError("IAP principals must include their principal type")
        return self


class DeploymentProfile(KubeCouncilModel):
    profile_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    project_number: str = Field(pattern=r"^[0-9]+$")
    region: str = Field(min_length=1)
    zone: str = Field(min_length=1)
    resource_prefix: str = Field(min_length=1)
    cluster: ClusterConfig
    artifact_registry: ArtifactRegistryConfig
    firestore: FirestoreConfig
    pubsub: PubSubConfig
    kubernetes: KubernetesConfig
    identities: IdentityConfig
    images: dict[str, ImageConfig]
    github: GitHubFederationConfig
    iap: IAPConfig
    vertex_model: str = Field(min_length=1)
    required_apis: tuple[str, ...] = Field(min_length=1)

    @classmethod
    def from_untrusted(cls, value: dict[str, Any]) -> DeploymentProfile:
        _reject_credential_fields(value)
        return cls.model_validate(value)

    @model_validator(mode="after")
    def profile_is_consistent(self) -> DeploymentProfile:
        if not self.zone.startswith(f"{self.region}-"):
            raise ValueError("zone must belong to the configured region")
        if self.firestore.location != self.region:
            raise ValueError("Firestore location must be explicitly aligned with this profile")
        expected_prefix = f"{self.region}-docker.pkg.dev/{self.project_id}/"
        if not self.artifact_registry.prefix.startswith(expected_prefix):
            raise ValueError("Artifact Registry prefix does not match project and region")
        expected_domain = f"@{self.project_id}.iam.gserviceaccount.com"
        if any(
            not identity.email.endswith(expected_domain)
            for identity in (
                self.identities.investigator,
                self.identities.executor,
                self.identities.scenario_controller,
                self.identities.github_deployer,
            )
        ):
            raise ValueError("Google service accounts must belong to the configured project")
        if set(self.images) != {"backend", "frontend", "executor", "scenario_controller"}:
            raise ValueError("profile must declare all incident-response image repositories")
        if len(self.required_apis) != len(set(self.required_apis)):
            raise ValueError("required APIs must be unique")
        return self


class ObservationStatus(StrEnum):
    COMPATIBLE = "compatible"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"


class BootstrapObservation(KubeCouncilModel):
    resource_id: str = Field(min_length=1)
    status: ObservationStatus
    summary: str = Field(min_length=1)
    details: dict[str, str | int | bool] = Field(default_factory=dict)


class BootstrapRequirement(KubeCouncilModel):
    resource_id: str = Field(min_length=1)
    category: str = Field(min_length=1)


class BootstrapAction(KubeCouncilModel):
    resource_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    approval: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    follow_up_commands: tuple[tuple[str, ...], ...] = ()
    reason: str = Field(min_length=1)


class BootstrapPlan(KubeCouncilModel):
    profile_id: str = Field(min_length=1)
    generated_at: datetime
    actions: tuple[BootstrapAction, ...]
    reused: tuple[str, ...]
    incompatible: tuple[str, ...]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        actions: tuple[BootstrapAction, ...],
        reused: tuple[str, ...],
        incompatible: tuple[str, ...],
    ) -> BootstrapPlan:
        payload = {
            "profile_id": profile_id,
            "actions": [action.model_dump(mode="json") for action in actions],
            "reused": reused,
            "incompatible": incompatible,
        }
        plan_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            profile_id=profile_id,
            generated_at=datetime.now(UTC),
            actions=actions,
            reused=reused,
            incompatible=incompatible,
            plan_hash=plan_hash,
        )


class BootstrapApplyReport(KubeCouncilModel):
    profile_id: str
    plan_hash: str
    applied: tuple[str, ...]
    reused: tuple[str, ...]


class PreflightCheck(KubeCouncilModel):
    check_id: str = Field(min_length=1)
    passed: bool
    summary: str = Field(min_length=1)


class PreflightReport(KubeCouncilModel):
    profile_id: str = Field(min_length=1)
    generated_at: datetime
    checks: tuple[PreflightCheck, ...] = Field(min_length=1)
    observations: tuple[BootstrapObservation, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)


class EnvironmentInventory(KubeCouncilModel):
    profile_id: str
    generated_at: datetime
    project_id: str
    region: str
    zone: str
    cluster: str
    kubernetes_context: str
    firestore_database: str
    firestore_location: str
    artifact_registry: str
    namespaces: tuple[str, ...]
    google_service_accounts: tuple[str, ...]
    workload_identity_bindings: tuple[str, ...]
    pubsub_topics: tuple[str, ...]
    pubsub_subscriptions: tuple[str, ...]
    image_references: dict[str, str]
    readiness: dict[str, bool]
    required_manual_inputs: tuple[str, ...]
    commands: tuple[str, ...]


def load_deployment_profile(path: Path) -> DeploymentProfile:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("deployment profile must contain a YAML mapping")
    return DeploymentProfile.from_untrusted(raw)
