"""Command-line entrypoint for preflight, plan, apply, and verify modes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import yaml

from app.bootstrap.inspector import LiveEnvironmentInspector, SubprocessCommandRunner
from app.bootstrap.models import (
    DeploymentProfile,
    EnvironmentInventory,
    PreflightReport,
    load_deployment_profile,
)
from app.bootstrap.planner import (
    BootstrapApprovalError,
    BootstrapPlanner,
    apply_bootstrap_plan,
)
from app.domain.models import KubeCouncilModel

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROFILE = ROOT / "deploy/profiles/findydevops-dev.yaml"
DEFAULT_MANIFESTS = ROOT / "manifests/incident-response/bootstrap"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kubecouncil-bootstrap",
        description="Plan and verify the credential-free KubeCouncil GCP/GKE environment.",
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--viewer-principal", action="append", default=[])
    parser.add_argument("--responder-principal", action="append", default=[])
    subcommands = parser.add_subparsers(dest="mode", required=True)
    subcommands.add_parser("preflight")
    plan = subcommands.add_parser("plan")
    plan.add_argument("--output", type=Path)
    apply = subcommands.add_parser("apply")
    apply.add_argument("--approve-plan-hash", required=True)
    apply.add_argument("--approve", action="append", default=[])
    apply.add_argument("--allow-mutation", action="store_true")
    verify = subcommands.add_parser("verify")
    verify.add_argument("--inventory", type=Path)
    verify.add_argument("--manifests", type=Path, default=DEFAULT_MANIFESTS)
    verify.add_argument("--server-dry-run", action="store_true")
    return parser


def _with_principals(profile: DeploymentProfile, args: argparse.Namespace) -> DeploymentProfile:
    if not args.viewer_principal and not args.responder_principal:
        return profile
    value = profile.model_dump(mode="python")
    value["iap"] = {
        "principal_source": "operator-input",
        "viewer_principals": tuple(args.viewer_principal),
        "responder_principals": tuple(args.responder_principal),
    }
    return DeploymentProfile.from_untrusted(value)


def _emit(value: object, *, output: Path | None = None) -> None:
    payload: object
    if isinstance(value, KubeCouncilModel):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{rendered}\n")
    print(rendered)


def _fail(message: str, *, exit_code: int = 2) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)


def validate_rendered_manifest(rendered: str) -> None:
    forbidden = (
        "PROJECT_ID",
        ":TAG",
        "replace-me",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PRIVATE KEY",
        "kubecouncil-rehearsal-manager",
        "REHEARSAL_NAMESPACE_PREFIX",
    )
    matches = [term for term in forbidden if term in rendered]
    if matches:
        raise ValueError(f"rendered manifest contains forbidden values: {', '.join(matches)}")


def build_inventory(
    profile: DeploymentProfile, report: PreflightReport
) -> EnvironmentInventory:
    image_references = {
        name: image.immutable_reference
        for name, image in profile.images.items()
        if image.immutable_reference is not None
    }
    required_manual_inputs: list[str] = []
    if not profile.iap.viewer_principals:
        required_manual_inputs.append("IAP Viewer principal list")
    if not profile.iap.responder_principals:
        required_manual_inputs.append("IAP Responder principal list")
    return EnvironmentInventory(
        profile_id=profile.profile_id,
        generated_at=datetime.now(UTC),
        project_id=profile.project_id,
        region=profile.region,
        zone=profile.zone,
        cluster=profile.cluster.name,
        kubernetes_context=profile.cluster.expected_context,
        firestore_database=profile.firestore.database_id,
        firestore_location=profile.firestore.location,
        artifact_registry=profile.artifact_registry.prefix,
        namespaces=(
            profile.kubernetes.system_namespace,
            profile.kubernetes.application_namespace,
            profile.kubernetes.demo_control_namespace,
        ),
        google_service_accounts=tuple(
            identity.email
            for identity in (
                profile.identities.investigator,
                profile.identities.executor,
                profile.identities.scenario_controller,
                profile.identities.github_deployer,
            )
        ),
        workload_identity_bindings=tuple(
            (
                f"{account.namespace}/{account.name} -> "
                f"{account.google_service_account}"
            )
            for account in (
                profile.kubernetes.service_accounts.investigator,
                profile.kubernetes.service_accounts.executor,
                profile.kubernetes.service_accounts.scenario_controller,
            )
        ),
        pubsub_topics=tuple(
            sorted(
                {
                    profile.pubsub.alerts.topic,
                    profile.pubsub.alerts.dead_letter_topic,
                    profile.pubsub.interventions.topic,
                    profile.pubsub.interventions.dead_letter_topic,
                }
            )
        ),
        pubsub_subscriptions=tuple(
            sorted(
                {
                    profile.pubsub.alerts.subscription,
                    profile.pubsub.alerts.dead_letter_subscription,
                    profile.pubsub.interventions.subscription,
                    profile.pubsub.interventions.dead_letter_subscription,
                }
            )
        ),
        image_references=image_references,
        readiness={check.check_id: check.passed for check in report.checks},
        required_manual_inputs=tuple(required_manual_inputs),
        commands=(
            (
                "PYTHONPATH=backend python -m app.bootstrap --profile "
                "deploy/profiles/findydevops-dev.yaml preflight"
            ),
            (
                "PYTHONPATH=backend python -m app.bootstrap --profile "
                "deploy/profiles/findydevops-dev.yaml plan"
            ),
            (
                "PYTHONPATH=backend python -m app.bootstrap --profile "
                "deploy/profiles/findydevops-dev.yaml verify --server-dry-run"
            ),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = _with_principals(load_deployment_profile(args.profile), args)
    inspector = LiveEnvironmentInspector()
    report = inspector.inspect(profile)
    if args.mode == "preflight":
        _emit(report)
        return 0 if report.ready else 2
    plan = BootstrapPlanner().plan(profile, report.observations)
    if args.mode == "plan":
        _emit(plan, output=args.output)
        return 0 if not plan.incompatible else 2
    if args.mode == "apply":
        if not args.allow_mutation:
            _fail("apply requires --allow-mutation in addition to the reviewed plan hash")
        try:
            applied = apply_bootstrap_plan(
                plan,
                approved_plan_hash=args.approve_plan_hash,
                approvals=frozenset(args.approve),
                runner=SubprocessCommandRunner(),
            )
        except BootstrapApprovalError as error:
            _fail(str(error))
        _emit(applied)
        return 0
    if plan.incompatible:
        _fail("verification failed because incompatible resources were found")
    if plan.actions:
        _fail("verification failed because required bootstrap resources are missing")
    render = SubprocessCommandRunner().capture(
        ("kubectl", "kustomize", str(args.manifests)), timeout=60
    )
    if render.returncode != 0:
        _fail(render.stderr.strip() or "client-side manifest rendering failed")
    try:
        validate_rendered_manifest(render.stdout)
    except ValueError as error:
        _fail(str(error))
    if args.server_dry_run:
        dry_run = SubprocessCommandRunner().capture(
            (
                "kubectl",
                "apply",
                "-k",
                str(args.manifests),
                "--server-side",
                "--dry-run=server",
            ),
            timeout=120,
        )
        if dry_run.returncode != 0:
            _fail(dry_run.stderr.strip() or "server-side dry-run failed")
    inventory = build_inventory(profile, report)
    if args.inventory:
        args.inventory.parent.mkdir(parents=True, exist_ok=True)
        args.inventory.write_text(
            yaml.safe_dump(inventory.model_dump(mode="json"), sort_keys=False)
        )
    _emit(inventory)
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
