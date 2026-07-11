# 14 — Application Profile and Enrollment readiness

---

id: KC-14
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-13]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Let operators load a typed Application Profile and see whether a Managed Application is safely enrolled. The product distinguishes executable Managed Workloads from Protected Dependencies, reports every failed Enrollment prerequisite, and refuses investigation or intervention targets outside declared scope.

## Blocked by

- KC-13 — Incident contracts and fake investigation path

## Acceptance criteria

- [x] Application Profile validation covers application identity, namespaces, workloads, dependency edges, criticality, replica bounds, allowed actions, Critical Journeys, evidence mappings, budgets, and recovery expectations.
- [x] Enrollment readiness checks namespace and workload selection, required labels, role bindings, profile validity, and admission-policy binding through an injectable provider.
- [x] Protected Dependencies are observable but never executable, and visibility alone never establishes Enrollment.
- [x] Startup and reload expose exact profile or Enrollment failures without partially enabling intervention authority.
- [x] The Managed Applications API and screen show readiness, workloads, Protected Dependencies, health placeholders, and Incident history using local fakes.
- [x] Fixture and UI tests cover valid, invalid, unmanaged, and protected targets.

## Implementation summary

Implemented typed Application Profile loading and deterministic Enrollment readiness:

* expanded profile contracts for namespace allowlists, workload topology, action bounds, Critical Journeys and probes, evidence mappings, recovery fallback, and identity-bound role requirements;
* added injectable profile and Enrollment providers with local ConfigMap-style and Kubernetes-readiness fakes;
* required namespace and workload labels, namespaced Investigator and Executor RoleBindings, and admission-policy binding before targets become observable or executable;
* guarded incident creation with the current loaded profile and Enrollment state, rejecting invalid, unready, and out-of-scope targets;
* added the Managed Applications API and UI with readiness failures, Managed Workloads, Protected Dependencies, health placeholders, and Incident history;
* added backend and UI coverage for ready, invalid, unmanaged, protected, reloaded, and namespace-specific RBAC states.

## Files changed

* `.scratch/issues/14-application-profile-and-enrollment-readiness.md`
* `backend/app/api/applications.py`
* `backend/app/api/incidents.py`
* `backend/app/domain/incident_fakes.py`
* `backend/app/domain/incidents.py`
* `backend/app/main.py`
* `backend/app/services/enrollment.py`
* `backend/tests/test_enrollment.py`
* `backend/tests/test_incidents.py`
* `frontend/src/App.css`
* `frontend/src/App.test.tsx`
* `frontend/src/App.tsx`

## Commands and tests run

* `cd backend && python -m pytest tests/test_enrollment.py tests/test_incidents.py`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd frontend && npm test -- --run`
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify`
