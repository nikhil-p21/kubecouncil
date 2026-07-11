"""Opt-in live smoke tests for the KC-16 GCP persistence and delivery adapters."""

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("KUBECOUNCIL_RUN_GCP_INTEGRATION") != "1",
        reason="set KUBECOUNCIL_RUN_GCP_INTEGRATION=1 with dedicated test resources",
    ),
]


def test_firestore_adapter_can_list_a_dedicated_incident_collection() -> None:
    from google.cloud import firestore

    from app.services.incident_store import GoogleFirestoreDocumentDatabase

    collection = os.environ["KUBECOUNCIL_TEST_FIRESTORE_COLLECTION"]
    database = GoogleFirestoreDocumentDatabase(firestore.Client(), collection=collection)

    assert isinstance(database.list(), tuple)


def test_pubsub_adapter_can_pull_from_a_dedicated_empty_subscription() -> None:
    from google.cloud import pubsub_v1

    from app.services.alerts import GooglePubSubSubscription

    subscription = GooglePubSubSubscription(
        pubsub_v1.SubscriberClient(),
        os.environ["KUBECOUNCIL_TEST_PUBSUB_SUBSCRIPTION"],
        timeout_seconds=2,
    )

    assert isinstance(subscription.pull(maximum_messages=1), tuple)
