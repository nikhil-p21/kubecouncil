import os

import pytest

from app.domain.incidents import (
    ApprovalDecision,
    InterventionState,
    InvestigationRecord,
    SpecialistRole,
)

pytestmark = pytest.mark.integration


def test_live_council_record_uses_only_real_providers() -> None:
    if os.getenv("KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("set KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION=1 for live GKE checks")
    notification_id = os.environ["KUBECOUNCIL_LIVE_NOTIFICATION_ID"]
    project_id = os.environ["KUBECOUNCIL_PROJECT_ID"]
    database_id = os.getenv("KUBECOUNCIL_FIRESTORE_DATABASE", "(default)")
    collection = os.getenv("KUBECOUNCIL_INCIDENT_COLLECTION", "kubecouncil-incidents")
    try:
        from google.cloud import firestore  # type: ignore[import-untyped]
    except ImportError as error:
        raise AssertionError("live integration dependencies are unavailable") from error

    client = firestore.Client(project=project_id, database=database_id)
    matching: list[InvestigationRecord] = []
    for snapshot in client.collection(collection).stream():
        record = InvestigationRecord.model_validate(snapshot.to_dict())
        if any(item.notification_id == notification_id for item in record.alert_signals):
            matching.append(record)

    assert len(matching) == 1
    record = matching[0]
    assert {item.source.value for item in record.evidence} == {
        "kubernetes",
        "cloud_logging",
        "cloud_monitoring",
    }
    assert not any("fake" in item.provider_reference for item in record.evidence)
    enrolled_targets = {workload.reference for workload in record.application_profile.workloads}
    assert all(item.scope in enrolled_targets for item in record.evidence)
    evidence_ids = {item.evidence_id for item in record.evidence}
    assert all(
        citation.evidence_id in evidence_ids
        for finding in record.findings
        for citation in finding.citations
    )
    assert {finding.specialist for finding in record.findings} == set(SpecialistRole)
    assert {invocation.role for invocation in record.model_invocations} == {
        *set(SpecialistRole),
        "coordinator",
    }
    coordinator = [
        invocation for invocation in record.model_invocations if invocation.role == "coordinator"
    ]
    assert len(coordinator) == 1
    assert coordinator[0].tool_count == 0
    assert all(
        invocation.tool_count <= 2
        for invocation in record.model_invocations
        if invocation.role != "coordinator"
    )
    assert all(invocation.model_id == "gemini-3.5-flash" for invocation in record.model_invocations)
    assert all(invocation.prompt_version for invocation in record.model_invocations)
    assert all(invocation.thinking_level for invocation in record.model_invocations)
    assert all(invocation.input_tokens > 0 for invocation in record.model_invocations)
    assert all(invocation.output_tokens > 0 for invocation in record.model_invocations)
    assert all(invocation.latency_ms > 0 for invocation in record.model_invocations)
    assert all(invocation.output_valid for invocation in record.model_invocations)
    assert all(invocation.failure_reason is None for invocation in record.model_invocations)
    assert record.proposal is None or record.proposal.action.target in enrolled_targets

    prompt_marker = os.getenv("KUBECOUNCIL_LIVE_PROMPT_INJECTION_MARKER")
    if prompt_marker:
        assert any(prompt_marker in item.redacted_excerpt for item in record.evidence)
        assert record.proposal is None or record.proposal.action.target.name != "redis-cart"


def test_live_components_use_distinct_identities_and_pinned_images() -> None:
    if os.getenv("KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("set KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION=1 for live GKE checks")
    try:
        from kubernetes import client, config  # type: ignore[import-not-found]
    except ImportError as error:
        raise AssertionError("live Kubernetes integration dependency is unavailable") from error
    config.load_kube_config()
    apps = client.AppsV1Api()

    investigator = apps.read_namespaced_deployment("investigator", "kubecouncil-system")
    executor = apps.read_namespaced_deployment("executor", "kubecouncil-system")
    ui = apps.read_namespaced_deployment("kubecouncil-ui", "kubecouncil-system")
    scenario = apps.read_namespaced_deployment(
        "scenario-controller", "kubecouncil-demo-control"
    )

    assert investigator.spec.template.spec.service_account_name == "investigator"
    assert executor.spec.template.spec.service_account_name == "executor"
    assert scenario.spec.template.spec.service_account_name == "scenario-controller"
    assert ui.spec.template.spec.automount_service_account_token is False
    for deployment in (investigator, executor, ui, scenario):
        assert deployment.status.ready_replicas == deployment.spec.replicas
        assert "@sha256:" in deployment.spec.template.spec.containers[0].image


def test_authenticated_approval_mutates_only_the_approved_online_boutique_target() -> None:
    if os.getenv("KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("set KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION=1 for live GKE checks")
    incident_id = os.environ["KUBECOUNCIL_LIVE_MUTATION_INCIDENT_ID"]
    project_id = os.environ["KUBECOUNCIL_PROJECT_ID"]
    database_id = os.getenv("KUBECOUNCIL_FIRESTORE_DATABASE", "(default)")
    collection = os.getenv("KUBECOUNCIL_INCIDENT_COLLECTION", "kubecouncil-incidents")
    responder = os.getenv("KUBECOUNCIL_LIVE_RESPONDER", "nikhil.p6257@gmail.com")
    try:
        from google.cloud import firestore  # type: ignore[import-untyped]
        from kubernetes import client, config  # type: ignore[import-not-found]
    except ImportError as error:
        raise AssertionError("live integration dependencies are unavailable") from error

    firestore_client = firestore.Client(project=project_id, database=database_id)
    snapshot = firestore_client.collection(collection).document(incident_id).get()
    assert snapshot.exists
    record = InvestigationRecord.model_validate(snapshot.to_dict())
    assert {item.source.value for item in record.evidence} == {
        "kubernetes",
        "cloud_logging",
        "cloud_monitoring",
    }
    assert {finding.specialist for finding in record.findings} == set(SpecialistRole)
    assert record.proposal is not None
    assert record.proposal.action.action_type == "rollback_deployment"
    assert record.proposal.action.target.namespace == "online-boutique"
    assert record.proposal.action.target.name == "recommendationservice"
    assert record.proposal.action.revision == 7
    assert len(record.approvals) == 1
    assert record.approvals[0].decision is ApprovalDecision.APPROVED
    assert record.approvals[0].responder_principal == responder
    assert len(record.interventions) == 1
    assert record.interventions[0].state is InterventionState.SUCCEEDED
    intervention_events = [
        event.event_type
        for event in record.audit_events
        if event.event_type.startswith("intervention_")
    ]
    assert intervention_events.count("intervention_mutated") == 1
    assert "intervention_dry_run_passed" in intervention_events
    assert "intervention_converged" in intervention_events

    config.load_kube_config()
    apps = client.AppsV1Api()
    recommendation = apps.read_namespaced_deployment(
        "recommendationservice", "online-boutique"
    )
    redis = apps.read_namespaced_deployment("redis-cart", "online-boutique")
    server = next(
        container
        for container in recommendation.spec.template.spec.containers
        if container.name == "server"
    )
    assert server.resources.requests["memory"] == "220Mi"
    assert server.resources.limits["memory"] == "450Mi"
    assert recommendation.status.ready_replicas == recommendation.spec.replicas == 1
    assert redis.status.ready_replicas == redis.spec.replicas == 1
