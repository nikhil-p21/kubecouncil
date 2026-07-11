"""Normalized, enrollment-scoped, idempotent alert ingestion."""

from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import Field

from app.domain.incidents import (
    AlertSignal,
    AlertSignalEvidence,
    ApplicationProfile,
    ApplicationProfileProvider,
    AuditEvent,
    EnrollmentProvider,
    IncidentLifecycle,
    IncidentStore,
    InvestigationRecord,
    WorkloadReference,
)
from app.domain.models import KubeCouncilModel
from app.services.enrollment import EnrollmentChecker, require_enrolled_target


class AlertNotification(KubeCouncilModel):
    """Provider-neutral input accepted from Pub/Sub and manual requests."""

    notification_id: str = Field(min_length=1)
    application_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    workload_name: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    state: Literal["open", "closed"] = "open"
    provider_incident_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None

    @classmethod
    def from_signal(
        cls, signal: AlertSignal, *, state: Literal["open", "closed"] = "open"
    ) -> "AlertNotification":
        return cls(
            notification_id=signal.signal_id,
            application_id=signal.application_id,
            namespace=signal.namespace,
            workload_name=signal.workload_name,
            summary=signal.summary,
            observed_at=signal.observed_at,
            state=state,
            provider_incident_id=signal.provider_incident_id,
            window_start=signal.window_start,
            window_end=signal.window_end,
        )


class AlertMessage(Protocol):
    notification: AlertNotification

    def acknowledge(self) -> None: ...


class PubSubDelivery(KubeCouncilModel):
    ack_id: str = Field(min_length=1)
    data: bytes = Field(min_length=1)


class PubSubSubscription(Protocol):
    def pull(self, *, maximum_messages: int) -> tuple[PubSubDelivery, ...]: ...

    def acknowledge(self, ack_id: str) -> None: ...


class _PubSubMessage(Protocol):
    data: bytes


class _PubSubReceivedMessage(Protocol):
    ack_id: str
    message: _PubSubMessage


class _PubSubPullResponse(Protocol):
    received_messages: tuple[_PubSubReceivedMessage, ...]


class _PubSubSubscriberClient(Protocol):
    def pull(self, *, request: dict[str, object], timeout: float) -> _PubSubPullResponse: ...

    def acknowledge(self, *, request: dict[str, object]) -> object: ...


class GooglePubSubSubscription:
    """Google Cloud Pub/Sub pull adapter with no SDK values in domain contracts."""

    def __init__(
        self,
        client: _PubSubSubscriberClient,
        subscription_path: str,
        *,
        timeout_seconds: float = 5,
    ) -> None:
        self._client = client
        self._subscription_path = subscription_path
        self._timeout_seconds = timeout_seconds

    def pull(self, *, maximum_messages: int) -> tuple[PubSubDelivery, ...]:
        response = self._client.pull(
            request={
                "subscription": self._subscription_path,
                "max_messages": maximum_messages,
            },
            timeout=self._timeout_seconds,
        )
        return tuple(
            PubSubDelivery(ack_id=item.ack_id, data=item.message.data)
            for item in response.received_messages
        )

    def acknowledge(self, ack_id: str) -> None:
        self._client.acknowledge(
            request={"subscription": self._subscription_path, "ack_ids": [ack_id]}
        )


class PubSubPullConsumer:
    """Bounded pull loop; failed validation or persistence intentionally leaves delivery unacked."""

    def __init__(
        self, subscription: PubSubSubscription, ingestion: "AlertIngestionService"
    ) -> None:
        self._subscription = subscription
        self._ingestion = ingestion

    def poll_once(self, *, maximum_messages: int = 10) -> tuple[InvestigationRecord, ...]:
        records: list[InvestigationRecord] = []
        for delivery in self._subscription.pull(maximum_messages=maximum_messages):
            notification = AlertNotification.model_validate_json(delivery.data)
            message = _PubSubAlertMessage(notification, self._subscription, delivery.ack_id)
            records.append(self._ingestion.consume(message))
        return tuple(records)


class _PubSubAlertMessage:
    def __init__(
        self,
        notification: AlertNotification,
        subscription: PubSubSubscription,
        ack_id: str,
    ) -> None:
        self.notification = notification
        self._subscription = subscription
        self._ack_id = ack_id

    def acknowledge(self) -> None:
        self._subscription.acknowledge(self._ack_id)


class InMemoryAlertMessage:
    def __init__(self, notification: AlertNotification) -> None:
        self.notification = notification
        self.acknowledged = False

    def acknowledge(self) -> None:
        self.acknowledged = True

    def redelivered(self) -> "InMemoryAlertMessage":
        return InMemoryAlertMessage(self.notification)


class AlertNormalizer:
    @staticmethod
    def normalize(notification: AlertNotification) -> AlertSignal:
        return AlertSignal(
            signal_id=notification.notification_id,
            application_id=notification.application_id,
            namespace=notification.namespace,
            workload_name=notification.workload_name,
            workload_namespace=notification.namespace,
            summary=notification.summary,
            observed_at=notification.observed_at,
            provider_incident_id=notification.provider_incident_id,
            window_start=notification.window_start,
            window_end=notification.window_end,
        )


class AlertIngestionService:
    """Persists a normalized signal before acknowledging its Pub/Sub delivery."""

    def __init__(
        self,
        store: IncidentStore,
        profiles: ApplicationProfileProvider,
        enrollment: EnrollmentProvider,
        *,
        correlation_window: timedelta = timedelta(minutes=15),
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._enrollment = enrollment
        self._correlation_window = correlation_window

    def consume(self, message: AlertMessage) -> InvestigationRecord:
        notification = message.notification
        signal = AlertNormalizer.normalize(notification)
        profile = self._required_profile(signal.application_id)
        target = WorkloadReference(namespace=signal.namespace, name=signal.workload_name)
        readiness = EnrollmentChecker().check(profile, self._enrollment.inspect(profile))
        require_enrolled_target(profile, readiness, target)

        duplicate = self._find_by_notification(notification.notification_id)
        if duplicate is not None:
            duplicate = self._ensure_alert_audit(duplicate, notification)
            message.acknowledge()
            return duplicate

        correlated = self._find_correlated(profile, notification)
        if correlated is None:
            if notification.state == "closed":
                raise ValueError("provider closure cannot create or resolve an Incident")
            correlated = self._store.create(profile, signal)

        correlated = self._store.append_alert_signal(
            correlated.incident.incident_id,
            AlertSignalEvidence(
                notification_id=notification.notification_id,
                incident_id=correlated.incident.incident_id,
                signal=signal,
                provider_state=notification.state,
                received_at=datetime.now(UTC),
            ),
        )

        persisted = self._ensure_alert_audit(correlated, notification)
        message.acknowledge()
        return persisted

    def _required_profile(self, application_id: str) -> ApplicationProfile:
        result = next(
            (
                item
                for item in self._profiles.list_profiles()
                if item.application_id == application_id
            ),
            None,
        )
        if result is None or result.profile is None:
            raise ValueError("alert application is not valid and enrolled")
        return result.profile

    def _find_by_notification(self, notification_id: str) -> InvestigationRecord | None:
        return next(
            (
                record
                for record in self._store.list()
                if any(item.notification_id == notification_id for item in record.alert_signals)
            ),
            None,
        )

    def _find_correlated(
        self, profile: ApplicationProfile, notification: AlertNotification
    ) -> InvestigationRecord | None:
        candidates = (
            record
            for record in self._store.list()
            if record.incident.application_id == notification.application_id
            and record.incident.lifecycle
            not in {IncidentLifecycle.RESOLVED, IncidentLifecycle.CLOSED}
            and abs(notification.observed_at - record.incident.opened_at)
            <= self._correlation_window
        )
        for record in candidates:
            if notification.provider_incident_id and any(
                item.signal.provider_incident_id == notification.provider_incident_id
                for item in record.alert_signals
            ):
                return record
            prior_targets = {item.signal.workload_name for item in record.alert_signals}
            if any(
                self._workloads_related(profile, notification.workload_name, prior)
                for prior in prior_targets
                if prior
            ):
                return record
        return None

    def _ensure_alert_audit(
        self, record: InvestigationRecord, notification: AlertNotification
    ) -> InvestigationRecord:
        if any(
            event.details.get("notification_id") == notification.notification_id
            for event in record.audit_events
        ):
            return record
        event_type = (
            "provider_alert_closed"
            if notification.state == "closed"
            else "alert_signal_received"
        )
        return self._store.append_audit_event(
            record.incident.incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=record.incident.incident_id,
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                actor="alert-consumer",
                details={
                    "notification_id": notification.notification_id,
                    "provider_incident_id": notification.provider_incident_id or "",
                    "namespace": notification.namespace,
                    "workload_name": notification.workload_name,
                    "provider_state": notification.state,
                },
            ),
        )

    @staticmethod
    def _workloads_related(profile: ApplicationProfile, left: str, right: str) -> bool:
        if left == right:
            return True
        workloads = {workload.reference.name: workload for workload in profile.workloads}
        left_workload = workloads.get(left)
        right_workload = workloads.get(right)
        if left_workload is None or right_workload is None:
            return False
        return right in left_workload.dependencies or left in right_workload.dependencies
