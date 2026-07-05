# `.scratch/issues/03-kustomize-analysis.md`

---

id: KC-03
status: DONE
depends_on: [KC-02]
--------------------

## Objective

Render the connected Kustomize deployment and build service profiles.

## Build

Implement `KustomizeManifestRenderer`.

Run:

```bash
kubectl kustomize <deployment-path>
```

Parse multi-document YAML.

Build a compatibility report.

Supported resources:

* Deployment;
* Service;
* ConfigMap;
* HPA;
* PDB.

Reject or report:

* StatefulSet;
* PVC;
* PV;
* CRD;
* cluster-scoped RBAC;
* unresolved Secret references;
* unsupported external dependencies.

Build `ServiceProfile` objects containing:

* service name;
* image;
* current replicas;
* minimum and maximum replicas;
* CPU and memory requests;
* criticality;
* dependencies;
* degradation modes;
* optional status;
* associated ConfigMaps;
* associated HPA.

Create API:

```text
POST /api/runs/{run_id}/analyse
```

## Acceptance criteria

* All demo services are discovered.
* Checkout dependencies include payment and recommendation.
* Unsupported resources appear in a structured compatibility report.
* Manifest parsing does not mutate the repository.
* Every inferred value includes its source.

## Tests

* Kustomize fixture rendering.
* Multi-document YAML parsing.
* Annotation parsing.
* Dependency graph creation.
* Secret-reference rejection.
* Unsupported-resource reporting.

## Commit

`feat: analyse kustomize repositories into service profiles`

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

Implemented Kustomize analysis with:

* `KustomizeManifestRenderer` using `kubectl kustomize <deployment-path>`;
* multi-document YAML parsing into Pydantic `ManifestResource` objects;
* structured compatibility reporting for unsupported resources, production Secrets, unresolved Secret references and dependencies that do not resolve to rendered Deployments;
* `ServiceProfile` extraction for Deployments including image, replicas, HPA bounds, resource requests, annotations, ConfigMaps, namespace and source metadata for inferred fields;
* dependency graph construction from service profile dependencies;
* `POST /api/runs/{run_id}/analyse` with run-store persistence for source, profiles, compatibility issues and analysis result;
* injected renderer command runner for local unit tests without real GitHub, GKE or credentials.

## Files changed

* `backend/app/api/runs.py`
* `backend/app/domain/models.py`
* `backend/app/kubernetes/kustomize.py`
* `backend/app/main.py`
* `backend/pyproject.toml`
* `backend/tests/test_domain_models.py`
* `backend/tests/test_kustomize_analysis.py`
* `.scratch/issues/03-kustomize-analysis.md`

## Commands and tests run

* `cd backend && python3 -m pytest tests/test_kustomize_analysis.py tests/test_domain_models.py tests/test_fakes.py tests/test_repository_api.py` (failed: this Python 3.14 interpreter has no pytest)
* `cd backend && python -m pytest tests/test_kustomize_analysis.py tests/test_domain_models.py tests/test_fakes.py tests/test_repository_api.py`
* `cd backend && python -m pytest`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `make verify`

## Remaining limitations

* Runtime rendering still requires `kubectl` on the backend host; unit tests inject rendered YAML to stay credential-free and deterministic.
* The analyser reports unsupported resources and unresolved external dependencies but does not attempt remediation in this phase.
* No real GKE, GitHub or credential-backed integration was exercised.
