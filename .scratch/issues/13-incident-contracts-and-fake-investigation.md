# 13 — Incident contracts and fake investigation path

---

id: KC-13
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: []

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Create the typed incident-response foundation and a minimal fake-backed investigation path. A local operator can open and inspect an Incident through the API and UI, while every Application Profile, Incident, evidence, finding, proposal, policy, Approval, Intervention, recovery, and audit value is represented by strict domain contracts independent of external SDKs.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [x] All Slice 1 contracts from the incident-response requirements serialize and round-trip without leaking FastAPI, Kubernetes, Google, Firestore, or ADK objects.
- [x] Malformed, unsupported, cross-scope, and stale contract values are rejected with descriptive errors.
- [x] Incident lifecycle, investigation outcome, and intervention outcome are modeled independently with tested transition rules.
- [x] In-memory implementations support incidents, immutable evidence, append-only audit events, and compare-and-set state changes.
- [x] A minimal API and UI path can create and display a fake Incident and its independent status dimensions.
- [x] Existing rehearsal behavior remains isolated from the new incident contracts and no compatibility layer is introduced.

## Implementation summary

Implemented the Slice 1 incident-response foundation:

* strict, SDK-independent Pydantic contracts for application enrollment, incidents, immutable evidence windows, evidence, specialist findings, model invocations, hypotheses, remediation, policy, approval, intervention, recovery, and audit records;
* independent incident lifecycle, investigation outcome, and intervention outcome transitions, with cross-scope and relational record validation;
* an in-memory IncidentStore fake with append-only evidence/audit records and compare-and-set state updates;
* fake-backed incident API endpoints to create, list, and inspect incidents;
* a focused incident UI that opens and displays the local fake incident, its independent statuses, enrollment boundary, and audit timeline.
* removal of the legacy rehearsal routers from the active application and removal of their obsolete API tests.

## Files changed

* `.scratch/issues/13-incident-contracts-and-fake-investigation.md`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/main.py`
* `backend/tests/test_incidents.py`
* `backend/tests/test_kustomize_analysis.py` (removed)
* `backend/tests/test_plan_execution.py` (removed)
* `backend/tests/test_pull_requests.py` (removed)
* `backend/tests/test_rehearsal.py` (removed)
* `backend/tests/test_repository_api.py` (removed)
* `backend/tests/test_scenarios.py` (removed)
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && python -m pytest tests/test_incidents.py`
* `cd backend && python -m ruff check app/domain/incidents.py app/domain/incident_fakes.py app/api/incidents.py tests/test_incidents.py`
* `cd backend && python -m mypy app`
* `cd frontend && npm test`
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify` — 77 passed; backend lint and typing passed; frontend tests, lint, and build passed.
