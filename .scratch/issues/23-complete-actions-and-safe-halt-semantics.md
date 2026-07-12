# 23 — Complete actions and Safe Halt semantics

---

id: KC-23
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-22]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Complete the allowlisted action set and make every failed or ambiguous Intervention stop safely. Scale and restart follow the same Approval, queue, lease, mutation, verification, and audit path as rollback, while each action uses its own failure and restoration semantics.

## Blocked by

- KC-22 — Recovery verification and stabilization

## Acceptance criteria

- [x] Bounded scale and controlled restart execute through action-specific minimal patches under the same deterministic safety boundary as rollback.
- [x] A failed scale restores the previous replica count only when current policy still proves that restoration safe.
- [x] A restart has no inverse mutation and escalates without inventing a rollback.
- [x] A failed Deployment rollback never automatically restores a revision implicated in the Incident.
- [x] Lease loss, stale state, external mutation, failed dry-run, ambiguous convergence, or unsafe restoration stops further writes and enters Safe Halt.
- [x] Every attempted action, restoration, refusal, failure, and Safe Halt is reconstructable from the Investigation Record.
- [x] UI and tests distinguish succeeded, rolled back, failed, monitoring, resolved, and safely halted states without conflating lifecycle and outcomes.

## Implementation summary

Implemented the complete allowlisted Intervention path and action-specific failure semantics:

* generalized the deterministic Executor to revalidate, dry-run, apply, and verify rollback, bounded scale, and controlled restart actions through the same Approval, queue, lease, and audit boundary;
* added typed convergence outcomes that distinguish success, definitive action failure, and ambiguity without relying on an LLM;
* restored a failed scale to its exact prior replica count only after current-state comparison, fresh deterministic policy evaluation, server-side dry-run, lease renewal, and restoration convergence;
* made restart and rollback failures terminal and operator-visible without inventing inverse mutations, including an explicit prohibition on restoring the revision implicated by a failed rollback;
* made stale state, external mutation, lease loss, dry-run rejection, ambiguous convergence, and unsafe restoration enter Safe Halt and stop Kubernetes writes;
* recorded claims, validation, mutations, action failures, restorations, refusals, escalations, and Safe Halt as append-only audit events;
* extended recovery verification to use action-specific convergence checks for scale and restart as well as rollback;
* added Intervention execution UI states and audit reasons while keeping lifecycle and intervention outcomes independent.

## Files changed

* `.scratch/issues/23-complete-actions-and-safe-halt-semantics.md`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/services/intervention_executor.py`
* `backend/app/services/recovery_verifier.py`
* `backend/tests/test_complete_intervention_actions.py`
* `backend/tests/test_intervention_executor.py`
* `backend/tests/test_recovery_verification.py`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && ../.venv/bin/python -m pytest tests/test_complete_intervention_actions.py tests/test_intervention_executor.py tests/test_recovery_verification.py tests/test_proposal_policy.py tests/test_approval.py -q` — 59 passed.
* `cd backend && ../.venv/bin/python -m pytest -q` — 182 passed, 2 integration tests skipped.
* `cd backend && ../.venv/bin/python -m ruff check .` — passed.
* Focused strict mypy for the changed backend source modules — passed.
* `cd frontend && npm test -- --run` — 12 passed.
* `cd frontend && npm run lint` — passed.
* `cd frontend && npm run build` — passed.
* `make verify` — 182 backend tests passed, 2 integration tests skipped; backend lint and strict typing passed; 12 frontend tests, lint, and production build passed.
