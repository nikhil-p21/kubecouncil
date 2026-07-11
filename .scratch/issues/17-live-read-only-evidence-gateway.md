# 17 — Live read-only evidence gateway

---

id: KC-17
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-15, KC-16]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Collect real incident evidence without giving Specialists broad provider access. The Evidence Query gateway translates typed, Incident-scoped requests into read-only Kubernetes, Cloud Logging, and Cloud Monitoring operations and appends safe, traceable results to the Investigation Record.

## Blocked by

- KC-15 — Manual Incident and deterministic Evidence Window
- KC-16 — Durable alerts, correlation, and timeline replay

## Acceptance criteria

- [x] A purpose-built Kubernetes adapter reads only allowed enrolled resources, pods, events, Deployment and ReplicaSet state, rollout state, and permitted pod logs.
- [x] Cloud Logging and Cloud Monitoring adapters expose only the approved bounded evidence operations behind domain interfaces.
- [x] Server-side scope is derived from the Incident and Application Profile rather than trusted from model arguments.
- [x] Namespace, workload, pod, metric, resource, time, result-size, and query-round budgets are enforced consistently across providers.
- [x] Provider deadlines, safe retries, truncation, provenance, redaction, and audit identifiers are preserved in every result.
- [x] Incident Detail links to relevant Cloud observability views and does not recreate full provider dashboards.
- [x] Adapter tests prove scope escape, Secret access, broad discovery, and unsupported provider operations fail closed.

## Implementation summary

Implemented an Incident-scoped, read-only Evidence Query gateway:

* added a strict API contract that accepts only a profile-owned evidence mapping identifier, Specialist role, and bounded query round;
* resolved namespace, workload, provider operation, metric template, immutable time window, result limits, and deadline server-side from the Investigation Record and Application Profile;
* added narrow Kubernetes, Cloud Logging, and Cloud Monitoring adapters whose interfaces cannot express Secret access, arbitrary discovery, writes, or unsupported provider operations;
* enforced per-Specialist query budgets, bounded safe retries, provider deadlines, result truncation, deterministic redaction, provenance hashes, append-only query records, and audit-linked evidence identifiers;
* preserved follow-up queries and safe failure metadata in both in-memory and Firestore-backed Incident stores;
* added protected `redis-cart` evidence mappings and safe Cloud Console deep links in Incident Detail without recreating provider dashboards.

## Files changed

* `.scratch/issues/17-live-read-only-evidence-gateway.md`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/main.py`
* `backend/app/services/evidence_gateway.py`
* `backend/app/services/incident_store.py`
* `backend/tests/test_durable_incidents.py`
* `backend/tests/test_enrollment.py`
* `backend/tests/test_evidence_gateway.py`
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && python -m pytest tests/test_evidence_gateway.py tests/test_incidents.py tests/test_durable_incidents.py -q` — 37 passed.
* `cd backend && python -m pytest tests/test_evidence_gateway.py tests/test_enrollment.py tests/test_durable_incidents.py -q` — 26 passed.
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd frontend && npm test -- --run`
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify` — 113 backend tests passed, 2 integration tests skipped; 4 frontend tests passed; backend lint and typing plus frontend lint and production build passed.
