# 21 — Privilege-separated rollback Intervention

---

id: KC-21
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-20]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Execute an approved Deployment rollback through a separately deployed, deterministic Executor. An idempotent queue crosses the privilege boundary, a workload lease serializes intervention, and the Executor independently revalidates every safety binding before changing only the approved Deployment revision.

## Blocked by

- KC-20 — Authenticated, freshness-bound Approval

## Acceptance criteria

- [x] The Investigator publishes a hashed, idempotent intervention request only after current valid Approval.
- [x] The Executor exposes no public endpoint, model runtime, agent tools, or broad evidence-query capability.
- [x] The Executor acquires and renews one Firestore-backed lease per Managed Workload before attempting mutation.
- [x] After lease acquisition, the Executor revalidates Approval, policy, Enrollment, labels, target versions, revision, patch shape, dry-run, and optimistic-concurrency preconditions.
- [x] A valid rollback changes only the approved Deployment and produces immutable audit events for receipt, validation, dry-run, mutation, and convergence progress.
- [x] Duplicate delivery cannot repeat a completed mutation and concurrent Interventions cannot act on the same workload.
- [x] Fake-backed end-to-end tests prove successful rollback plus stale state, invalid authority, external mutation, and duplicate-message rejection.

## Implementation summary

Implemented the privilege-separated rollback Intervention vertical slice:

* added strict, hash-bound Intervention request and workload-lease contracts plus narrow publisher, lease, and Executor Kubernetes interfaces;
* published idempotent requests only after a current authenticated Approval, with an in-memory fake and durable Pub/Sub publisher adapter;
* added Firestore transactional workload leases with local parity fakes, per-workload acquisition, renewal, expiry, ownership checks, and release;
* added a deterministic rollback Executor consumer with no HTTP, model, agent, or evidence-query surface;
* independently revalidated Approval, policy, Enrollment labels, target versions, revision safety, exact patch shape, server dry-run, and optimistic concurrency after lease acquisition;
* recorded append-only receipt, validation, dry-run, mutation, convergence, failure, and duplicate-delivery audit events through both in-memory and Firestore-shaped Incident stores;
* added fake-backed end-to-end coverage for successful rollback, rejected human decisions, stale state, forged authority, racing external mutation, duplicate delivery, and lease contention.

## Files changed

* `.scratch/issues/21-privilege-separated-rollback-intervention.md`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/main.py`
* `backend/app/services/approval.py`
* `backend/app/services/incident_store.py`
* `backend/app/services/intervention_executor.py`
* `backend/app/services/intervention_queue.py`
* `backend/app/services/workload_lease.py`
* `backend/tests/test_intervention_executor.py`

## Commands and tests run

* `cd backend && ../.venv/bin/python -m pytest tests/test_intervention_executor.py -q` — 8 passed.
* `cd backend && ../.venv/bin/python -m pytest tests/test_intervention_executor.py tests/test_approval.py tests/test_proposal_policy.py tests/test_durable_incidents.py -q` — 43 passed.
* `cd backend && ../.venv/bin/python -m pytest -q` — 159 passed, 2 skipped.
* `cd backend && ../.venv/bin/python -m ruff check .` — passed.
* Focused strict mypy for all KC-21 modules and `app/main.py` — passed.
* `cd frontend && npm test -- --run` — 7 passed.
* `cd frontend && npm run lint` — passed.
* `cd frontend && npm run build` — passed.
* `make verify` — 159 backend tests passed, 2 integration tests skipped, strict backend typing and lint passed, and 7 frontend tests, lint, and production build passed.
