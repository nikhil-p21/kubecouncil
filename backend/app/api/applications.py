"""Managed Application readiness endpoints backed by typed local fakes."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.identity import get_current_identity
from app.api.incidents import (
    get_application_profile_provider,
    get_enrollment_provider,
    get_incident_store,
)
from app.domain.incidents import (
    ApplicationProfileProvider,
    EnrollmentCheck,
    EnrollmentCheckCode,
    EnrollmentProvider,
    EnrollmentReadiness,
    IncidentStore,
    ManagedApplication,
)
from app.services.enrollment import EnrollmentChecker

router = APIRouter(
    prefix="/api/applications",
    tags=["applications"],
    dependencies=[Depends(get_current_identity)],
)


@router.get("", response_model=tuple[ManagedApplication, ...])
def list_managed_applications(
    profiles: Annotated[ApplicationProfileProvider, Depends(get_application_profile_provider)],
    enrollment_provider: Annotated[EnrollmentProvider, Depends(get_enrollment_provider)],
    incident_store: Annotated[IncidentStore, Depends(get_incident_store)],
) -> tuple[ManagedApplication, ...]:
    checker = EnrollmentChecker()
    applications: list[ManagedApplication] = []
    for load_result in profiles.list_profiles():
        profile = load_result.profile
        if profile is None:
            applications.append(
                ManagedApplication(
                    profile_load=load_result,
                    enrollment=EnrollmentReadiness(
                        application_id=load_result.application_id or "invalid-application-profile",
                        profile_version="invalid",
                        ready=False,
                        checks=(
                            EnrollmentCheck(
                                code=EnrollmentCheckCode.PROFILE_VALID,
                                passed=False,
                                message="; ".join(error.message for error in load_result.errors),
                            ),
                        ),
                    ),
                    incident_count=0,
                )
            )
            continue
        applications.append(
            ManagedApplication(
                application_profile=profile,
                profile_load=load_result,
                enrollment=checker.check(profile, enrollment_provider.inspect(profile)),
                incident_count=sum(
                    record.incident.application_id == profile.application_id
                    for record in incident_store.list()
                ),
            )
        )
    return tuple(applications)
