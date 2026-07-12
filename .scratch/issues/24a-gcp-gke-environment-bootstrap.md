# 24A — GCP and GKE environment bootstrap

---

id: KC-24A
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-18]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Create an idempotent, credential-free bootstrap and verification path for the GCP and GKE environment that will host Online Boutique and KubeCouncil. The path must revalidate and reuse compatible resources from the earlier live deployment, create only explicitly approved missing resources, publish immutable container images, and finish with a real cloud smoke test plus a sanitized environment inventory that KC-24 and KC-25 can consume.

The previously proven hackathon defaults are:

* GCP project `findydevops`;
* region `asia-northeast1` and zone `asia-northeast1-a`;
* GKE cluster `kubecouncil-dev`;
* Artifact Registry repository `kubecouncil`;
* KubeCouncil namespace `kubecouncil-system`;
* Artifact Registry prefix `asia-northeast1-docker.pkg.dev/findydevops/kubecouncil`;
* an `amd64` GKE node and `linux/amd64` application images;
* working local `gcloud`, `kubectl`, Docker, GKE authentication, Artifact Registry push, Kustomize server-side dry-run, and Vertex AI access through Workload Identity.

These values are deployment-profile defaults to verify, not assumptions to hardcode. The bootstrap must accept an explicit project, region, zone, cluster, repository, Firestore location, resource-name prefix, image tag, and create-or-reuse mode. Firestore location is an immutable manual choice and must never be guessed.

The earlier `kubecouncil` Google/Kubernetes service-account pair and rehearsal-oriented ClusterRole are reference material only. The incident-response deployment must create or bind distinct least-privilege identities for the Investigator, Executor, and Scenario Controller, and must not restore the old cluster-wide rehearsal authority.

## Acceptance criteria

- [x] A checked-in, non-secret deployment profile declares project, region, zone, cluster, Artifact Registry repository, Firestore database and location, Pub/Sub resource names, Kubernetes namespaces, Google service-account names, Kubernetes service-account names, image repositories, and immutable image tag; environment-specific secrets and tokens are never stored in it.
- [x] A preflight command verifies the active `gcloud` account and project, billing/API access, GKE credentials, cluster version, Workload Identity support, `ValidatingAdmissionPolicy` compatibility, node architecture, schedulable capacity, Artifact Registry authentication, and required local tools before any mutation.
- [x] The bootstrap has explicit `plan`, `apply`, and `verify` modes, is safe to rerun, distinguishes reused from created resources, fails on incompatible existing resources, and requires a deliberate create-or-reuse choice rather than silently creating a new cluster.
- [x] Required Google APIs are verified or enabled for GKE, Artifact Registry, Vertex AI, Firestore, Pub/Sub, Cloud Logging, Cloud Monitoring, IAM Credentials, Service Usage, and IAP prerequisites, with every change recorded in a sanitized bootstrap report.
- [x] The configured GKE cluster is reused when compatible or created only after explicit approval, has Workload Identity enabled, exposes the expected Kubernetes context, supports the required admission APIs, and has enough allocatable CPU and memory for Online Boutique, its load generator, Investigator, UI, Executor, Scenario Controller, and smoke-test headroom.
- [x] Artifact Registry exists in the configured region; backend, frontend, Executor, and Scenario Controller images that exist at this slice are built for `linux/amd64`, tagged immutably from the source commit, pushed, resolved to digests, and recorded without registry credentials.
- [x] Firestore Native mode is configured in the explicitly approved immutable location, and dedicated Pub/Sub topics and subscriptions exist for Cloud Monitoring Alert Signals and approved Intervention requests with acknowledgement, retry, retention, and dead-letter settings documented.
- [x] Distinct Google service accounts exist for the Investigator, Executor, and Scenario Controller; each is bound only to its matching Kubernetes service account through Workload Identity and receives a reviewed minimum-role matrix for its Firestore, Pub/Sub, Vertex AI, Logging, and Monitoring responsibilities.
- [x] Negative IAM checks prove the Investigator cannot publish or consume Executor work or obtain Kubernetes write authority, the Executor cannot invoke Vertex AI or read broad observability data, and the Scenario Controller receives no Investigator or Executor cloud authority.
- [x] IAP prerequisites and the explicit Viewer and Responder principal lists are validated without committing OAuth secrets; final IAP attachment to the deployed UI/API remains part of KC-25 because it requires the live ingress or backend service.
- [x] GitHub Actions can authenticate to Google Cloud through short-lived OIDC/Workload Identity Federation, build and push immutable images, and run a manually gated deployment job without repository-stored service-account keys; automatic production deployment is not enabled.
- [x] Incident-response Kustomize configuration consumes the sanitized bootstrap outputs, renders with immutable image references and distinct identities, and contains no `PROJECT_ID`, `TAG`, `replace-me`, legacy GitHub/rehearsal settings, static credentials, or unresolved environment placeholders.
- [x] Rendered cluster-scoped and namespaced resources pass client rendering and server-side dry-run against the target GKE version before KC-24 or KC-25 applies them.
- [x] A short-lived, clearly labeled smoke workload proves Workload Identity from GKE can invoke the configured Vertex AI model, perform a Firestore round trip, perform a Pub/Sub publish/consume round trip, and access only the intended Logging and Monitoring read operations; the smoke resources and test data are removed afterward without deleting shared infrastructure.
- [x] A sanitized environment inventory records the project, cluster context, locations, namespaces, service-account emails, Workload Identity bindings, Firestore database, Pub/Sub resources, image digests, API/readiness results, and exact non-secret commands needed by KC-24 and KC-25.
- [x] A deployment runbook covers preflight, plan review, apply, image publication, verification, safe rerun, partial-failure recovery, and cleanup of only resources labeled as bootstrap smoke resources; it explicitly forbids deleting the GCP project, GKE cluster, Artifact Registry, Firestore database, shared Pub/Sub resources, or unrelated namespaces by default.
- [x] Local tests require no GCP credentials and validate configuration, command planning, idempotency, least-privilege role generation, placeholder/secret scanning, rendered manifests, failure recovery, and teardown guards; live cloud checks are marked `integration` and skip without an explicitly selected deployment profile.

## Manual checkpoints

The operator must confirm before live `apply`:

* whether to reuse `findydevops` and `kubecouncil-dev` or supply another project and cluster;
* the immutable Firestore location and database identifier;
* permission to enable APIs, create or update IAM bindings, Firestore, Pub/Sub, Artifact Registry, Workload Identity Federation, and—only if missing and explicitly requested—the GKE cluster;
* the IAP Viewer and Responder principals and eventual judge-accessible endpoint ownership;
* the configured Vertex AI model is available in the selected project and location;
* approval before applying any cluster-scoped admission policy or binding.

Codex must not print, persist, or commit access tokens, OAuth client secrets, service-account keys, Kubernetes Secret values, or credential file paths.

## Blocked by

- KC-18 — Bounded parallel Council

## Implementation summary

Implemented the credential-free environment bootstrap path and its local verification surface:

* added strict, secret-rejecting deployment-profile, observation, plan, report, smoke-result, and inventory contracts;
* added read-only `preflight`, deterministic `plan`, approval-bound `apply`, and manifest/inventory `verify` commands;
* added compatibility checks for the GCP project, billing and APIs, GKE version/context/Workload Identity/admission APIs/capacity, Artifact Registry, Firestore Native location, Pub/Sub delivery settings, IAM, Kubernetes service accounts, GitHub Workload Identity Federation, immutable image digests, negative permissions, and IAP principal lists;
* made every planned mutation deterministic and bound `apply` to an exact plan hash plus explicit approval labels, including safe recovery after partial failure;
* added a provider-backed, short-lived smoke runner for Vertex AI, Firestore, Pub/Sub, Logging, and Monitoring with guarded cleanup and provider-payload redaction;
* added least-privilege bootstrap Kustomize layers with distinct Investigator, Executor, and Scenario Controller identities and namespace-local CI deployment roles;
* added a manually gated GitHub Actions OIDC/WIF image-publish and namespace deployment path without service-account keys;
* added the confirmed `findydevops` deployment profile, sanitized provisional inventory/report, and an operator runbook covering apply, immutable image publication, verification, smoke cleanup, reruns, and partial-failure recovery;
* recorded the operator-confirmed Firestore, Pub/Sub, GSA/KSA, and GitHub WIF resources without storing credentials.

The implementation and live acceptance gate are complete. The bootstrap repaired the missing `kubecouncil-demo-control` namespace, created and bound only the Scenario Controller's matching KSA, applied the reviewed namespace-local bootstrap resources, published a new immutable `linux/amd64` backend image, passed the final idempotent live verifier and server-side dry-run, and passed the five-capability Workload Identity smoke. Both ownership labels were verified before cleanup, and Kubernetes and Pub/Sub were confirmed free of smoke resources afterward.

## Files changed

* `.github/workflows/verify-deploy.yml`
* `.scratch/issues/24a-gcp-gke-environment-bootstrap.md`
* `backend/app/bootstrap/__init__.py`
* `backend/app/bootstrap/__main__.py`
* `backend/app/bootstrap/cli.py`
* `backend/app/bootstrap/inspector.py`
* `backend/app/bootstrap/models.py`
* `backend/app/bootstrap/planner.py`
* `backend/app/bootstrap/smoke.py`
* `backend/pyproject.toml`
* `backend/tests/integration/test_gke_bootstrap_live.py`
* `backend/tests/test_gke_bootstrap.py`
* `deploy/inventory/findydevops-dev.yaml`
* `deploy/profiles/findydevops-dev.yaml`
* `deploy/reports/findydevops-dev-bootstrap.yaml`
* `docs/runbooks/gke-environment-bootstrap.md`
* `manifests/incident-response/README.md`
* `manifests/incident-response/bootstrap/admin/ci-rbac.yaml`
* `manifests/incident-response/bootstrap/admin/kustomization.yaml`
* `manifests/incident-response/bootstrap/admin/namespaces.yaml`
* `manifests/incident-response/bootstrap/kustomization.yaml`
* `manifests/incident-response/bootstrap/namespaced/environment-config.yaml`
* `manifests/incident-response/bootstrap/namespaced/kustomization.yaml`
* `manifests/incident-response/bootstrap/namespaced/service-accounts.yaml`

## Commands and tests run

* `cd backend && python -m pytest tests/test_gke_bootstrap.py -q` — 18 passed.
* `cd backend && python -m ruff check app/bootstrap tests/test_gke_bootstrap.py tests/integration/test_gke_bootstrap_live.py` — passed.
* `cd backend && python -m mypy app/bootstrap` — passed.
* `kubectl kustomize manifests/incident-response/bootstrap` — passed.
* YAML parsing for `.github/workflows/verify-deploy.yml` — passed.
* `git diff --check` — passed.
* `KUBECOUNCIL_RUN_GKE_BOOTSTRAP_INTEGRATION=1 python -m pytest tests/integration/test_gke_bootstrap_live.py -q` — live environment test passed; provider smoke case skipped because the temporary-resource variables were intentionally removed after the separately recorded successful pod smoke.
* `make verify` — 200 backend tests passed, 4 explicitly gated integration tests skipped; Ruff and mypy passed; 12 frontend tests, lint, and production build passed.

## Live verification evidence

* Preflight confirmed the active project and account, billing, all required APIs, GKE `1.35.5-gke.1241004`, the expected context, Workload Identity, admission API availability, `amd64` capacity, Artifact Registry, Firestore location, Pub/Sub delivery configuration, GitHub federation, immutable image digests, and every positive and negative IAM check.
* The reviewed plan hash `f39d5127886eff5ea8c8c1034ca334263b63517e44477e7624134bdfce4b6bc1` contained only the missing demo namespace, Scenario Controller KSA, and its matching Workload Identity binding; it was applied with only the `identity` approval.
* The complete incident-response bootstrap passed server-side dry-run and the final verifier returned no incompatible resources, no planned actions, and no required manual inputs.
* Backend image tag `4e1f6a84c445-live24` resolves to `sha256:2cde6d17600de23b44c399eee48da82d8a299fa515bd8ed9453351451d45b331`; the existing frontend digest was reverified.
* The Investigator KSA smoke passed Vertex AI, Firestore create/read/delete, Pub/Sub publish/consume, Logging read, and Monitoring read.
* Both ownership labels were checked on the temporary pod, ConfigMap, topic, and subscription before deletion; follow-up Kubernetes and Pub/Sub listings confirmed no smoke resources remained.
* `user:nikhil.p6257@gmail.com` is the initial allowed Viewer and Responder. Judge identities remain a KC-25 Viewer-only addition when their addresses are known; final IAP attachment remains KC-25 because it requires the live ingress/backend service.
