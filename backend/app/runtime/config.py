"""Strict environment contract for the deployed incident-response runtime."""

import os
from dataclasses import dataclass


class RuntimeConfigurationError(RuntimeError):
    """Deployed mode is missing an explicit real-provider binding."""


@dataclass(frozen=True)
class DeployedRuntimeConfig:
    project_id: str
    firestore_database: str
    incident_collection: str
    lease_collection: str
    alert_subscription: str
    intervention_topic: str
    intervention_subscription: str
    profile_path: str
    vertex_model: str
    vertex_location: str
    iap_audience: str
    responder_principals: frozenset[str]
    admission_policy_binding: str
    preflight_identity: str

    @classmethod
    def from_environment(cls) -> "DeployedRuntimeConfig":
        required = {
            "project_id": "KUBECOUNCIL_PROJECT_ID",
            "firestore_database": "KUBECOUNCIL_FIRESTORE_DATABASE",
            "alert_subscription": "KUBECOUNCIL_ALERT_SUBSCRIPTION",
            "intervention_topic": "KUBECOUNCIL_INTERVENTION_TOPIC",
            "intervention_subscription": "KUBECOUNCIL_INTERVENTION_SUBSCRIPTION",
            "profile_path": "KUBECOUNCIL_APPLICATION_PROFILE_PATH",
            "vertex_model": "KUBECOUNCIL_VERTEX_MODEL",
            "vertex_location": "GOOGLE_CLOUD_LOCATION",
            "iap_audience": "KUBECOUNCIL_IAP_AUDIENCE",
            "admission_policy_binding": "KUBECOUNCIL_ADMISSION_POLICY_BINDING",
            "preflight_identity": "KUBECOUNCIL_PREFLIGHT_IDENTITY",
        }
        values: dict[str, str] = {}
        for field, variable in required.items():
            value = os.getenv(variable, "").strip()
            if not value:
                raise RuntimeConfigurationError(
                    f"{variable} is required; deployed mode never falls back to a fake provider"
                )
            values[field] = value
        responders = frozenset(
            principal.strip().lower()
            for principal in os.getenv("KUBECOUNCIL_RESPONDER_PRINCIPALS", "").split(",")
            if principal.strip()
        )
        if not responders:
            raise RuntimeConfigurationError(
                "KUBECOUNCIL_RESPONDER_PRINCIPALS requires at least one verified Responder"
            )
        return cls(
            **values,
            incident_collection=os.getenv(
                "KUBECOUNCIL_INCIDENT_COLLECTION", "kubecouncil-incidents"
            ),
            lease_collection=os.getenv(
                "KUBECOUNCIL_LEASE_COLLECTION", "kubecouncil-workload-leases"
            ),
            responder_principals=responders,
        )

    @property
    def alert_subscription_path(self) -> str:
        return f"projects/{self.project_id}/subscriptions/{self.alert_subscription}"

    @property
    def intervention_topic_path(self) -> str:
        return f"projects/{self.project_id}/topics/{self.intervention_topic}"

    @property
    def intervention_subscription_path(self) -> str:
        return f"projects/{self.project_id}/subscriptions/{self.intervention_subscription}"


@dataclass(frozen=True)
class ExecutorRuntimeConfig:
    project_id: str
    firestore_database: str
    incident_collection: str
    lease_collection: str
    intervention_subscription: str
    admission_policy_binding: str

    @classmethod
    def from_environment(cls) -> "ExecutorRuntimeConfig":
        required = {
            "project_id": "KUBECOUNCIL_PROJECT_ID",
            "firestore_database": "KUBECOUNCIL_FIRESTORE_DATABASE",
            "intervention_subscription": "KUBECOUNCIL_INTERVENTION_SUBSCRIPTION",
            "admission_policy_binding": "KUBECOUNCIL_ADMISSION_POLICY_BINDING",
        }
        values: dict[str, str] = {}
        for field, variable in required.items():
            value = os.getenv(variable, "").strip()
            if not value:
                raise RuntimeConfigurationError(f"{variable} is required by the Executor")
            values[field] = value
        return cls(
            **values,
            incident_collection=os.getenv(
                "KUBECOUNCIL_INCIDENT_COLLECTION", "kubecouncil-incidents"
            ),
            lease_collection=os.getenv(
                "KUBECOUNCIL_LEASE_COLLECTION", "kubecouncil-workload-leases"
            ),
        )

    @property
    def intervention_subscription_path(self) -> str:
        return f"projects/{self.project_id}/subscriptions/{self.intervention_subscription}"
