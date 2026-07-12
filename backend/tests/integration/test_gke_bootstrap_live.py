"""Opt-in live verification for the KC-24A GCP/GKE bootstrap."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.bootstrap.inspector import LiveEnvironmentInspector
from app.bootstrap.models import load_deployment_profile
from app.bootstrap.planner import BootstrapPlanner
from app.bootstrap.smoke import BootstrapSmokeRunner, GoogleCloudSmokeProbe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("KUBECOUNCIL_RUN_GKE_BOOTSTRAP_INTEGRATION") != "1",
        reason="select the deployment profile explicitly before live bootstrap verification",
    ),
]

ROOT = Path(__file__).resolve().parents[3]


def test_live_environment_has_no_unreviewed_bootstrap_actions() -> None:
    profile = load_deployment_profile(ROOT / "deploy/profiles/findydevops-dev.yaml")

    report = LiveEnvironmentInspector().inspect(profile)
    plan = BootstrapPlanner().plan(profile, report.observations)

    assert not plan.incompatible
    assert not plan.actions


def test_investigator_workload_identity_cloud_smoke() -> None:
    required = (
        "KUBECOUNCIL_SMOKE_PUBSUB_TOPIC",
        "KUBECOUNCIL_SMOKE_PUBSUB_SUBSCRIPTION",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing short-lived smoke resources: {', '.join(missing)}")
    profile = load_deployment_profile(ROOT / "deploy/profiles/findydevops-dev.yaml")
    report = BootstrapSmokeRunner(
        GoogleCloudSmokeProbe(
            project_id=profile.project_id,
            location=profile.region,
            model=profile.vertex_model,
            firestore_database=profile.firestore.database_id,
            pubsub_topic=os.environ["KUBECOUNCIL_SMOKE_PUBSUB_TOPIC"],
            pubsub_subscription=os.environ["KUBECOUNCIL_SMOKE_PUBSUB_SUBSCRIPTION"],
        )
    ).run()

    assert report.passed, report.model_dump(mode="json")
