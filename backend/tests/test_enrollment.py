from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.incidents import (
    get_application_profile_provider,
    get_enrollment_provider,
    get_evidence_provider,
    get_evidence_redactor,
    get_incident_store,
)
from app.domain.incident_fakes import (
    FakeEnrollmentProvider,
    FakeEvidenceProvider,
    InMemoryApplicationProfileProvider,
    InMemoryIncidentStore,
    fake_application_profile,
)
from app.domain.incidents import ApplicationProfile, ManagedWorkload, WorkloadReference
from app.main import app
from app.services.enrollment import EnrollmentChecker, require_enrolled_target
from app.services.evidence import DeterministicEvidenceRedactor


def test_application_profile_rejects_a_dependency_outside_its_workloads() -> None:
    profile = fake_application_profile()
    invalid_workload = profile.workloads[0].model_copy(update={"dependencies": ("missing",)})

    with pytest.raises(ValidationError, match="declared workload"):
        ApplicationProfile.model_validate(
            profile.model_dump() | {"workloads": (invalid_workload, *profile.workloads[1:])}
        )


def test_application_profile_rejects_an_unclassified_non_executable_workload() -> None:
    profile = fake_application_profile()

    with pytest.raises(ValidationError, match="protected dependencies"):
        ManagedWorkload(
            reference=WorkloadReference(namespace=profile.namespace, name="catalogservice"),
            criticality=profile.workloads[0].criticality,
            replica_bounds=profile.workloads[0].replica_bounds,
            executable=False,
        )


def test_application_profile_requires_distinct_investigator_and_executor_identities() -> None:
    profile = fake_application_profile()

    with pytest.raises(ValidationError, match="identities must be distinct"):
        ApplicationProfile.model_validate(
            profile.model_dump() | {"executor_identity": profile.investigator_identity}
        )


def test_enrollment_readiness_reports_each_failed_prerequisite() -> None:
    profile = fake_application_profile()
    readiness = EnrollmentChecker().check(
        profile,
        FakeEnrollmentProvider.unready_for(profile).inspect(profile),
    )

    assert not readiness.ready
    assert {check.code for check in readiness.failed_checks} == {
        "namespace_enrolled_label",
        "investigator_role_binding",
        "executor_role_binding",
        "managed_workload_label",
        "admission_policy_binding",
    }
    assert all(check.message for check in readiness.failed_checks)


def test_enrollment_checks_role_bindings_for_each_namespace_and_identity() -> None:
    profile = fake_application_profile()
    multi_namespace_profile = ApplicationProfile.model_validate(
        profile.model_dump() | {"namespaces": ("online-boutique", "online-boutique-canary")}
    )
    ready_snapshot = FakeEnrollmentProvider.ready_for(multi_namespace_profile).inspect(
        multi_namespace_profile
    )
    missing_canary_executor = ready_snapshot.model_copy(
        update={
            "role_bindings": tuple(
                binding
                for binding in ready_snapshot.role_bindings
                if not (
                    binding.namespace == "online-boutique-canary"
                    and binding.subject == multi_namespace_profile.executor_identity
                )
            )
        }
    )

    readiness = EnrollmentChecker().check(multi_namespace_profile, missing_canary_executor)

    assert not readiness.ready
    assert any(
        check.code == "executor_role_binding" and "online-boutique-canary" in check.message
        for check in readiness.failed_checks
    )


def test_enrollment_requires_readiness_and_keeps_protected_dependencies_observe_only() -> None:
    profile = fake_application_profile()
    readiness = EnrollmentChecker().check(
        profile, FakeEnrollmentProvider.ready_for(profile).inspect(profile)
    )
    protected = profile.workloads[1].reference

    assert require_enrolled_target(profile, readiness, protected).protected_dependency
    with pytest.raises(ValueError, match="protected dependency"):
        require_enrolled_target(profile, readiness, protected, require_executable=True)

    unavailable = EnrollmentChecker().check(
        profile,
        FakeEnrollmentProvider.unready_for(profile).inspect(profile),
    )
    with pytest.raises(ValueError, match="not ready"):
        require_enrolled_target(profile, unavailable, profile.workloads[0].reference)
    with pytest.raises(ValueError, match="outside the enrolled"):
        require_enrolled_target(
            profile,
            readiness,
            WorkloadReference(namespace=profile.namespace, name="unmanaged-service"),
        )


def test_managed_applications_api_shows_readiness_and_incident_history() -> None:
    profile = fake_application_profile()
    profiles = InMemoryApplicationProfileProvider((profile,))
    enrollment = FakeEnrollmentProvider.ready_for(profile)
    store = InMemoryIncidentStore()
    store.open_fake_incident("recommendationservice is OOMKilled")
    app.dependency_overrides[get_application_profile_provider] = lambda: profiles
    app.dependency_overrides[get_enrollment_provider] = lambda: enrollment
    app.dependency_overrides[get_incident_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.get("/api/applications")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body[0]["application_profile"]["application_id"] == "online-boutique"
    assert body[0]["enrollment"]["ready"] is True
    assert body[0]["incident_count"] == 1
    assert body[0]["health"]["status"] == "unknown"


def test_managed_applications_api_exposes_invalid_profile_without_enabling_enrollment() -> None:
    profiles = InMemoryApplicationProfileProvider(
        ({"application_id": "broken-profile", "namespace": "online-boutique"},)
    )
    app.dependency_overrides[get_application_profile_provider] = lambda: profiles
    app.dependency_overrides[get_enrollment_provider] = FakeEnrollmentProvider.empty
    app.dependency_overrides[get_incident_store] = InMemoryIncidentStore
    client = TestClient(app)
    try:
        response = client.get("/api/applications")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body[0]["profile_load"]["valid"] is False
    assert body[0]["enrollment"]["ready"] is False
    assert body[0]["enrollment"]["failed_checks"][0]["code"] == "profile_valid"


def test_incident_api_rejects_invalid_profile_and_unmanaged_target() -> None:
    invalid_profiles = InMemoryApplicationProfileProvider(
        ({"application_id": "online-boutique", "namespace": "online-boutique"},)
    )
    app.dependency_overrides[get_application_profile_provider] = lambda: invalid_profiles
    app.dependency_overrides[get_enrollment_provider] = FakeEnrollmentProvider.empty
    app.dependency_overrides[get_incident_store] = InMemoryIncidentStore
    app.dependency_overrides[get_evidence_provider] = lambda: FakeEvidenceProvider()
    app.dependency_overrides[get_evidence_redactor] = DeterministicEvidenceRedactor
    client = TestClient(app)
    try:
        invalid_profile = client.post("/api/incidents", json={"summary": "restart spike"})
    finally:
        app.dependency_overrides.clear()

    assert invalid_profile.status_code == 409
    assert invalid_profile.json()["detail"]["code"] == "profile_invalid"

    profile = fake_application_profile()
    app.dependency_overrides[get_application_profile_provider] = lambda: (
        InMemoryApplicationProfileProvider((profile,))
    )
    app.dependency_overrides[get_enrollment_provider] = lambda: FakeEnrollmentProvider.ready_for(
        profile
    )
    app.dependency_overrides[get_incident_store] = InMemoryIncidentStore
    app.dependency_overrides[get_evidence_provider] = lambda: FakeEvidenceProvider()
    app.dependency_overrides[get_evidence_redactor] = DeterministicEvidenceRedactor
    client = TestClient(app)
    try:
        unmanaged = client.post(
            "/api/incidents",
            json={
                "summary": "restart spike",
                "target": {"namespace": "online-boutique", "name": "unmanaged-service"},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert unmanaged.status_code == 409
    assert unmanaged.json()["detail"]["code"] == "enrollment_not_ready"


def test_profile_reload_exposes_invalid_configuration_without_authority() -> None:
    profiles = InMemoryApplicationProfileProvider((fake_application_profile(),))

    reload_result = profiles.reload(({"application_id": "broken-profile"},))

    assert reload_result[0].valid is False
    assert reload_result[0].errors
