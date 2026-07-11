from datetime import UTC, datetime, timedelta

import pytest

from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    InMemoryApplicationProfileProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import (
    AlertSignal,
    AuditEvent,
    EvidenceQuery,
    EvidenceQueryKind,
    IncidentLifecycle,
    SpecialistRole,
    transition_incident,
)
from app.services.alerts import (
    AlertIngestionService,
    AlertNotification,
    InMemoryAlertMessage,
    PubSubDelivery,
    PubSubPullConsumer,
)
from app.services.incident_store import FirestoreIncidentStore, InMemoryDocumentDatabase


@pytest.mark.parametrize(
    "store",
    [InMemoryIncidentStore(), FirestoreIncidentStore(InMemoryDocumentDatabase())],
)
def test_incident_stores_have_equivalent_append_and_compare_and_set_behavior(store: object) -> None:
    typed_store = store
    profile = fake_application_profile()
    signal = _signal("signal-1")
    record = typed_store.create(profile, signal)  # type: ignore[attr-defined]
    event = AuditEvent(
        event_id="event-1",
        incident_id=record.incident.incident_id,
        event_type="incident_opened",
        occurred_at=signal.observed_at,
        actor="alert-consumer",
    )

    appended = typed_store.append_audit_event(record.incident.incident_id, event)  # type: ignore[attr-defined]
    assert appended.audit_events[0].cursor == 1
    queried = typed_store.append_evidence_query(  # type: ignore[attr-defined]
        record.incident.incident_id,
        EvidenceQuery(
            query_id="query-1",
            incident_id=record.incident.incident_id,
            specialist=SpecialistRole.HEALTH,
            kind=EvidenceQueryKind.WORKLOAD_STATE,
            target=profile.workloads[0].reference,
            requested_at=signal.observed_at,
            query_round=1,
        ),
    )
    assert queried.evidence_queries[0].query_id == "query-1"
    updated = transition_incident(record.incident, lifecycle=IncidentLifecycle.INVESTIGATING)
    compared = typed_store.compare_and_set(  # type: ignore[attr-defined]
        record.incident.incident_id, expected_version=0, replacement=updated
    )
    assert compared.incident.version == 1
    with pytest.raises(ValueError, match="stale incident version"):
        typed_store.compare_and_set(  # type: ignore[attr-defined]
            record.incident.incident_id, expected_version=0, replacement=updated
        )


def test_alert_ingestion_deduplicates_delivery_and_acknowledges_after_persistence() -> None:
    store = InMemoryIncidentStore()
    profile = fake_application_profile()
    service = AlertIngestionService(
        store,
        InMemoryApplicationProfileProvider((profile,)),
        FakeEnrollmentProvider.ready_for(profile),
    )
    message = InMemoryAlertMessage(AlertNotification.from_signal(_signal("signal-1")))

    first = service.consume(message)
    second = service.consume(message.redelivered())

    assert first.incident.incident_id == second.incident.incident_id
    assert len(store.list()) == 1
    assert message.acknowledged is True
    assert len(second.audit_events) == 1


def test_pubsub_pull_acknowledges_only_a_durably_persisted_delivery() -> None:
    store = InMemoryIncidentStore()
    profile = fake_application_profile()
    subscription = FakeSubscription(
        PubSubDelivery(
            ack_id="ack-1",
            data=AlertNotification.from_signal(_signal("signal-1")).model_dump_json().encode(),
        )
    )
    consumer = PubSubPullConsumer(
        subscription,
        AlertIngestionService(
            store,
            InMemoryApplicationProfileProvider((profile,)),
            FakeEnrollmentProvider.ready_for(profile),
        ),
    )

    records = consumer.poll_once()

    assert len(records) == 1
    assert subscription.acknowledged == ["ack-1"]


def test_related_alerts_correlate_and_provider_closure_does_not_resolve_incident() -> None:
    store = InMemoryIncidentStore()
    profile = fake_application_profile()
    service = AlertIngestionService(
        store,
        InMemoryApplicationProfileProvider((profile,)),
        FakeEnrollmentProvider.ready_for(profile),
        correlation_window=timedelta(minutes=20),
    )
    opened = service.consume(
        InMemoryAlertMessage(AlertNotification.from_signal(_signal("signal-open")))
    )
    related = _signal(
        "signal-related",
        workload="redis-cart",
        observed_at=_signal("unused").observed_at + timedelta(minutes=2),
    )
    updated = service.consume(InMemoryAlertMessage(AlertNotification.from_signal(related)))
    closed = service.consume(
        InMemoryAlertMessage(
            AlertNotification.from_signal(
                _signal(
                    "signal-closed",
                    observed_at=_signal("unused").observed_at + timedelta(minutes=3),
                ),
                state="closed",
            )
        )
    )

    assert updated.incident.incident_id == opened.incident.incident_id
    assert closed.incident.incident_id == opened.incident.incident_id
    assert closed.incident.lifecycle is IncidentLifecycle.OPEN
    assert closed.audit_events[-1].event_type == "provider_alert_closed"
    assert closed.alert_signals[-1].provider_state == "closed"


def test_timeline_replay_uses_a_durable_exclusive_cursor() -> None:
    store = FirestoreIncidentStore(InMemoryDocumentDatabase())
    record = store.create(fake_application_profile(), _signal("signal-1"))
    for number in range(1, 4):
        store.append_audit_event(
            record.incident.incident_id,
            AuditEvent(
                event_id=f"event-{number}",
                incident_id=record.incident.incident_id,
                event_type=f"event_{number}",
                occurred_at=datetime.now(UTC),
                actor="test",
            ),
        )

    cursors = [event.cursor for event in store.timeline(record.incident.incident_id, after=1)]
    assert cursors == [2, 3]


def _signal(
    signal_id: str,
    *,
    workload: str = "recommendationservice",
    observed_at: datetime | None = None,
) -> AlertSignal:
    return AlertSignal(
        signal_id=signal_id,
        application_id="online-boutique",
        namespace="online-boutique",
        workload_name=workload,
        workload_namespace="online-boutique",
        summary=f"{workload} alert",
        observed_at=observed_at or datetime(2026, 7, 11, 0, 0, tzinfo=UTC),
        provider_incident_id="provider-incident-1",
    )


class FakeSubscription:
    def __init__(self, *deliveries: PubSubDelivery) -> None:
        self._deliveries = deliveries
        self.acknowledged: list[str] = []

    def pull(self, *, maximum_messages: int) -> tuple[PubSubDelivery, ...]:
        return self._deliveries[:maximum_messages]

    def acknowledge(self, ack_id: str) -> None:
        self.acknowledged.append(ack_id)
