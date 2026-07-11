"""Minimal fake-backed incident API used by the first incident-response slice."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field

from app.domain.incident_fakes import InMemoryIncidentStore, fake_application_profile
from app.domain.incidents import AlertSignal, AuditEvent, IncidentStore, InvestigationRecord
from app.domain.models import KubeCouncilModel

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


class OpenIncidentRequest(KubeCouncilModel):
    summary: str = Field(min_length=1, max_length=1000)


def get_incident_store(request: Request) -> IncidentStore:
    store = getattr(request.app.state, "incident_store", None)
    if not isinstance(store, InMemoryIncidentStore):
        raise RuntimeError(
            "incident store is unavailable; application lifespan did not initialize it"
        )
    return store


@router.post("", response_model=InvestigationRecord, status_code=status.HTTP_201_CREATED)
def open_incident(
    request: OpenIncidentRequest,
    store: Annotated[IncidentStore, Depends(get_incident_store)],
) -> InvestigationRecord:
    profile = fake_application_profile()
    record = store.create(
        profile,
        AlertSignal(
            signal_id=f"manual-{uuid4().hex}",
            application_id=profile.application_id,
            namespace=profile.namespace,
            workload_name="recommendationservice",
            workload_namespace=profile.namespace,
            summary=request.summary,
            observed_at=datetime.now(UTC),
        ),
    )
    return store.append_audit_event(
        record.incident.incident_id,
        event=AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=record.incident.incident_id,
            event_type="incident_opened",
            occurred_at=datetime.now(UTC),
            actor="local-operator",
        ),
    )


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
