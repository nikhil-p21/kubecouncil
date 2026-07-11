"""Minimal fake-backed incident API used by the first incident-response slice."""

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field

from app.domain.incident_fakes import InMemoryIncidentStore
from app.domain.incidents import (
    AlertSignal,
    ApplicationProfileProvider,
    AuditEvent,
    EnrollmentProvider,
    EvidenceProvider,
    EvidenceRedactor,
    IncidentStore,
    InvestigationRecord,
    WorkloadReference,
)
from app.domain.models import KubeCouncilModel
from app.services.enrollment import EnrollmentChecker, require_enrolled_target
from app.services.evidence import InitialEvidenceWindowCollector

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


class OpenIncidentRequest(KubeCouncilModel):
    summary: str = Field(min_length=1, max_length=1000)
    application_id: str = Field(default="online-boutique", min_length=1)
    target: WorkloadReference = Field(
        default_factory=lambda: WorkloadReference(
            namespace="online-boutique", name="recommendationservice"
        )
    )


def get_incident_store(request: Request) -> IncidentStore:
    store = getattr(request.app.state, "incident_store", None)
    if not isinstance(store, InMemoryIncidentStore):
        raise RuntimeError(
            "incident store is unavailable; application lifespan did not initialize it"
        )
    return store


def get_application_profile_provider(request: Request) -> ApplicationProfileProvider:
    provider = getattr(request.app.state, "application_profile_provider", None)
    if not callable(getattr(provider, "list_profiles", None)):
        raise RuntimeError("application profile provider is unavailable")
    return cast(ApplicationProfileProvider, provider)


def get_enrollment_provider(request: Request) -> EnrollmentProvider:
    provider = getattr(request.app.state, "enrollment_provider", None)
    if not callable(getattr(provider, "inspect", None)):
        raise RuntimeError("enrollment provider is unavailable")
    return cast(EnrollmentProvider, provider)


def get_evidence_provider(request: Request) -> EvidenceProvider:
    provider = getattr(request.app.state, "evidence_provider", None)
    if not callable(getattr(provider, "collect_initial", None)):
        raise RuntimeError("evidence provider is unavailable")
    return cast(EvidenceProvider, provider)


def get_evidence_redactor(request: Request) -> EvidenceRedactor:
    redactor = getattr(request.app.state, "evidence_redactor", None)
    if not callable(getattr(redactor, "redact", None)):
        raise RuntimeError("evidence redactor is unavailable")
    return cast(EvidenceRedactor, redactor)


@router.post("", response_model=InvestigationRecord, status_code=status.HTTP_201_CREATED)
def open_incident(
    request: OpenIncidentRequest,
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    profiles: Annotated[ApplicationProfileProvider, Depends(get_application_profile_provider)],
    enrollment_provider: Annotated[EnrollmentProvider, Depends(get_enrollment_provider)],
    evidence_provider: Annotated[EvidenceProvider, Depends(get_evidence_provider)],
    evidence_redactor: Annotated[EvidenceRedactor, Depends(get_evidence_redactor)],
) -> InvestigationRecord:
    load_result = next(
        (
            result
            for result in profiles.list_profiles()
            if result.application_id == request.application_id
        ),
        None,
    )
    if load_result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "application_not_enrolled", "message": "application is not enrolled"},
        )
    if load_result.profile is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "profile_invalid",
                "message": "; ".join(error.message for error in load_result.errors),
            },
        )
    profile = load_result.profile
    readiness = EnrollmentChecker().check(profile, enrollment_provider.inspect(profile))
    try:
        require_enrolled_target(profile, readiness, request.target)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "enrollment_not_ready", "message": str(error)},
        ) from error
    signal = AlertSignal(
        signal_id=f"manual-{uuid4().hex}",
        application_id=profile.application_id,
        namespace=request.target.namespace,
        workload_name=request.target.name,
        workload_namespace=request.target.namespace,
        summary=request.summary,
        observed_at=datetime.now(UTC),
    )
    record = store.create(
        profile,
        signal,
    )
    record = store.append_audit_event(
        record.incident.incident_id,
        event=AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=record.incident.incident_id,
            event_type="incident_opened",
            occurred_at=datetime.now(UTC),
            actor="local-operator",
        ),
    )
    InitialEvidenceWindowCollector(evidence_provider, evidence_redactor).collect(
        store,
        incident_id=record.incident.incident_id,
        profile=profile,
        signal=signal,
        window=record.evidence_window,
    )
    completed_record = store.get(record.incident.incident_id)
    if completed_record is None:
        raise RuntimeError("incident disappeared while collecting its evidence window")
    return completed_record


@router.get("", response_model=tuple[InvestigationRecord, ...])
def list_incidents(
    store: Annotated[IncidentStore, Depends(get_incident_store)],
) -> tuple[InvestigationRecord, ...]:
    return store.list()


@router.get("/{incident_id}", response_model=InvestigationRecord)
def get_incident(
    incident_id: str,
    store: Annotated[IncidentStore, Depends(get_incident_store)],
) -> InvestigationRecord:
    record = store.get(incident_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "incident_not_found", "message": "incident does not exist"},
        )
    return record
