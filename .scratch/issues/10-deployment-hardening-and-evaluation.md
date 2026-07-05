# `.scratch/issues/10-deployment-hardening-and-evaluation.md`

---

id: KC-10
status: BLOCKED
depends_on: [KC-09]
------------------

## Objective

Deploy the complete application and make the demonstration repeatable.

## Build

Deploy:

* KubeCouncil backend;
* KubeCouncil frontend;
* demo source application;
* rehearsal namespace permissions.

Add:

* GitHub credentials through environment or secret injection;
* Gemini credentials;
* namespace-scoped Kubernetes RBAC;
* structured Cloud Logging;
* rehearsal TTL cleanup;
* health and readiness endpoints;
* API request IDs;
* timeouts;
* retries only for safe idempotent operations.

Create CI:

1. backend tests;
2. frontend tests;
3. lint and type checks;
4. container builds;
5. image push;
6. deployment;
7. smoke test.

Create agent evaluation fixtures:

* successful capacity negotiation;
* critical-service protection;
* unavailable degradation mode;
* insufficient capacity;
* malformed proposal;
* unsafe repository change.

## Acceptance criteria

* Public or judge-accessible UI works.
* Full workflow succeeds three consecutive times.
* Draft PR creation works from the deployed application.
* Rehearsal namespaces are cleaned up.
* Secrets are absent from logs and repository.
* The system demonstrates both successful negotiation and safe refusal.

## Manual checkpoints

The user must provide or approve:

* GCP project;
* GKE access;
* Gemini credentials;
* GitHub token or GitHub App installation;
* final public repository;
* deployment domain or endpoint.

Codex must not invent credentials or commit secrets.

## Commit

`chore: deploy and harden kubecouncil demonstration`

## Implementation summary

Implemented the credential-free deployment hardening and evaluation groundwork:

* added backend request ID middleware and structured JSON request logging;
* added `/ready` alongside `/health` for Kubernetes readiness checks;
* added bounded subprocess timeouts for Git, kubectl rehearsal operations and k6 Jobs;
* added TTL annotations to generated rehearsal resources;
* added a guarded rehearsal namespace cleanup command that deletes only expired `kc-rehearsal-*` namespaces with KubeCouncil rehearsal labels;
* added Kubernetes manifests for backend, frontend, namespace-scoped system deployment, RBAC, network policy and cleanup CronJob;
* added a CI workflow for backend tests, frontend tests, lint, type checks, image builds and guarded deploy placeholders;
* added deterministic agent evaluation fixtures for successful negotiation and safe-refusal cases;
* added tests for health/readiness, request IDs, cleanup guards and secret-free deployment manifests.

The live deployment portion is blocked because this environment does not have the required GCP/GKE/Gemini/GitHub credentials or an approved public endpoint.

## Files changed

* `.github/workflows/verify-deploy.yml`
* `backend/app/api/health.py`
* `backend/app/kubernetes/cleanup.py`
* `backend/app/kubernetes/client.py`
* `backend/app/main.py`
* `backend/app/observability.py`
* `backend/app/rehearsal/planner.py`
* `backend/app/repositories/github.py`
* `backend/app/scenarios/k6.py`
* `backend/tests/test_operational_hardening.py`
* `docs/agent-evaluation-fixtures.yaml`
* `manifests/kubecouncil/README.md`
* `manifests/kubecouncil/base/backend.yaml`
* `manifests/kubecouncil/base/cleanup-cronjob.yaml`
* `manifests/kubecouncil/base/configmap.yaml`
* `manifests/kubecouncil/base/frontend.yaml`
* `manifests/kubecouncil/base/kustomization.yaml`
* `manifests/kubecouncil/base/namespace.yaml`
* `manifests/kubecouncil/base/network-policy.yaml`
* `manifests/kubecouncil/base/service-account-rbac.yaml`

## Commands and tests run

* `cd backend && python -m pytest tests/test_operational_hardening.py tests/test_rehearsal.py tests/test_scenarios.py tests/test_repository_provider.py`
* `cd backend && python -m pytest`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd backend && python -m compileall app`
* `cd frontend && npm test`
* `cd frontend && npm run lint`
* `cd frontend && npm run build`
* `make verify`
* `rg -n "(BEGIN [A-Z ]*PRIVATE KEY|PRIVATE KEY|password:|token:|secret:|GITHUB_TOKEN=|GOOGLE_APPLICATION_CREDENTIALS=|AIza[0-9A-Za-z_-]{35}|ghp_[0-9A-Za-z_]{36}|github_pat_[0-9A-Za-z_]+)" . --glob '!frontend/node_modules/**' --glob '!backend/.mypy_cache/**' --glob '!backend/.pytest_cache/**' --glob '!backend/__pycache__/**' --glob '!backend/app/**/__pycache__/**' --glob '!frontend/dist/**'`

## Remaining limitations

* Manual action required: provide the target GCP project, GKE cluster access, Artifact Registry repository, deployable image tags, Gemini Vertex AI credentials, GitHub token or GitHub App installation, final public repository and deployment domain or endpoint.
* Manual action required: create the deployment-time `kubecouncil-deployment-config` ConfigMap and `kubecouncil-credentials` Secret, or replace the GitHub token path with an approved GitHub App configuration.
* Manual action required: enable the CI image push and deploy steps after configuring Workload Identity or an equivalent GitHub Actions-to-GCP authentication path.
* Public or judge-accessible UI validation, three consecutive full workflow runs, live draft PR creation, and live namespace cleanup verification remain blocked on the above credentials and access.
* No real GKE, Gemini, GitHub network PR creation or public deployment was executed from this environment.
