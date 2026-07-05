# `.scratch/issues/04-safe-rehearsal-twin.md`

---

id: KC-04
status: DONE
depends_on: [KC-03]
--------------------

## Objective

Generate and deploy a safe ephemeral rehearsal twin.

## Build

Implement `RehearsalPlanner`.

Generate an ephemeral Kustomize overlay outside the source repository.

The overlay must:

* target namespace `kc-rehearsal-<run-id>`;
* add rehearsal labels;
* create a ResourceQuota;
* disable or omit Ingress;
* omit production Secrets;
* omit unsupported resources;
* preserve internal Service names;
* apply rehearsal-only ConfigMap values;
* use synthetic or mock dependency settings;
* add cleanup metadata.

Implement deterministic policy checks before deployment.

Implement Kubernetes operations:

* create namespace;
* validate rendered manifests;
* run server-side dry run;
* apply resources;
* wait for rollout;
* inspect pod readiness;
* delete rehearsal namespace.

Create APIs:

```text
POST   /api/runs/{run_id}/rehearsal
GET    /api/runs/{run_id}/rehearsal
DELETE /api/runs/{run_id}/rehearsal
```

## Safety requirements

* Refuse namespaces not beginning with `kc-rehearsal-`.
* Never use the source namespace.
* Never copy Secrets.
* Never apply cluster-scoped resources.
* Record every resource created.

## Acceptance criteria

* The demo target deploys into a unique rehearsal namespace.
* Source manifests remain unchanged.
* All expected Deployments become ready.
* Deleting the rehearsal removes only rehearsal resources.
* A failed rollout returns a structured error and cleanup remains possible.

## Tests

* Overlay generation snapshot.
* Namespace guard.
* Secret omission.
* Cluster-scoped resource rejection.
* Fake Kubernetes deployment.
* Cleanup idempotency.

## Integration test

Provide an optional integration test requiring a configured Kubernetes context.

## Commit

`feat: create isolated kubernetes rehearsal twins`

## Note

GCP project: configured through GOOGLE_CLOUD_PROJECT
Region: asia-northeast1
Zone: asia-northeast1-a
Cluster: kubecouncil-dev
Cluster mode: GKE Standard zonal
Source namespace: shop-demo
Rehearsal namespace prefix: kc-rehearsal-
Artifact Registry repository: kubecouncil
Kubernetes context: current GKE context

## Implementation summary

Implemented safe rehearsal twin creation with:

* `RehearsalPlanner` that generates run-scoped overlays outside the source repository;
* deterministic namespace, source-namespace, Secret, Ingress, unsupported-resource and cluster-scoped-resource safety handling;
* generated rehearsal `Namespace` and `ResourceQuota` manifests with cleanup metadata and rehearsal labels;
* ConfigMap rehearsal substitutions, Secret env/volume omission and internal service preservation;
* `KubectlKubernetesClient` behind the `KubernetesClient` interface for validation, server-side dry run, apply, rollout wait, pod readiness inspection and namespace deletion;
* persisted `RehearsalState`, recorded created resources and readiness validation;
* `POST`, `GET` and `DELETE /api/runs/{run_id}/rehearsal`;
* fake Kubernetes support for unit tests;
* an optional Kubernetes integration smoke test gated by `KUBECOUNCIL_RUN_K8S_INTEGRATION=1`.

## Files changed

* `backend/app/api/runs.py`
* `backend/app/domain/__init__.py`
* `backend/app/domain/fakes.py`
* `backend/app/domain/interfaces.py`
* `backend/app/domain/models.py`
* `backend/app/kubernetes/client.py`
* `backend/app/kubernetes/kustomize.py`
* `backend/app/rehearsal/planner.py`
* `backend/pyproject.toml`
* `backend/tests/test_domain_models.py`
* `backend/tests/test_rehearsal.py`
* `.scratch/issues/04-safe-rehearsal-twin.md`

## Commands and tests run

* `cd backend && python -m pytest tests/test_rehearsal.py tests/test_domain_models.py tests/test_fakes.py`
* `cd backend && python -m pytest tests/test_rehearsal.py`
* `cd backend && python -m pytest`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `make verify`

## Remaining limitations

* Real Kubernetes deployment was not executed by default; the integration test is opt-in and requires a configured Kubernetes context.
* The first real deployment will still require images referenced by the rendered manifests to be pullable by the target cluster.
* Rehearsal ConfigMap substitutions are intentionally conservative and deterministic; richer dependency mocking remains for later phases.
