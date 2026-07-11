# 15 — Manual Incident and deterministic Evidence Window

---

id: KC-15
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-14]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Allow a Responder to open a manual Incident for an enrolled Managed Application and receive a deterministic, immutable initial Evidence Window. Evidence comes through bounded fake providers, is redacted before persistence or display, and remains traceable to its source in Incident Detail.

## Blocked by

- KC-14 — Application Profile and Enrollment readiness

## Acceptance criteria

- [x] Manual requests normalize into Alert Signals and reject applications, namespaces, or workloads outside Enrollment.
- [x] Initial evidence includes bounded workload health, events, logs, metrics, rollout state, and change observations appropriate to the Application Profile.
- [x] Every observation records source, query, timestamp, content hash, scope, and truncation metadata.
- [x] Kubernetes Secrets and container environment values cannot be requested or returned.
- [x] Redaction runs before model, persistence, and UI access; redaction failure closes retrieval without exposing its result.
- [x] Incident Detail shows the immutable Evidence Window, provenance, truncation, and explicit retrieval failures.
- [x] Tests cover evidence budgets, scope escape, prompt-injected logs, secret exclusion, truncation, and fail-closed redaction.

## Implementation summary

Implemented the manual Incident Evidence Window workflow:

* normalized manual requests into scoped `AlertSignal` values and created profile-budgeted immutable windows;
* introduced typed raw evidence, redacted evidence, and append-only retrieval-failure contracts behind injectable provider and redactor interfaces;
* added bounded fake health, event, log, metric, rollout, and change evidence, including deterministic secret redaction and fail-closed redaction handling;
* displayed evidence provenance, time window, truncation, and safe retrieval failures in Incident Detail;
* added API, contract, and UI coverage for scope escape, Secret/environment exclusion, prompt-injected log text, truncation, and redaction failure.

## Files changed

* `.scratch/issues/15-manual-incident-and-evidence-window.md`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/main.py`
* `backend/app/services/evidence.py`
* `backend/tests/test_incidents.py`
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && python -m pytest tests/test_incidents.py`
* `cd backend && python -m pytest`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd frontend && npm test -- --run`
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify`
