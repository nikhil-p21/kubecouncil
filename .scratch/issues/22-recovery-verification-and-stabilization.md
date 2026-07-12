# 22 — Recovery verification and stabilization

---

id: KC-22
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-21]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Prove that an approved rollback restores customer health before resolving an Incident. Verification combines Kubernetes convergence, workload symptom cessation, Critical Journey evidence, Evidence Sufficiency, and a Stabilization Window while streaming progress to the Responder.

## Blocked by

- KC-21 — Privilege-separated rollback Intervention

## Acceptance criteria

- [x] Verification checks observed generation, approved active revision, desired and updated replicas, availability, unavailable replicas, OOM terminations, and restart deltas.
- [x] Critical Journey verification checks success rate and P95 latency against the Application Profile and healthy baseline.
- [x] Application metric windows require at least 100 requests before success or latency claims are considered sufficient.
- [x] Repeated Synthetic Probes may establish fallback availability for sparse traffic, while latency remains explicitly insufficient.
- [x] Recovery Criteria must hold for two consecutive 60-second windows before the Incident becomes resolved.
- [x] Successful mutation remains distinct from verified recovery, and provider alert closure cannot substitute for recovery evidence.
- [x] Incident Detail streams convergence, verification, traffic sufficiency, stabilization, and final lifecycle and intervention outcomes.
- [x] Tests use configurable clocks and deterministic fakes to cover recovery, regression, sparse traffic, insufficient evidence, and stabilization reset.

## Implementation summary

Implemented deterministic post-rollback recovery verification and stabilization:

* added provider-independent Kubernetes, Critical Journey, Synthetic Probe, and Recovery Assessment contracts;
* added a deterministic verifier that checks generation, approved revision, complete replica convergence, OOM cessation, restart deltas, profile thresholds, and healthy baselines;
* enforced the Application Profile request minimum before availability or P95 conclusions, with repeated Synthetic Probes limited to sparse-traffic availability fallback;
* added append-only, compare-and-set recovery persistence with equivalent in-memory and Firestore-shaped behavior;
* kept successful mutation in monitoring until two contiguous configured windows pass, resetting progress on regression and ignoring provider alert closure as lifecycle authority;
* added replay-driven Incident Detail recovery progress for convergence, symptoms, traffic sufficiency, stabilization, and final outcomes.

## Files changed

* `.scratch/issues/22-recovery-verification-and-stabilization.md`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/services/incident_store.py`
* `backend/app/services/recovery_verifier.py`
* `backend/tests/test_recovery_verification.py`
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && ../.venv/bin/python -m pytest tests/test_recovery_verification.py -q` — 11 passed.
* Focused backend incident, executor, durable-store, lint, and strict typing checks — passed.
* `cd frontend && npm test -- --run` — 8 passed.
* `cd frontend && npm run lint` — passed.
* `cd frontend && npm run build` — passed.
* `make verify` — 170 backend tests passed, 2 integration tests skipped; backend lint and strict typing passed; 8 frontend tests, lint, and production build passed.
