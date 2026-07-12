"""Long-running Pub/Sub workers for the unprivileged Investigator and deterministic Executor."""

import asyncio
import logging
import time
from typing import Any

from app.domain.incidents import AlertSignal, InterventionRequest, InvestigationOutcome
from app.runtime.config import ExecutorRuntimeConfig
from app.runtime.live_providers import (
    KubernetesEnrollmentProvider,
    KubernetesIncidentProvider,
    create_kubernetes_clients,
)
from app.services.alerts import (
    AlertIngestionService,
    AlertNotification,
    GooglePubSubSubscription,
    InMemoryAlertMessage,
)
from app.services.evidence import InitialEvidenceWindowCollector
from app.services.incident_store import FirestoreIncidentStore, GoogleFirestoreDocumentDatabase
from app.services.intervention_executor import (
    DeterministicInterventionExecutor,
    InterventionExecutionError,
)
from app.services.workload_lease import (
    FirestoreWorkloadLeaseStore,
    GoogleFirestoreLeaseDatabase,
)

logger = logging.getLogger(__name__)


def run_executor_worker() -> None:
    """Consume only hash-bound Intervention requests; this process has no HTTP or model runtime."""

    from google.cloud import firestore, pubsub_v1  # type: ignore[import-untyped]

    config = ExecutorRuntimeConfig.from_environment()
    core, apps, rbac, admission, api_client = create_kubernetes_clients()
    enrollment = KubernetesEnrollmentProvider(
        core_api=core,
        apps_api=apps,
        rbac_api=rbac,
        admission_api=admission,
        admission_policy_binding=config.admission_policy_binding,
        inspect_protected_dependencies=False,
    )
    kubernetes = KubernetesIncidentProvider(
        core_api=core,
        apps_api=apps,
        api_client=api_client,
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
    leases = FirestoreWorkloadLeaseStore(
        GoogleFirestoreLeaseDatabase(
            firestore_client,
            collection=config.lease_collection,
        )
    )
    executor = DeterministicInterventionExecutor(
        kubernetes=kubernetes,
        enrollment=enrollment,
        leases=leases,
        owner="gke-executor",
    )
    subscriber = GooglePubSubSubscription(
        pubsub_v1.SubscriberClient(),
        config.intervention_subscription_path,
        timeout_seconds=10,
    )
    while True:
        deliveries = subscriber.pull(maximum_messages=1)
        if not deliveries:
            time.sleep(1)
            continue
        for delivery in deliveries:
            try:
                request = InterventionRequest.model_validate_json(delivery.data)
                executor.consume(store, request)
            except InterventionExecutionError:
                logger.exception("intervention safely refused or halted request delivery")
                subscriber.acknowledge(delivery.ack_id)
            except Exception:
                logger.exception("intervention delivery failed before a deterministic outcome")
            else:
                subscriber.acknowledge(delivery.ack_id)


async def run_alert_worker(runtime: Any) -> None:
    """Durably ingest alerts, capture evidence, and start the real bounded Council."""

    subscription = runtime.alert_subscription
    store = runtime.incident_store
    profiles = runtime.application_profile_provider
    enrollment = runtime.enrollment_provider
    ingestion = AlertIngestionService(store, profiles, enrollment)
    collector = InitialEvidenceWindowCollector(
        runtime.evidence_provider,
        runtime.evidence_redactor,
    )
    while True:
        deliveries = await asyncio.to_thread(subscription.pull, maximum_messages=5)
        if not deliveries:
            await asyncio.sleep(1)
            continue
        for delivery in deliveries:
            try:
                notification = AlertNotification.model_validate_json(delivery.data)
                message = InMemoryAlertMessage(notification)
                record = await asyncio.to_thread(ingestion.consume, message)
                if not message.acknowledged:
                    raise RuntimeError("alert persistence completed without acknowledgement")
                subscription.acknowledge(delivery.ack_id)
                if not record.evidence:
                    collector.collect(
                        store,
                        incident_id=record.incident.incident_id,
                        profile=record.application_profile,
                        signal=notification_to_signal(notification),
                        window=record.evidence_window,
                    )
                current = store.get(record.incident.incident_id)
                if (
                    current is not None
                    and current.evidence
                    and current.incident.investigation_outcome
                    is InvestigationOutcome.NOT_STARTED
                ):
                    investigated = await runtime.incident_council.investigate(
                        store, record.incident.incident_id
                    )
                    if investigated.proposal is not None:
                        runtime.proposal_policy.evaluate_and_record(
                            store, record.incident.incident_id
                        )
            except Exception:
                logger.exception("alert delivery failed after durable ingestion")


def notification_to_signal(notification: AlertNotification) -> AlertSignal:
    from app.services.alerts import AlertNormalizer

    return AlertNormalizer.normalize(notification)


__all__ = ["run_alert_worker", "run_executor_worker"]
