"""Short-lived Workload Identity smoke checks with deterministic cleanup."""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import Field

from app.domain.models import KubeCouncilModel


class SmokeProbe(Protocol):
    def vertex(self) -> None: ...

    def firestore_round_trip(self, marker: str) -> None: ...

    def pubsub_round_trip(self, marker: str) -> None: ...

    def logging_read(self) -> None: ...

    def monitoring_read(self) -> None: ...


class _HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...


class _AuthorizedSession(Protocol):
    def post(self, url: str, *, json: dict[str, object], timeout: int) -> _HttpResponse: ...

    def get(self, url: str, *, timeout: int) -> _HttpResponse: ...


class SmokeCheck(KubeCouncilModel):
    name: str = Field(min_length=1)
    passed: bool
    error_type: str | None = None


class SmokeReport(KubeCouncilModel):
    smoke_id: str = Field(min_length=1)
    generated_at: datetime
    checks: tuple[SmokeCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


class BootstrapSmokeRunner:
    """Runs all cloud capabilities without exposing provider payloads or credentials."""

    def __init__(self, probe: SmokeProbe) -> None:
        self._probe = probe

    def run(self, *, smoke_id: str | None = None) -> SmokeReport:
        identifier = smoke_id or f"smoke-{uuid4().hex}"
        checks: list[SmokeCheck] = []
        operations = (
            ("vertex", self._probe.vertex),
            ("firestore", lambda: self._probe.firestore_round_trip(identifier)),
            ("pubsub", lambda: self._probe.pubsub_round_trip(identifier)),
            ("logging-read", self._probe.logging_read),
            ("monitoring-read", self._probe.monitoring_read),
        )
        for name, operation in operations:
            try:
                operation()
            except Exception as error:  # provider type only; message may contain sensitive data
                checks.append(
                    SmokeCheck(name=name, passed=False, error_type=type(error).__name__)
                )
            else:
                checks.append(SmokeCheck(name=name, passed=True))
        return SmokeReport(
            smoke_id=identifier,
            generated_at=datetime.now(UTC),
            checks=tuple(checks),
        )


class GoogleCloudSmokeProbe:
    """Live adapter intended to run under the Investigator Kubernetes identity."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        model: str,
        firestore_database: str,
        pubsub_topic: str,
        pubsub_subscription: str,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._model = model
        self._firestore_database = firestore_database
        self._pubsub_topic = pubsub_topic
        self._pubsub_subscription = pubsub_subscription

    def vertex(self) -> None:
        genai = importlib.import_module("google.genai")
        client = genai.Client(
            vertexai=True,
            project=self._project_id,
            location=self._location,
        )
        response = client.models.generate_content(
            model=self._model,
            contents="Return the single word READY.",
        )
        if not getattr(response, "text", "").strip():
            raise RuntimeError("Vertex AI returned an empty smoke response")

    def firestore_round_trip(self, marker: str) -> None:
        firestore = importlib.import_module("google.cloud.firestore")
        client = firestore.Client(
            project=self._project_id,
            database=self._firestore_database,
        )
        document = client.collection("_kc_bootstrap_smoke").document(marker)
        try:
            document.create({"marker": marker, "bootstrap_smoke": True})
            snapshot = document.get()
            value = snapshot.to_dict() if snapshot.exists else None
            if value is None or value.get("marker") != marker:
                raise RuntimeError("Firestore smoke round trip did not match")
        finally:
            document.delete()

    def pubsub_round_trip(self, marker: str) -> None:
        pubsub = importlib.import_module("google.cloud.pubsub_v1")
        publisher = pubsub.PublisherClient()
        subscriber = pubsub.SubscriberClient()
        publisher.publish(self._pubsub_topic, marker.encode()).result(timeout=20)
        response = subscriber.pull(
            request={"subscription": self._pubsub_subscription, "max_messages": 1},
            timeout=20,
        )
        received = tuple(response.received_messages)
        if len(received) != 1 or received[0].message.data.decode() != marker:
            raise RuntimeError("Pub/Sub smoke round trip did not match")
        subscriber.acknowledge(
            request={
                "subscription": self._pubsub_subscription,
                "ack_ids": [received[0].ack_id],
            }
        )

    def logging_read(self) -> None:
        session = self._authorized_session()
        response = session.post(
            "https://logging.googleapis.com/v2/entries:list",
            json={
                "resourceNames": [f"projects/{self._project_id}"],
                "pageSize": 1,
                "filter": 'resource.type="k8s_container"',
            },
            timeout=20,
        )
        response.raise_for_status()

    def monitoring_read(self) -> None:
        session = self._authorized_session()
        response = session.get(
            (
                "https://monitoring.googleapis.com/v3/projects/"
                f"{self._project_id}/metricDescriptors?pageSize=1"
            ),
            timeout=20,
        )
        response.raise_for_status()

    @staticmethod
    def _authorized_session() -> _AuthorizedSession:
        auth = importlib.import_module("google.auth")
        transport = importlib.import_module("google.auth.transport.requests")
        credentials, _ = auth.default(
            scopes=("https://www.googleapis.com/auth/cloud-platform",)
        )
        session: _AuthorizedSession = transport.AuthorizedSession(credentials)
        return session


def main() -> int:
    required = {
        "project_id": "GOOGLE_CLOUD_PROJECT",
        "location": "GOOGLE_CLOUD_LOCATION",
        "model": "KUBECOUNCIL_GEMINI_MODEL",
        "firestore_database": "KUBECOUNCIL_FIRESTORE_DATABASE",
        "pubsub_topic": "KUBECOUNCIL_SMOKE_PUBSUB_TOPIC",
        "pubsub_subscription": "KUBECOUNCIL_SMOKE_PUBSUB_SUBSCRIPTION",
    }
    values = {name: os.getenv(variable, "") for name, variable in required.items()}
    missing = [variable for name, variable in required.items() if not values[name]]
    if missing:
        raise RuntimeError(f"missing smoke configuration: {', '.join(sorted(missing))}")
    report = BootstrapSmokeRunner(GoogleCloudSmokeProbe(**values)).run()
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
