# 18 — Bounded parallel Council

---

id: KC-18
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-17]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Turn the Evidence Window into one auditable investigation outcome. Four bounded Specialists run concurrently, may request narrowly scoped follow-up evidence, and return validated findings before the Incident Coordinator ranks Root Cause Hypotheses or safely declines action.

## Blocked by

- KC-17 — Live read-only evidence gateway

## Acceptance criteria

- [x] Health, Logs, Metrics, and Change Specialists run concurrently with independent deadlines, failures, and no more than two Evidence Query rounds each.
- [x] Specialists receive role-relevant redacted evidence and no credentials, write tools, shell, raw kubectl, or provider tool catalogs.
- [x] Every Specialist Finding validates through Pydantic and contains cited observations, candidate explanations, confidence, contradictions, and unknowns.
- [x] The Coordinator runs after every Specialist succeeds, fails, or times out and produces ranked falsifiable hypotheses plus exactly one supported outcome.
- [x] Malformed output, missing Specialists, contradictions, and prompt-injected evidence remain explicit and cannot bypass deterministic policy.
- [x] Model ID, prompt version, thinking level, token usage, latency, tool count, validation, and failure metadata are audited.
- [x] Incident Detail streams Specialist progress, findings, disagreements, failures, hypotheses, and the consolidated outcome.
- [x] Evaluation fixtures cover the OOM diagnosis, Protected Dependency refusal, partial failure, malformed output, query limits, and prompt injection.

## Implementation summary

Implemented the bounded incident-response Council through the public Investigator/API seam:

* added strict provider-independent Specialist request, follow-up query, finding, model metadata, result, Coordinator input, hypothesis, and consolidated outcome contracts;
* ran Health, Logs, Metrics, and Change Specialists concurrently with independent deadlines and explicit failure or timeout results;
* limited follow-up observations to two profile-owned Evidence Query mappings per Specialist through the existing server-scoped gateway;
* persisted append-only findings, model invocations, audit progress, hypotheses, proposals, and Safe Refusal guidance through both in-memory and Firestore-shaped stores;
* added a deterministic local Council fake for the recommendation OOM rollback and Protected `redis-cart` Safe Refusal evaluations;
* exposed the Council command through the Incident API and displayed live progress, findings, disagreements, failures, ranked hypotheses, and the consolidated outcome in Incident Detail.

## Files changed

* `.scratch/issues/18-bounded-parallel-council.md`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/main.py`
* `backend/app/services/council.py`
* `backend/app/services/incident_store.py`
* `backend/tests/test_incident_council.py`
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && python -m pytest tests/test_incident_council.py -q` — 9 passed.
* `cd backend && python -m pytest tests/test_incident_council.py tests/test_incidents.py tests/test_durable_incidents.py tests/test_evidence_gateway.py -q` — 45 passed.
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd frontend && npm test -- --run` — 5 passed.
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify` — 122 backend tests passed, 2 integration tests skipped; 5 frontend tests passed; backend lint and typing plus frontend lint and production build passed.
