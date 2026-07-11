# 16 — Durable alerts, correlation, and timeline replay

---

id: KC-16
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-15]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Make Incidents durable and alert-driven. Cloud Monitoring notifications and manual requests use one normalization path, related signals update the correct Investigation Record, and reconnecting operators can replay every append-only timeline event without losing progress.

## Blocked by

- KC-15 — Manual Incident and deterministic Evidence Window

## Acceptance criteria

- [x] The IncidentStore has behaviorally equivalent in-memory and Firestore implementations with domain contracts independent of Firestore SDK objects.
- [x] A Pub/Sub pull consumer validates, Enrollment-checks, deduplicates, and correlates Alert Signals within the configured application or dependency window.
- [x] Pub/Sub messages are acknowledged only after durable Incident creation or update, and retries remain idempotent.
- [x] Provider-side alert closure is appended as evidence and cannot resolve an Incident by itself.
- [x] Evidence and audit events are append-only, while lifecycle changes and claims use transactional compare-and-set behavior.
- [x] Server-Sent Events replay from a durable cursor and the UI reconnects without duplicating or skipping timeline entries.
- [x] Local tests use fakes; live Firestore and Pub/Sub tests are explicitly marked integration.

## Implementation summary

Implemented durable, alert-driven Incident correlation and replay:

* added behaviorally equivalent in-memory and transactional Firestore Incident stores behind provider-independent domain contracts;
* normalized manual and Pub/Sub alert inputs, enforced Enrollment before persistence, correlated provider and dependency-path signals, and made delivery retries idempotent;
* retained provider open and closure notifications as append-only evidence without granting provider closure lifecycle authority;
* assigned durable monotonic cursors to append-only audit events and exposed polling and reconnectable Server-Sent Event replay APIs;
* connected the Incident UI to cursor-aware EventSource replay with duplicate suppression;
* added transactional fakes and focused backend and UI coverage without requiring live GCP services.

## Files changed

* `.scratch/issues/16-durable-alerts-correlation-and-timeline-replay.md`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/services/alerts.py`
* `backend/app/services/incident_store.py`
* `backend/pyproject.toml`
* `backend/tests/test_durable_incidents.py`
* `backend/tests/integration/test_durable_gcp_adapters.py`
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && python -m pytest tests/test_durable_incidents.py tests/test_incidents.py -q`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd frontend && npm test -- --run`
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify` — 103 backend tests and 4 frontend tests passed; backend lint and typing, frontend lint, and production build passed.
