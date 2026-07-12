"""Explicit runtime wiring; deployed mode contains no fake fallback path."""

import os

from fastapi import FastAPI

from app.domain.identity import OperatorIdentity, OperatorRole
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryApplicationProfileProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import EvidenceSource
from app.runtime.config import DeployedRuntimeConfig, RuntimeConfigurationError
from app.runtime.live_providers import (
    FileApplicationProfileProvider,
    GoogleCloudLoggingReader,
    GoogleCloudMonitoringReader,
    KubernetesEnrollmentProvider,
    KubernetesIncidentProvider,
    LiveInitialEvidenceProvider,
    create_authorized_session,
    create_impersonated_apps_client,
    create_kubernetes_clients,
)
from app.runtime.readiness import ReadinessRegistry
from app.services.adk_council import GoogleADKIncidentCouncilModel
from app.services.approval import ApprovalService
from app.services.council import BoundedIncidentCouncil, FakeIncidentCouncilModel
from app.services.enrollment import EnrollmentChecker
from app.services.evidence import DeterministicEvidenceRedactor
from app.services.evidence_gateway import (
    CloudLoggingEvidenceAdapter,
    CloudMonitoringEvidenceAdapter,
    EvidenceQueryGateway,
    FakeEvidenceQueryAdapter,
    KubernetesEvidenceAdapter,
)
from app.services.identity import IAPIdentityProvider, LocalIdentityProvider
from app.services.incident_store import (
    FirestoreIncidentStore,
    GoogleFirestoreDocumentDatabase,
)
from app.services.intervention_queue import (
    InMemoryInterventionQueue,
    PubSubInterventionPublisher,
)
from app.services.proposal_policy import (
    DeterministicProposalPolicy,
    FakePolicyKubernetesProvider,
)


async def compose_runtime(application: FastAPI) -> None:
    mode = os.getenv("KUBECOUNCIL_RUNTIME_MODE", "deployed").strip().lower()
    application.state.runtime_mode = mode
    if mode in {"development", "test"}:
        _compose_local(application, mode)
        return
    if mode != "deployed":
        raise RuntimeConfigurationError(
            "KUBECOUNCIL_RUNTIME_MODE must be development, test, or deployed"
        )
    await _compose_deployed(application, DeployedRuntimeConfig.from_environment())


def _compose_local(application: FastAPI, mode: str) -> None:
    readiness = ReadinessRegistry()
    profile = fake_application_profile()
    store = InMemoryIncidentStore()
    enrollment = FakeEnrollmentProvider.ready_for(profile)
    redactor = DeterministicEvidenceRedactor()
    query_adapter = FakeEvidenceQueryAdapter()
    gateway = EvidenceQueryGateway(
        adapters={
            EvidenceSource.KUBERNETES: query_adapter,
            EvidenceSource.CLOUD_LOGGING: query_adapter,
            EvidenceSource.CLOUD_MONITORING: query_adapter,
        },
        redactor=redactor,
    )
    council = BoundedIncidentCouncil(FakeIncidentCouncilModel(), evidence_gateway=gateway)
    policy_kubernetes = FakePolicyKubernetesProvider.ready()
    queue = InMemoryInterventionQueue()
    principal = os.getenv("KUBECOUNCIL_LOCAL_PRINCIPAL", "developer@example.com")

    application.state.incident_store = store
    application.state.application_profile_provider = InMemoryApplicationProfileProvider((profile,))
    application.state.enrollment_provider = enrollment
    application.state.evidence_provider = FakeEvidenceProvider()
    application.state.evidence_redactor = redactor
    application.state.evidence_query_gateway = gateway
    application.state.incident_council = council
    application.state.proposal_policy = DeterministicProposalPolicy(
        policy_kubernetes, enrollment
    )
    application.state.intervention_publisher = queue
    application.state.approval_service = ApprovalService(
        policy_kubernetes, publisher=queue
    )
    application.state.identity_provider = LocalIdentityProvider(
        runtime_mode="development",
        identity=OperatorIdentity(
            principal=principal,
            subject=f"local:{principal}",
            role=OperatorRole.RESPONDER,
        ),
    )
    for name in ("api", "identity", "incident_store", "evidence", "council", "intervention"):
        readiness.set(name, ready=True, detail=f"Explicit {mode} provider is ready.")
    application.state.readiness = readiness


async def _compose_deployed(application: FastAPI, config: DeployedRuntimeConfig) -> None:
    readiness = ReadinessRegistry()
    readiness.set("api", ready=True, detail="API process is available.")
    try:
        from google.cloud import firestore, pubsub_v1  # type: ignore[import-untyped]
    except ImportError as error:
        raise RuntimeConfigurationError(
            "deployed mode requires google-cloud-firestore and google-cloud-pubsub"
        ) from error

    core, apps, rbac, admission, api_client = create_kubernetes_clients()
    profile_provider = FileApplicationProfileProvider(config.profile_path)
    enrollment = KubernetesEnrollmentProvider(
        core_api=core,
        apps_api=apps,
        rbac_api=rbac,
        admission_api=admission,
        admission_policy_binding=config.admission_policy_binding,
    )
    kubernetes = KubernetesIncidentProvider(
        core_api=core,
        apps_api=apps,
        api_client=api_client,
        dry_run_apps_api=create_impersonated_apps_client(config.preflight_identity),
    )
    session = create_authorized_session()
    logging_reader = GoogleCloudLoggingReader(session, project_id=config.project_id)
    monitoring_reader = GoogleCloudMonitoringReader(session, project_id=config.project_id)
    kubernetes_adapter = KubernetesEvidenceAdapter(kubernetes)
    logging_adapter = CloudLoggingEvidenceAdapter(logging_reader)
    monitoring_adapter = CloudMonitoringEvidenceAdapter(monitoring_reader)
    redactor = DeterministicEvidenceRedactor()
    evidence_gateway = EvidenceQueryGateway(
        adapters={
            EvidenceSource.KUBERNETES: kubernetes_adapter,
            EvidenceSource.CLOUD_LOGGING: logging_adapter,
            EvidenceSource.CLOUD_MONITORING: monitoring_adapter,
        },
        redactor=redactor,
    )
    evidence_provider = LiveInitialEvidenceProvider(
        kubernetes=kubernetes_adapter,
        logging=logging_adapter,
        monitoring=monitoring_adapter,
    )

    firestore_client = firestore.Client(
        project=config.project_id,
        database=config.firestore_database,
    )
    store = FirestoreIncidentStore(
        GoogleFirestoreDocumentDatabase(
            firestore_client,
            collection=config.incident_collection,
        )
    )
    publisher_client = pubsub_v1.PublisherClient()
    subscriber_client = pubsub_v1.SubscriberClient()
    publisher = PubSubInterventionPublisher(
        publisher_client,
        topic=config.intervention_topic_path,
    )
    model = GoogleADKIncidentCouncilModel(model_id=config.vertex_model)
    council = BoundedIncidentCouncil(
        model,
        model_id=config.vertex_model,
        evidence_gateway=evidence_gateway,
    )

    application.state.runtime_config = config
    from app.services.alerts import GooglePubSubSubscription

    application.state.alert_subscription = GooglePubSubSubscription(
        subscriber_client,
        config.alert_subscription_path,
        timeout_seconds=10,
    )
    application.state.incident_store = store
    application.state.application_profile_provider = profile_provider
    application.state.enrollment_provider = enrollment
    application.state.evidence_provider = evidence_provider
    application.state.evidence_redactor = redactor
    application.state.evidence_query_gateway = evidence_gateway
    application.state.incident_council = council
    application.state.proposal_policy = DeterministicProposalPolicy(kubernetes, enrollment)
    application.state.intervention_publisher = publisher
    application.state.approval_service = ApprovalService(kubernetes, publisher=publisher)
    application.state.identity_provider = IAPIdentityProvider(
        audience=config.iap_audience,
        responder_principals=config.responder_principals,
    )
    application.state.readiness = readiness

    readiness.set(
        "identity",
        ready=True,
        detail="Signed IAP assertion verification and Responder mapping are configured.",
    )
    try:
        store.list()
    except Exception as error:
        readiness.set("incident_store", ready=False, detail=_readiness_error(error))
    else:
        readiness.set(
            "incident_store",
            ready=True,
            detail="Firestore IncidentStore transaction boundary is reachable.",
        )

    profile_results = profile_provider.list_profiles()
    profile = profile_results[0].profile if profile_results else None
    if profile is None:
        detail = "; ".join(
            issue.message for result in profile_results for issue in result.errors
        ) or "No Application Profile was loaded."
        readiness.set("evidence", ready=False, detail=detail)
        readiness.set("intervention", ready=False, detail=detail)
    else:
        try:
            enrollment_readiness = EnrollmentChecker().check(profile, enrollment.inspect(profile))
        except Exception as error:
            readiness.set("evidence", ready=False, detail=_readiness_error(error))
            readiness.set("intervention", ready=False, detail=_readiness_error(error))
        else:
            readiness.set(
                "evidence",
                ready=enrollment_readiness.ready,
                detail=(
                    "Kubernetes, Cloud Logging, and Cloud Monitoring evidence adapters are "
                    "bound to the enrolled Application Profile."
                    if enrollment_readiness.ready
                    else "; ".join(check.message for check in enrollment_readiness.failed_checks)
                ),
            )
            readiness.set(
                "intervention",
                ready=enrollment_readiness.ready,
                detail=(
                    "Executor RBAC, managed labels, Enrollment, and admission binding are active."
                    if enrollment_readiness.ready
                    else "; ".join(check.message for check in enrollment_readiness.failed_checks)
                ),
            )
    try:
        await model.probe()
    except Exception as error:
        readiness.set(
            "council",
            ready=False,
            detail=f"ADK structured Specialist/Coordinator probe failed: {_readiness_error(error)}",
        )
    else:
        readiness.set(
            "council",
            ready=True,
            detail=(
                "ADK structured Specialist and Coordinator contracts passed on "
                f"{config.vertex_model}."
            ),
        )


def _readiness_error(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error)[:300]}"


__all__ = ["compose_runtime"]
