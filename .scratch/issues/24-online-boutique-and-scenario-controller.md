# 24 — Online Boutique and isolated Scenario Controller

---

id: KC-24
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-14, KC-24A]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Provide the real Managed Application and safe demo controls required to exercise KubeCouncil. Online Boutique runs under steady traffic with an authoritative Application Profile, while a separately permissioned Scenario Controller can inject and reset the recommendation OOM rollout and Protected Redis outage without revealing ground truth to the Council.

## Blocked by

- KC-14 — Application Profile and Enrollment readiness
- KC-24A — GCP and GKE environment bootstrap

## Acceptance criteria

- [x] Online Boutique deploys as the configured Managed Application with enrolled executable Deployments, dependency topology, Critical Journeys, recovery thresholds, alert mappings, and normal load generation.
- [x] `redis-cart` is declared as a Protected Dependency and is observable but outside executable remediation authority.
- [x] The recommendation scenario applies an empirically configurable unsafe memory limit that produces a new rollout and observable OOM-related customer impact.
- [x] The Redis scenario makes the Protected Dependency unavailable and can reset it after the demonstration.
- [x] Scenario Controller identity, permissions, audit trail, and code path are separate from the Investigator and Executor.
- [x] Injected ground truth is never included in Alert Signals, evidence, prompts, findings, or Investigation Records.
- [x] Demo controls are clearly labeled and disabled or absent in non-demo profiles.
- [x] Local rendering and fake tests validate Enrollment metadata, permissions, scenario transitions, reset behavior, and ground-truth isolation.

## Implementation summary

Implemented the complete local KC-24 slice and published its immutable Scenario Controller image:

* vendored the official Online Boutique `v0.10.5` release with its source checksum and a Kustomize demo overlay;
* removed the upstream external LoadBalancer while retaining the steady ten-user load generator;
* enrolled `online-boutique`, labeled ten executable Deployments as managed, and kept `redis-cart` observable as a Protected Dependency;
* added a Pydantic-valid Application Profile with full dependency topology, replica bounds, allowed actions, Critical Journey, recovery thresholds, alert mappings, evidence mappings, budgets, and observability links;
* added separate namespace Roles and RoleBindings for read-only Investigator evidence, bounded Executor targets, and a Scenario Controller restricted to `get` and `patch` on only `recommendationservice` and `redis-cart`;
* added a separately deployed, demo-only Scenario Controller using its existing dedicated KSA/GSA and internal-only Service;
* implemented typed, idempotent inject/reset state machines with resource-version preconditions, exact known-good reset values, drift refusal, structured controller-only audit events, and a non-demo readiness gate;
* used the Kubernetes Python client rather than arbitrary shell or kubectl access in the controller;
* kept scenario names and the configurable unsafe memory limit out of the Application Profile, target annotations, Alert Signals, evidence mappings, and KubeCouncil Investigation paths;
* published `linux/amd64` Scenario Controller tag `bcc5223c20c5-live27`; the successful registry push emitted digest `sha256:31ca22bb21aa818f29330d4a0e8c42a61082a50f3bd9ac77d32ad6d19e78d5de`;
* added a runbook for rendering, server-side dry-run, deployment, both scenarios, resets, negative permission checks, audit isolation, empirical memory calibration, and emergency operator reset.

Live GKE validation completed on `kubecouncil-dev`:

* all 12 Online Boutique Deployments and the separately deployed Scenario Controller became Available;
* recommendation injection changed only the `server` memory request/limit from `220Mi`/`450Mi` to the Kubernetes-valid `25Mi`/`25Mi`, created a new ReplicaSet, and produced repeated `OOMKilled`, `CrashLoopBackOff`, readiness failures, and elevated steady-load latency; reset restored `220Mi`/`450Mi` and healthy convergence;
* Redis injection changed only `redis-cart` from one replica to zero, produced cart-storage connection failures plus steady-load request failures, and reset restored one healthy replica;
* four inject/reset transitions were present on the controller-only structured audit surface, while the target Deployments contained no scenario metadata;
* negative RBAC checks denied scenario access to `frontend` and Secrets, denied Investigator mutation, and denied Executor mutation of protected Redis; the Scenario Controller could patch only its two exact targets;
* the final cluster state is healthy with no active scenario.

## Files changed

* `.scratch/issues/24-online-boutique-and-scenario-controller.md`
* `backend/app/domain/incidents.py`
* `backend/app/scenario_controller/__init__.py`
* `backend/app/scenario_controller/api.py`
* `backend/app/scenario_controller/kubernetes.py`
* `backend/app/scenario_controller/models.py`
* `backend/app/scenario_controller/service.py`
* `backend/pyproject.toml`
* `backend/tests/test_gke_bootstrap.py`
* `backend/tests/test_online_boutique_scenarios.py`
* `deploy/inventory/findydevops-dev.yaml`
* `deploy/profiles/findydevops-dev.yaml`
* `deploy/reports/findydevops-dev-bootstrap.yaml`
* `docs/runbooks/online-boutique-demo-scenarios.md`
* `manifests/incident-response/demo/application-profile.yaml`
* `manifests/incident-response/demo/kustomization.yaml`
* `manifests/incident-response/demo/scenario-controller/deployment.yaml`
* `manifests/incident-response/demo/scenario-controller/kustomization.yaml`
* `manifests/incident-response/demo/scenario-controller/service.yaml`
* `manifests/online-boutique/README.md`
* `manifests/online-boutique/overlays/demo/delete-frontend-external.yaml`
* `manifests/online-boutique/overlays/demo/enrollment-rbac.yaml`
* `manifests/online-boutique/overlays/demo/kustomization.yaml`
* `manifests/online-boutique/overlays/demo/namespace.yaml`
* `manifests/online-boutique/upstream/kubernetes-manifests-v0.10.5.yaml`
* `manifests/online-boutique/upstream/kustomization.yaml`

## Commands and tests run

* `shasum -a 256 manifests/online-boutique/upstream/kubernetes-manifests-v0.10.5.yaml` — matched the recorded `3e4d7b...` checksum.
* `kubectl kustomize manifests/incident-response/demo` — rendered 44 resources, including 13 Deployments and exactly 10 managed application Deployments.
* `cd backend && python -m pytest tests/test_online_boutique_scenarios.py -q` — 9 passed.
* `cd backend && python -m pytest tests/test_online_boutique_scenarios.py tests/test_gke_bootstrap.py tests/test_enrollment.py tests/test_incidents.py -q` — 62 passed.
* `cd backend && python -m ruff check app/scenario_controller app/domain/incidents.py tests/test_online_boutique_scenarios.py tests/test_gke_bootstrap.py` — passed.
* `cd backend && python -m mypy app` — passed.
* `docker buildx build --platform linux/amd64 --push ...kubecouncil-scenario-controller:bcc5223c20c5-live27 backend` — pushed successfully and emitted the recorded immutable digest.
* `make verify` — 209 backend tests passed, 4 gated integrations skipped; Ruff and mypy passed; 12 frontend tests, lint, and production build passed.
* `kubectl apply -k manifests/incident-response/demo --server-side --dry-run=server` — all 44 resources accepted by the live API server.
* `kubectl wait -n online-boutique --for=condition=Available deployment --all --timeout=10m` — all 12 application Deployments Available after reset.
* live recommendation inject/reset — new rollout repeatedly reached `OOMKilled`/`CrashLoopBackOff`; reset restored `220Mi`/`450Mi` and one Available replica.
* live Redis inject/reset — zero replicas produced cart-storage and load-test failures; reset restored one Available replica.
* live RBAC checks — expected results `no, no, no, yes, yes, no` for the documented negative and positive checks.
* `GET /api/demo/audit` — four isolated applied events; target metadata contained no ground truth.
