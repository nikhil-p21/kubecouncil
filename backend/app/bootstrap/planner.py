"""Deterministic, approval-bound planning for GCP and GKE bootstrap changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.bootstrap.models import (
    BootstrapAction,
    BootstrapApplyReport,
    BootstrapObservation,
    BootstrapPlan,
    BootstrapRequirement,
    DeploymentProfile,
    ObservationStatus,
)


class BootstrapApprovalError(ValueError):
    """Raised when apply is not bound to the exact reviewed plan and approvals."""


class BootstrapCommandRunner(Protocol):
    def run(self, command: tuple[str, ...]) -> None: ...


@dataclass
class RecordingCommandRunner:
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, command: tuple[str, ...]) -> None:
        self.commands.append(command)


class BootstrapPlanner:
    """Converts observed environment facts into a stable, reviewable action plan."""

    def requirements(self, profile: DeploymentProfile) -> tuple[BootstrapRequirement, ...]:
        requirements: list[BootstrapRequirement] = [
            *(
                BootstrapRequirement(resource_id=f"api/{api}", category="api")
                for api in profile.required_apis
            ),
            BootstrapRequirement(
                resource_id=f"cluster/{profile.cluster.name}", category="cluster"
            ),
            BootstrapRequirement(
                resource_id=f"registry/{profile.artifact_registry.repository}",
                category="artifact-registry",
            ),
            BootstrapRequirement(
                resource_id=f"firestore/{profile.firestore.database_id}",
                category="firestore",
            ),
        ]
        for stream in (profile.pubsub.alerts, profile.pubsub.interventions):
            requirements.extend(
                (
                    BootstrapRequirement(
                        resource_id=f"pubsub-topic/{stream.topic}", category="pubsub"
                    ),
                    BootstrapRequirement(
                        resource_id=f"pubsub-topic/{stream.dead_letter_topic}",
                        category="pubsub",
                    ),
                    BootstrapRequirement(
                        resource_id=f"pubsub-subscription/{stream.subscription}",
                        category="pubsub",
                    ),
                    BootstrapRequirement(
                        resource_id=f"pubsub-subscription/{stream.dead_letter_subscription}",
                        category="pubsub",
                    ),
                )
            )
        for identity_name in (
            "investigator",
            "executor",
            "scenario_controller",
            "github_deployer",
        ):
            identity = getattr(profile.identities, identity_name)
            requirements.append(
                BootstrapRequirement(resource_id=f"gsa/{identity.name}", category="identity")
            )
            requirements.extend(
                BootstrapRequirement(
                    resource_id=f"iam-role/{identity.name}/{role}", category="identity"
                )
                for role in identity.project_roles
            )
        requirements.extend(
            (
                BootstrapRequirement(
                    resource_id=f"namespace/{profile.kubernetes.system_namespace}",
                    category="identity",
                ),
                BootstrapRequirement(
                    resource_id=f"namespace/{profile.kubernetes.demo_control_namespace}",
                    category="identity",
                ),
            )
        )
        for identity_name in ("investigator", "executor", "scenario_controller"):
            service_account = getattr(profile.kubernetes.service_accounts, identity_name)
            requirements.append(
                BootstrapRequirement(
                    resource_id=(
                        f"ksa/{service_account.namespace}/{service_account.name}"
                    ),
                    category="identity",
                )
            )
        for identity_name in ("investigator", "executor", "scenario_controller"):
            requirements.append(
                BootstrapRequirement(
                    resource_id=f"workload-identity/{identity_name.replace('_', '-')}",
                    category="identity",
                )
            )
        requirements.extend(
            (
                BootstrapRequirement(
                    resource_id=f"github-pool/{profile.github.pool}", category="github"
                ),
                BootstrapRequirement(
                    resource_id=f"github-provider/{profile.github.provider}", category="github"
                ),
                BootstrapRequirement(
                    resource_id="github-binding/deployer", category="github"
                ),
            )
        )
        requirements.extend(
            BootstrapRequirement(resource_id=f"image/{name}", category="image")
            for name, image in profile.images.items()
            if image.available
        )
        return tuple(requirements)

    def plan(
        self,
        profile: DeploymentProfile,
        observations: tuple[BootstrapObservation, ...],
    ) -> BootstrapPlan:
        observed = {observation.resource_id: observation for observation in observations}
        actions: list[BootstrapAction] = []
        reused: list[str] = []
        incompatible: list[str] = []
        for requirement in self.requirements(profile):
            observation = observed.get(requirement.resource_id)
            status = observation.status if observation else ObservationStatus.MISSING
            if status == ObservationStatus.COMPATIBLE:
                reused.append(requirement.resource_id)
                continue
            if status == ObservationStatus.INCOMPATIBLE:
                summary = observation.summary if observation else "incompatible"
                incompatible.append(f"{requirement.resource_id}: {summary}")
                continue
            if requirement.category == "cluster" and profile.cluster.mode == "reuse-only":
                incompatible.append(
                    f"{requirement.resource_id}: reuse-only cluster is missing"
                )
                continue
            actions.append(self._action_for(profile, requirement))
        return BootstrapPlan.create(
            profile_id=profile.profile_id,
            actions=tuple(actions),
            reused=tuple(reused),
            incompatible=tuple(incompatible),
        )

    def _action_for(
        self, profile: DeploymentProfile, requirement: BootstrapRequirement
    ) -> BootstrapAction:
        resource_id = requirement.resource_id
        project_flag = f"--project={profile.project_id}"
        if resource_id.startswith("api/"):
            api = resource_id.removeprefix("api/")
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="enable-apis",
                command=("gcloud", "services", "enable", api, project_flag),
                reason="required Google API is disabled",
            )
        if resource_id.startswith("cluster/"):
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="create-cluster",
                command=(
                    "gcloud",
                    "container",
                    "clusters",
                    "create",
                    profile.cluster.name,
                    f"--zone={profile.zone}",
                    "--machine-type=e2-standard-4",
                    "--num-nodes=1",
                    f"--workload-pool={profile.cluster.workload_pool}",
                    project_flag,
                ),
                reason="explicit create-if-missing cluster is absent",
            )
        if resource_id.startswith("registry/"):
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="create-shared-resources",
                command=(
                    "gcloud",
                    "artifacts",
                    "repositories",
                    "create",
                    profile.artifact_registry.repository,
                    "--repository-format=docker",
                    f"--location={profile.region}",
                    project_flag,
                ),
                reason="configured Artifact Registry repository is absent",
            )
        if resource_id.startswith("firestore/"):
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval=f"firestore-location:{profile.firestore.location}",
                command=(
                    "gcloud",
                    "firestore",
                    "databases",
                    "create",
                    f"--database={profile.firestore.database_id}",
                    f"--location={profile.firestore.location}",
                    "--type=firestore-native",
                    "--delete-protection",
                    project_flag,
                ),
                reason="explicitly located Firestore Native database is absent",
            )
        if resource_id.startswith("pubsub-topic/"):
            topic = resource_id.removeprefix("pubsub-topic/")
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="create-shared-resources",
                command=("gcloud", "pubsub", "topics", "create", topic, project_flag),
                reason="required Pub/Sub topic is absent",
            )
        if resource_id.startswith("pubsub-subscription/"):
            name = resource_id.removeprefix("pubsub-subscription/")
            stream = next(
                candidate
                for candidate in (profile.pubsub.alerts, profile.pubsub.interventions)
                if name in {candidate.subscription, candidate.dead_letter_subscription}
            )
            is_dead_letter_reader = name == stream.dead_letter_subscription
            command = [
                "gcloud",
                "pubsub",
                "subscriptions",
                "create",
                name,
                f"--topic={stream.dead_letter_topic if is_dead_letter_reader else stream.topic}",
                f"--ack-deadline={10 if is_dead_letter_reader else stream.ack_deadline_seconds}",
                f"--message-retention-duration={stream.retention}",
                "--expiration-period=never",
            ]
            if not is_dead_letter_reader:
                command.extend(
                    (
                        f"--dead-letter-topic={stream.dead_letter_topic}",
                        f"--max-delivery-attempts={stream.maximum_delivery_attempts}",
                        f"--min-retry-delay={stream.retry_minimum}",
                        f"--max-retry-delay={stream.retry_maximum}",
                    )
                )
            command.append(project_flag)
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="create-shared-resources",
                command=tuple(command),
                reason="required Pub/Sub subscription is absent",
            )
        if resource_id.startswith("gsa/"):
            name = resource_id.removeprefix("gsa/")
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="identity",
                command=(
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "create",
                    name,
                    project_flag,
                ),
                reason="required Google service account is absent",
            )
        if resource_id.startswith("iam-role/"):
            _, identity_name, role = resource_id.split("/", maxsplit=2)
            identity = next(
                candidate
                for candidate in (
                    profile.identities.investigator,
                    profile.identities.executor,
                    profile.identities.scenario_controller,
                    profile.identities.github_deployer,
                )
                if candidate.name == identity_name
            )
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="identity",
                command=(
                    "gcloud",
                    "projects",
                    "add-iam-policy-binding",
                    profile.project_id,
                    f"--member=serviceAccount:{identity.email}",
                    f"--role={role}",
                    "--condition=None",
                ),
                reason="required least-privilege project role is absent",
            )
        if resource_id.startswith("namespace/"):
            namespace = resource_id.removeprefix("namespace/")
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="identity",
                command=("kubectl", "create", "namespace", namespace),
                reason="required Kubernetes identity namespace is absent",
            )
        if resource_id.startswith("ksa/"):
            _, namespace, name = resource_id.split("/", maxsplit=2)
            account = next(
                candidate
                for candidate in (
                    profile.kubernetes.service_accounts.investigator,
                    profile.kubernetes.service_accounts.executor,
                    profile.kubernetes.service_accounts.scenario_controller,
                )
                if candidate.namespace == namespace and candidate.name == name
            )
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="identity",
                command=(
                    "kubectl",
                    "create",
                    "serviceaccount",
                    name,
                    f"--namespace={namespace}",
                ),
                follow_up_commands=(
                    (
                        "kubectl",
                        "annotate",
                        "serviceaccount",
                        name,
                        f"--namespace={namespace}",
                        f"iam.gke.io/gcp-service-account={account.google_service_account}",
                        "--overwrite",
                    ),
                ),
                reason="required Kubernetes service account is absent",
            )
        if resource_id.startswith("workload-identity/"):
            identity_name = resource_id.removeprefix("workload-identity/").replace("-", "_")
            identity = getattr(profile.identities, identity_name)
            account = getattr(profile.kubernetes.service_accounts, identity_name)
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="identity",
                command=(
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "add-iam-policy-binding",
                    identity.email,
                    "--role=roles/iam.workloadIdentityUser",
                    (
                        "--member=serviceAccount:"
                        f"{profile.project_id}.svc.id.goog[{account.namespace}/{account.name}]"
                    ),
                    project_flag,
                ),
                follow_up_commands=(
                    (
                        "kubectl",
                        "annotate",
                        "serviceaccount",
                        account.name,
                        f"--namespace={account.namespace}",
                        f"iam.gke.io/gcp-service-account={identity.email}",
                        "--overwrite",
                    ),
                ),
                reason="Workload Identity binding or annotation is absent",
            )
        if resource_id.startswith("github-pool/"):
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="github-federation",
                command=(
                    "gcloud",
                    "iam",
                    "workload-identity-pools",
                    "create",
                    profile.github.pool,
                    "--location=global",
                    "--display-name=GitHub Actions",
                    project_flag,
                ),
                reason="GitHub Workload Identity pool is absent",
            )
        if resource_id.startswith("github-provider/"):
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="github-federation",
                command=(
                    "gcloud",
                    "iam",
                    "workload-identity-pools",
                    "providers",
                    "create-oidc",
                    profile.github.provider,
                    f"--workload-identity-pool={profile.github.pool}",
                    "--location=global",
                    "--issuer-uri=https://token.actions.githubusercontent.com",
                    (
                        "--attribute-mapping=google.subject=assertion.sub,"
                        "attribute.repository=assertion.repository,attribute.ref=assertion.ref"
                    ),
                    f"--attribute-condition=assertion.repository=='{profile.github.repository}'",
                    project_flag,
                ),
                reason="repository-restricted GitHub OIDC provider is absent",
            )
        if resource_id == "github-binding/deployer":
            principal = (
                "principalSet://iam.googleapis.com/projects/"
                f"{profile.project_number}/locations/global/workloadIdentityPools/"
                f"{profile.github.pool}/attribute.repository/{profile.github.repository}"
            )
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="github-federation",
                command=(
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "add-iam-policy-binding",
                    profile.identities.github_deployer.email,
                    "--role=roles/iam.workloadIdentityUser",
                    f"--member={principal}",
                    project_flag,
                ),
                reason="GitHub repository impersonation binding is absent",
            )
        if resource_id.startswith("image/"):
            name = resource_id.removeprefix("image/")
            image = profile.images[name]
            return BootstrapAction(
                resource_id=resource_id,
                category=requirement.category,
                approval="publish-images",
                command=(
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    image.platform,
                    "--tag",
                    f"{image.repository}:{image.tag}",
                    "--push",
                    image.build_context or "",
                ),
                reason="immutable image tag and digest are not published",
            )
        raise ValueError(f"unsupported bootstrap resource: {resource_id}")


def apply_bootstrap_plan(
    plan: BootstrapPlan,
    *,
    approved_plan_hash: str,
    approvals: frozenset[str],
    runner: BootstrapCommandRunner,
) -> BootstrapApplyReport:
    if approved_plan_hash != plan.plan_hash:
        raise BootstrapApprovalError("approved plan hash does not match the current plan hash")
    if plan.incompatible:
        raise BootstrapApprovalError("incompatible resources prevent bootstrap apply")
    required = {action.approval for action in plan.actions}
    missing = sorted(required - approvals)
    if missing:
        raise BootstrapApprovalError(f"missing explicit approval: {', '.join(missing)}")
    applied: list[str] = []
    for action in plan.actions:
        runner.run(action.command)
        for command in action.follow_up_commands:
            runner.run(command)
        applied.append(action.resource_id)
    return BootstrapApplyReport(
        profile_id=plan.profile_id,
        plan_hash=plan.plan_hash,
        applied=tuple(applied),
        reused=plan.reused,
    )


def smoke_resource_can_be_deleted(labels: dict[str, str], *, environment: str) -> bool:
    """Deletion guard used by smoke cleanup; shared resources can never satisfy it."""

    return (
        labels.get("kubecouncil.io/bootstrap-smoke") == "true"
        and labels.get("kubecouncil.io/environment") == environment
    )
