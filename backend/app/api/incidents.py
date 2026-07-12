"""Incident commands and replay APIs backed by the selected explicit runtime profile."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import Field

from app.api.identity import get_current_identity, require_responder
from app.domain.identity import OperatorIdentity
from app.domain.incidents import (
    AlertSignalEvidence,
    ApplicationProfileProvider,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalReview,
    AuditEvent,
    EnrollmentProvider,
    EvidenceProvider,
    EvidenceRedactor,
    IncidentLifecycle,
    IncidentStore,
    InvestigationRecord,
    SpecialistRole,
    WorkloadReference,
    transition_incident,
)
from app.domain.models import KubeCouncilModel
from app.runtime.readiness import ReadinessRegistry
from app.services.alerts import AlertNormalizer, AlertNotification
from app.services.approval import ApprovalError, ApprovalService
from app.services.council import BoundedIncidentCouncil, CouncilError
from app.services.enrollment import EnrollmentChecker, require_enrolled_target
from app.services.evidence import InitialEvidenceWindowCollector
from app.services.evidence_gateway import EvidenceGatewayError, EvidenceQueryGateway
from app.services.proposal_policy import DeterministicProposalPolicy, ProposalPolicyError

router = APIRouter(
    prefix="/api/incidents",
    tags=["incidents"],
    dependencies=[Depends(get_current_identity)],
)


class OpenIncidentRequest(KubeCouncilModel):
    summary: str = Field(min_length=1, max_length=1000)
    application_id: str = Field(default="online-boutique", min_length=1)
    target: WorkloadReference = Field(
        default_factory=lambda: WorkloadReference(
            namespace="online-boutique", name="recommendationservice"
        )
    )


class RunEvidenceQueryRequest(KubeCouncilModel):
    """A specialist may select only a profile-owned mapping, never raw provider scope."""

    specialist: SpecialistRole
    mapping_identifier: str = Field(min_length=1, max_length=500)
    query_round: int = Field(ge=1, le=2)


class DecideProposalRequest(KubeCouncilModel):
    decision: ApprovalDecision
    reviewed_binding: ApprovalBinding


class CloseIncidentRequest(KubeCouncilModel):
    expected_version: int = Field(ge=0)


def get_incident_store(request: Request) -> IncidentStore:
    store = getattr(request.app.state, "incident_store", None)
    if not all(
        callable(getattr(store, method, None))
        for method in (
            "create",
            "get",
            "list",
            "append_alert_signal",
            "append_evidence",
            "append_evidence_query",
            "append_finding",
            "append_model_invocation",
            "complete_investigation",
            "record_policy_decision",
            "record_approval_decision",
            "record_recovery_assessment",
            "append_evidence_retrieval_failure",
            "append_audit_event",
            "timeline",
        )
    ):
        raise RuntimeError(
            "incident store is unavailable; application lifespan did not initialize it"
        )
    return cast(IncidentStore, store)


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


def get_evidence_query_gateway(request: Request) -> EvidenceQueryGateway:
    gateway = getattr(request.app.state, "evidence_query_gateway", None)
    if not callable(getattr(gateway, "execute", None)):
        raise RuntimeError("evidence query gateway is unavailable")
    return cast(EvidenceQueryGateway, gateway)


def get_incident_council(request: Request) -> BoundedIncidentCouncil:
    council = getattr(request.app.state, "incident_council", None)
    if not callable(getattr(council, "investigate", None)):
        raise RuntimeError("incident Council is unavailable")
    return cast(BoundedIncidentCouncil, council)


def get_proposal_policy(request: Request) -> DeterministicProposalPolicy:
    policy = getattr(request.app.state, "proposal_policy", None)
    if not callable(getattr(policy, "evaluate_and_record", None)):
        raise RuntimeError("deterministic proposal policy is unavailable")
    return cast(DeterministicProposalPolicy, policy)


def get_approval_service(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    if not all(
        callable(getattr(service, method, None)) for method in ("review", "decide")
    ):
        raise RuntimeError("Approval service is unavailable")
    return cast(ApprovalService, service)


def require_intervention_readiness(request: Request) -> None:
    if getattr(request.app.state, "runtime_mode", "test") != "deployed":
        return
    registry = getattr(request.app.state, "readiness", None)
    if not isinstance(registry, ReadinessRegistry) or not registry.approval_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "intervention_not_ready",
                "message": (
                    "Approval is disabled until identity, evidence, Council, persistence, "
                    "and every intervention enforcement layer are ready"
                ),
            },
        )


@router.post("", response_model=InvestigationRecord, status_code=status.HTTP_201_CREATED)
def open_incident(
    request: OpenIncidentRequest,
    identity: Annotated[OperatorIdentity, Depends(require_responder)],
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
    signal = AlertNormalizer.normalize(
        AlertNotification(
            notification_id=f"manual-{uuid4().hex}",
            application_id=profile.application_id,
            namespace=request.target.namespace,
            workload_name=request.target.name,
            summary=request.summary,
            observed_at=datetime.now(UTC),
            provider_incident_id=None,
        )
    )
    record = store.create(
        profile,
        signal,
    )
    record = store.append_alert_signal(
        record.incident.incident_id,
        AlertSignalEvidence(
            notification_id=signal.signal_id,
            incident_id=record.incident.incident_id,
            signal=signal,
            provider_state="open",
            received_at=datetime.now(UTC),
        ),
    )
    record = store.append_audit_event(
        record.incident.incident_id,
        event=AuditEvent(
            event_id=f"audit-{uuid4().hex}",
            incident_id=record.incident.incident_id,
            event_type="incident_opened",
            occurred_at=datetime.now(UTC),
            actor=identity.principal,
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


@router.post("/{incident_id}/investigate", response_model=InvestigationRecord)
async def investigate_incident(
    incident_id: str,
    identity: Annotated[OperatorIdentity, Depends(require_responder)],
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    council: Annotated[BoundedIncidentCouncil, Depends(get_incident_council)],
    policy: Annotated[DeterministicProposalPolicy, Depends(get_proposal_policy)],
) -> InvestigationRecord:
    try:
        result = await council.investigate(store, incident_id)
        if result.proposal is None:
            return result
        return policy.evaluate_and_record(store, incident_id)
    except CouncilError as error:
        missing = str(error) == "incident does not exist"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if missing else status.HTTP_409_CONFLICT,
            detail={"code": "investigation_rejected", "message": str(error)},
        ) from error
    except ProposalPolicyError as error:
        missing = str(error) == "incident does not exist"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if missing else status.HTTP_409_CONFLICT,
            detail={"code": "proposal_policy_rejected", "message": str(error)},
        ) from error


@router.get("/{incident_id}/approval-review", response_model=ApprovalReview)
def review_proposal(
    incident_id: str,
    _: Annotated[None, Depends(require_intervention_readiness)],
    identity: Annotated[OperatorIdentity, Depends(require_responder)],
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalReview:
    try:
        return service.review(store, incident_id, identity)
    except ApprovalError as error:
        missing = str(error) == "incident does not exist"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if missing else status.HTTP_409_CONFLICT,
            detail={"code": "approval_review_rejected", "message": str(error)},
        ) from error


@router.post("/{incident_id}/approval-decisions", response_model=InvestigationRecord)
def decide_proposal(
    incident_id: str,
    request: DecideProposalRequest,
    _: Annotated[None, Depends(require_intervention_readiness)],
    identity: Annotated[OperatorIdentity, Depends(require_responder)],
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> InvestigationRecord:
    try:
        return service.decide(
            store,
            incident_id=incident_id,
            identity=identity,
            decision=request.decision,
            reviewed_binding=request.reviewed_binding,
        )
    except ApprovalError as error:
        missing = str(error) == "incident does not exist"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if missing else status.HTTP_409_CONFLICT,
            detail={"code": "approval_rejected", "message": str(error)},
        ) from error


@router.post("/{incident_id}/close", response_model=InvestigationRecord)
def close_incident(
    incident_id: str,
    request: CloseIncidentRequest,
    identity: Annotated[OperatorIdentity, Depends(require_responder)],
    store: Annotated[IncidentStore, Depends(get_incident_store)],
) -> InvestigationRecord:
    record = store.get(incident_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "incident_not_found", "message": "incident does not exist"},
        )
    try:
        replacement = transition_incident(
            record.incident, lifecycle=IncidentLifecycle.CLOSED
        )
        store.compare_and_set(incident_id, request.expected_version, replacement)
        return store.append_audit_event(
            incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=incident_id,
                event_type="incident_closed",
                occurred_at=datetime.now(UTC),
                actor=identity.principal,
                details={"subject": identity.subject},
            ),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "incident_close_rejected", "message": str(error)},
        ) from error


@router.post(
    "/{incident_id}/evidence-queries",
    response_model=InvestigationRecord,
    status_code=status.HTTP_201_CREATED,
)
def run_evidence_query(
    incident_id: str,
    request: RunEvidenceQueryRequest,
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    gateway: Annotated[EvidenceQueryGateway, Depends(get_evidence_query_gateway)],
) -> InvestigationRecord:
    try:
        return gateway.execute(
            store,
            incident_id=incident_id,
            specialist=request.specialist,
            mapping_identifier=request.mapping_identifier,
            query_round=request.query_round,
        )
    except EvidenceGatewayError as error:
        missing = str(error) == "incident does not exist"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if missing else status.HTTP_409_CONFLICT,
            detail={"code": "evidence_query_rejected", "message": str(error)},
        ) from error


@router.get("/{incident_id}/timeline", response_model=tuple[AuditEvent, ...])
def replay_timeline(
    incident_id: str,
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    after: Annotated[int, Query(ge=0)] = 0,
) -> tuple[AuditEvent, ...]:
    try:
        return store.timeline(incident_id, after=after)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "incident_not_found", "message": str(error)},
        ) from error


@router.get("/{incident_id}/events")
def stream_timeline(
    incident_id: str,
    request: Request,
    store: Annotated[IncidentStore, Depends(get_incident_store)],
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Replay durable events and continue from the last acknowledged EventSource cursor."""

    try:
        header_cursor = int(last_event_id) if last_event_id is not None else 0
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_timeline_cursor", "message": "Last-Event-ID must be numeric"},
        ) from error
    cursor = max(after, header_cursor)
    try:
        store.timeline(incident_id, after=cursor)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "incident_not_found", "message": str(error)},
        ) from error

    async def encode() -> AsyncIterator[str]:
        current_cursor = cursor
        while not await request.is_disconnected():
            events = store.timeline(incident_id, after=current_cursor)
            for event in events:
                current_cursor = event.cursor
                yield (
                    f"id: {event.cursor}\n"
                    "event: timeline\n"
                    f"data: {json.dumps(event.model_dump(mode='json'))}\n\n"
                )
            if not events:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        encode(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
