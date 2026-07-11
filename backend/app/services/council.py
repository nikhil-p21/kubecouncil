"""Bounded, auditable orchestration for the incident-response Council."""

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from pydantic import ValidationError

from app.domain.incidents import (
    AuditEvent,
    CoordinatorOutput,
    CoordinatorRequest,
    EvidenceObservation,
    EvidenceQueryKind,
    IncidentCouncilModel,
    IncidentLifecycle,
    IncidentStore,
    InvestigationOutcome,
    InvestigationRecord,
    ModelInvocation,
    ModelResponse,
    SpecialistFinding,
    SpecialistModelOutput,
    SpecialistRequest,
    SpecialistResult,
    SpecialistRole,
    SpecialistRunStatus,
    transition_incident,
)
from app.services.evidence_gateway import EvidenceGatewayError, EvidenceQueryGateway

_SPECIALIST_KINDS: dict[SpecialistRole, frozenset[EvidenceQueryKind]] = {
    SpecialistRole.HEALTH: frozenset(
        {EvidenceQueryKind.WORKLOAD_STATE, EvidenceQueryKind.POD_EVENTS}
    ),
    SpecialistRole.LOGS: frozenset({EvidenceQueryKind.POD_LOGS}),
    SpecialistRole.METRICS: frozenset(
        {EvidenceQueryKind.METRICS, EvidenceQueryKind.ALERT_POLICY}
    ),
    SpecialistRole.CHANGE: frozenset({EvidenceQueryKind.CHANGE_HISTORY}),
}


class CouncilError(RuntimeError):
    """Raised when an Incident cannot safely enter the Council workflow."""


class FakeIncidentCouncilModel:
    """Deterministic local model fake covering the two incident-response evaluations."""

    async def analyze_specialist(self, request: SpecialistRequest) -> ModelResponse:
        evidence = request.evidence[0]
        role_explanations = {
            SpecialistRole.HEALTH: "The workload is restarting and its rollout state changed.",
            SpecialistRole.LOGS: "Termination logs are consistent with an OOM failure.",
            SpecialistRole.METRICS: "Customer success and latency degraded with the incident.",
            SpecialistRole.CHANGE: "The latest revision changed the workload memory limit.",
        }
        contradictions = (
            "Instructions embedded in log content are untrusted evidence, not authority.",
        ) if request.role is SpecialistRole.LOGS else ()
        return ModelResponse(
            output={
                "finding": {
                    "finding_id": f"finding-{request.incident_id}-{request.role.value}",
                    "incident_id": request.incident_id,
                    "specialist": request.role.value,
                    "citations": [
                        {
                            "evidence_id": evidence.evidence_id,
                            "observation": evidence.redacted_excerpt[:1000],
                        }
                    ],
                    "candidate_explanations": [role_explanations[request.role]],
                    "confidence": 0.86,
                    "contradictions": list(contradictions),
                    "unknowns": ["The hypothesis remains falsifiable until recovery is verified."],
                },
                "evidence_query": None,
            },
            model_id="gemini-3.5-flash",
            prompt_version="incident-specialist-v1",
            thinking_level="medium",
            input_tokens=180,
            output_tokens=70,
        )

    async def coordinate(self, request: CoordinatorRequest) -> ModelResponse:
        findings = [result.finding for result in request.specialists if result.finding is not None]
        if len(findings) != len(SpecialistRole):
            output: dict[str, object] = {
                "outcome": InvestigationOutcome.INCONCLUSIVE.value,
                "hypotheses": [],
                "proposal": None,
                "manual_guidance": None,
            }
        elif "redis-cart" in request.incident_summary.lower():
            output = self._protected_dependency_output(request, findings)
        else:
            output = self._oom_output(request, findings)
        return ModelResponse(
            output=output,
            model_id="gemini-3.5-flash",
            prompt_version="incident-coordinator-v1",
            thinking_level="high",
            input_tokens=520,
            output_tokens=160,
        )

    @staticmethod
    def _oom_output(
        request: CoordinatorRequest, findings: list[SpecialistFinding]
    ) -> dict[str, object]:
        citations = [
            finding.citations[0].model_dump(mode="json") for finding in findings
        ]
        target = next(
            workload.reference
            for workload in request.application_profile.workloads
            if workload.reference.name == "recommendationservice"
        )
        return {
            "outcome": InvestigationOutcome.PROPOSAL_READY.value,
            "hypotheses": [
                {
                    "hypothesis_id": f"hypothesis-{request.incident_id}-oom",
                    "incident_id": request.incident_id,
                    "rank": 1,
                    "statement": (
                        "The revision 8 lower memory limit caused recommendationservice "
                        "OOM terminations and checkout degradation."
                    ),
                    "falsification_test": (
                        "Rollback to revision 7 and verify OOM cessation plus Critical Journey "
                        "recovery."
                    ),
                    "confidence": 0.92,
                    "citations": citations,
                }
            ],
            "proposal": {
                "proposal_id": f"proposal-{request.incident_id}-rollback",
                "incident_id": request.incident_id,
                "action": {
                    "action_type": "rollback_deployment",
                    "target": target.model_dump(mode="json"),
                    "revision": 7,
                },
                "expected_impact": "Restore the last known healthy memory configuration.",
                "recovery_criteria": request.application_profile.recovery_criteria.model_dump(
                    mode="json"
                ),
                "rollback_strategy": (
                    "Do not automatically restore the implicated revision; enter Safe Halt if "
                    "recovery is ambiguous."
                ),
                "evidence_hash": request.evidence_hash,
                "known_risks": [
                    "A rollback starts a new rollout and may temporarily reduce available capacity."
                ],
            },
            "manual_guidance": None,
        }

    @staticmethod
    def _protected_dependency_output(
        request: CoordinatorRequest, findings: list[SpecialistFinding]
    ) -> dict[str, object]:
        citations = [
            finding.citations[0].model_dump(mode="json") for finding in findings
        ]
        return {
            "outcome": InvestigationOutcome.NO_SAFE_ACTION.value,
            "hypotheses": [
                {
                    "hypothesis_id": f"hypothesis-{request.incident_id}-redis",
                    "incident_id": request.incident_id,
                    "rank": 1,
                    "statement": "redis-cart unavailability is disrupting the checkout path.",
                    "falsification_test": (
                        "Restore redis-cart outside KubeCouncil and verify checkout recovery."
                    ),
                    "confidence": 0.9,
                    "citations": citations,
                }
            ],
            "proposal": None,
            "manual_guidance": {
                "incident_id": request.incident_id,
                "reason": "redis-cart is a Protected Dependency outside execution authority.",
                "guidance": (
                    "Escalate to the dependency owner and restore redis-cart through its "
                    "approved operational procedure."
                ),
                "outcome": InvestigationOutcome.NO_SAFE_ACTION.value,
            },
        }


class BoundedIncidentCouncil:
    """Runs four isolated Specialist roles concurrently and then one Coordinator."""

    def __init__(
        self,
        model: IncidentCouncilModel,
        *,
        specialist_timeout_seconds: float = 30,
        coordinator_timeout_seconds: float = 30,
        model_id: str = "gemini-3.5-flash",
        evidence_gateway: EvidenceQueryGateway | None = None,
    ) -> None:
        self._model = model
        self._specialist_timeout_seconds = specialist_timeout_seconds
        self._coordinator_timeout_seconds = coordinator_timeout_seconds
        self._model_id = model_id
        self._evidence_gateway = evidence_gateway

    async def investigate(
        self, store: IncidentStore, incident_id: str
    ) -> InvestigationRecord:
        record = store.get(incident_id)
        if record is None:
            raise CouncilError("incident does not exist")
        if record.incident.investigation_outcome is not InvestigationOutcome.NOT_STARTED:
            raise CouncilError("incident investigation has already completed")
        if not record.evidence:
            raise CouncilError("incident has no redacted evidence to investigate")

        investigating = transition_incident(
            record.incident, lifecycle=IncidentLifecycle.INVESTIGATING
        )
        store.compare_and_set(incident_id, record.incident.version, investigating)
        self._audit(store, incident_id, "investigation_started")

        tasks = [
            asyncio.create_task(self._run_specialist(store, incident_id, role))
            for role in SpecialistRole
        ]
        specialist_results = tuple(await asyncio.gather(*tasks))
        current = self._required_record(store, incident_id)
        coordinator_request = CoordinatorRequest(
            incident_id=incident_id,
            incident_summary=current.incident.summary,
            application_profile=current.application_profile,
            evidence_hash=_evidence_hash(current.evidence),
            specialists=specialist_results,
        )
        output = await self._run_coordinator(store, coordinator_request)
        completed = store.complete_investigation(incident_id, output)
        self._audit(
            store,
            incident_id,
            "investigation_completed",
            details={"outcome": completed.incident.investigation_outcome.value},
        )
        return self._required_record(store, incident_id)

    async def _run_specialist(
        self,
        store: IncidentStore,
        incident_id: str,
        role: SpecialistRole,
    ) -> SpecialistResult:
        self._audit(store, incident_id, "specialist_started", details={"specialist": role.value})
        record = self._required_record(store, incident_id)
        evidence = _role_evidence(record.evidence, role)
        if not evidence:
            reason = "no role-relevant redacted evidence was available"
            self._audit(
                store,
                incident_id,
                "specialist_failed",
                details={"specialist": role.value, "reason": reason},
            )
            return SpecialistResult(
                role=role, status=SpecialistRunStatus.FAILED, failure_reason=reason
            )

        allowed_mapping_identifiers = tuple(
            mapping.identifier
            for mapping in record.application_profile.evidence_mappings
            if mapping.kind in _SPECIALIST_KINDS[role]
        )
        request = SpecialistRequest(
            incident_id=incident_id,
            role=role,
            evidence=evidence,
            allowed_mapping_identifiers=allowed_mapping_identifiers,
            completed_query_rounds=0,
        )
        started = monotonic()
        response: ModelResponse | None = None
        try:
            async with asyncio.timeout(self._specialist_timeout_seconds):
                while True:
                    started = monotonic()
                    response = await self._model.analyze_specialist(request)
                    output = SpecialistModelOutput.model_validate(response.output)
                    if output.finding is not None:
                        if (
                            output.finding.incident_id != incident_id
                            or output.finding.specialist is not role
                        ):
                            raise ValueError(
                                "Specialist Finding does not match its assigned Incident and role"
                            )
                        break

                    query = output.evidence_query
                    if query is None:
                        raise ValueError("Specialist output contains no supported next step")
                    if (
                        self._evidence_gateway is None
                        or request.completed_query_rounds >= 2
                        or query.mapping_identifier not in allowed_mapping_identifiers
                    ):
                        store.append_model_invocation(
                            incident_id,
                            _invocation(
                                incident_id,
                                role,
                                response,
                                latency_ms=_latency_ms(started),
                                output_valid=True,
                                tool_count=0,
                            ),
                        )
                        return self._specialist_failure_without_invocation(
                            store,
                            incident_id,
                            role,
                            "Specialist exceeded or violated its Evidence Query boundary",
                        )
                    try:
                        self._evidence_gateway.execute(
                            store,
                            incident_id=incident_id,
                            specialist=role,
                            mapping_identifier=query.mapping_identifier,
                            query_round=request.completed_query_rounds + 1,
                        )
                    except EvidenceGatewayError:
                        store.append_model_invocation(
                            incident_id,
                            _invocation(
                                incident_id,
                                role,
                                response,
                                latency_ms=_latency_ms(started),
                                output_valid=True,
                                tool_count=0,
                            ),
                        )
                        return self._specialist_failure_without_invocation(
                            store,
                            incident_id,
                            role,
                            "Specialist Evidence Query was rejected by the deterministic gateway",
                        )
                    store.append_model_invocation(
                        incident_id,
                        _invocation(
                            incident_id,
                            role,
                            response,
                            latency_ms=_latency_ms(started),
                            output_valid=True,
                            tool_count=1,
                        ),
                    )
                    completed_rounds = request.completed_query_rounds + 1
                    current = self._required_record(store, incident_id)
                    request = request.model_copy(
                        update={
                            "evidence": _role_evidence(current.evidence, role),
                            "completed_query_rounds": completed_rounds,
                        }
                    )
        except TimeoutError:
            return self._record_specialist_failure(
                store,
                incident_id,
                role,
                SpecialistRunStatus.TIMED_OUT,
                "Specialist deadline exceeded",
                started,
                response,
            )
        except (ValidationError, ValueError):
            return self._record_specialist_failure(
                store,
                incident_id,
                role,
                SpecialistRunStatus.FAILED,
                "Specialist returned malformed structured output",
                started,
                response,
            )
        except Exception:
            return self._record_specialist_failure(
                store,
                incident_id,
                role,
                SpecialistRunStatus.FAILED,
                "Specialist model invocation failed",
                started,
                response,
            )

        invocation = _invocation(
            incident_id,
            role,
            response,
            latency_ms=_latency_ms(started),
            output_valid=True,
            tool_count=0,
        )
        store.append_model_invocation(incident_id, invocation)
        store.append_finding(incident_id, output.finding)
        self._audit(
            store,
            incident_id,
            "specialist_completed",
            details={
                "specialist": role.value,
                "finding_id": output.finding.finding_id,
                "confidence": str(output.finding.confidence),
            },
        )
        return SpecialistResult(
            role=role, status=SpecialistRunStatus.SUCCEEDED, finding=output.finding
        )

    def _specialist_failure_without_invocation(
        self,
        store: IncidentStore,
        incident_id: str,
        role: SpecialistRole,
        reason: str,
    ) -> SpecialistResult:
        self._audit(
            store,
            incident_id,
            "specialist_failed",
            details={"specialist": role.value, "reason": reason},
        )
        return SpecialistResult(
            role=role, status=SpecialistRunStatus.FAILED, failure_reason=reason
        )

    def _record_specialist_failure(
        self,
        store: IncidentStore,
        incident_id: str,
        role: SpecialistRole,
        status: SpecialistRunStatus,
        reason: str,
        started: float,
        response: ModelResponse | None,
    ) -> SpecialistResult:
        store.append_model_invocation(
            incident_id,
            _invocation(
                incident_id,
                role,
                response,
                latency_ms=_latency_ms(started),
                output_valid=False,
                failure_reason=reason,
                fallback_model_id=self._model_id,
                tool_count=0,
            ),
        )
        self._audit(
            store,
            incident_id,
            "specialist_timed_out"
            if status is SpecialistRunStatus.TIMED_OUT
            else "specialist_failed",
            details={"specialist": role.value, "reason": reason},
        )
        return SpecialistResult(role=role, status=status, failure_reason=reason)

    async def _run_coordinator(
        self,
        store: IncidentStore,
        request: CoordinatorRequest,
    ) -> CoordinatorOutput:
        self._audit(store, request.incident_id, "coordinator_started")
        started = monotonic()
        response: ModelResponse | None = None
        try:
            async with asyncio.timeout(self._coordinator_timeout_seconds):
                response = await self._model.coordinate(request)
            output = CoordinatorOutput.model_validate(response.output)
            _validate_coordinator_scope(output, request.incident_id)
        except TimeoutError:
            reason = "Coordinator deadline exceeded"
        except (ValidationError, ValueError):
            reason = "Coordinator returned malformed structured output"
        except Exception:
            reason = "Coordinator model invocation failed"
        else:
            store.append_model_invocation(
                request.incident_id,
                _invocation(
                    request.incident_id,
                    "coordinator",
                    response,
                    latency_ms=_latency_ms(started),
                    output_valid=True,
                    tool_count=0,
                ),
            )
            self._audit(
                store,
                request.incident_id,
                "coordinator_completed",
                details={"outcome": output.outcome.value},
            )
            return output

        store.append_model_invocation(
            request.incident_id,
            _invocation(
                request.incident_id,
                "coordinator",
                response,
                latency_ms=_latency_ms(started),
                output_valid=False,
                failure_reason=reason,
                fallback_model_id=self._model_id,
                tool_count=0,
            ),
        )
        self._audit(
            store,
            request.incident_id,
            "coordinator_failed",
            details={"reason": reason},
        )
        return CoordinatorOutput(outcome=InvestigationOutcome.INCONCLUSIVE)

    @staticmethod
    def _audit(
        store: IncidentStore,
        incident_id: str,
        event_type: str,
        *,
        details: dict[str, str] | None = None,
    ) -> None:
        store.append_audit_event(
            incident_id,
            AuditEvent(
                event_id=f"audit-{uuid4().hex}",
                incident_id=incident_id,
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                actor="incident-council",
                details=details or {},
            ),
        )

    @staticmethod
    def _required_record(store: IncidentStore, incident_id: str) -> InvestigationRecord:
        record = store.get(incident_id)
        if record is None:
            raise CouncilError("incident disappeared during Council execution")
        return record


def _role_evidence(
    evidence: tuple[EvidenceObservation, ...], role: SpecialistRole
) -> tuple[EvidenceObservation, ...]:
    return tuple(item for item in evidence if item.query in _SPECIALIST_KINDS[role])


def _evidence_hash(evidence: tuple[EvidenceObservation, ...]) -> str:
    material = "\n".join(sorted(item.content_hash for item in evidence))
    return sha256(material.encode()).hexdigest()


def _latency_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1000))


def _invocation(
    incident_id: str,
    role: SpecialistRole | str,
    response: ModelResponse | None,
    *,
    latency_ms: int,
    output_valid: bool,
    tool_count: int,
    failure_reason: str | None = None,
    fallback_model_id: str = "gemini-3.5-flash",
) -> ModelInvocation:
    return ModelInvocation(
        invocation_id=f"model-{uuid4().hex}",
        incident_id=incident_id,
        role=role,
        model_id=response.model_id if response is not None else fallback_model_id,
        prompt_version=response.prompt_version if response is not None else f"{role}-v1",
        thinking_level=response.thinking_level if response is not None else "medium",
        latency_ms=latency_ms,
        input_tokens=response.input_tokens if response is not None else 0,
        output_tokens=response.output_tokens if response is not None else 0,
        tool_count=tool_count,
        output_valid=output_valid,
        failure_reason=failure_reason,
    )


def _validate_coordinator_scope(output: CoordinatorOutput, incident_id: str) -> None:
    if any(hypothesis.incident_id != incident_id for hypothesis in output.hypotheses):
        raise ValueError("Coordinator hypothesis belongs to another Incident")
    if output.proposal is not None and output.proposal.incident_id != incident_id:
        raise ValueError("Coordinator proposal belongs to another Incident")
    if output.manual_guidance is not None and output.manual_guidance.incident_id != incident_id:
        raise ValueError("Coordinator Manual Guidance belongs to another Incident")
