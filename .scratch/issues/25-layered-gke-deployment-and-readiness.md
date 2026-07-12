# 25 — Layered GKE deployment and intervention readiness

---

id: KC-25
type: IMPLEMENTATION
status: DONE
labels: [ready-for-agent]
depends_on: [KC-16, KC-17, KC-18, KC-20, KC-23, KC-24, KC-24A]

---

## Parent

KC-12 — KubeCouncil Kubernetes-Native Incident Response

## What to build

Deploy the incident-response product on GKE with its intended cloud integrations and privilege boundaries. Replace the local fake runtime composition with real Firestore, Pub/Sub, Kubernetes, Cloud Logging, Cloud Monitoring, and ADK-on-Vertex providers in deployed mode. Operators can see whether the API is available, whether the real investigation providers and Gemini Council are healthy, and whether every independent Kubernetes write-enforcement layer is active before Approval is enabled.

## Blocked by

- KC-16 — Durable alerts, correlation, and timeline replay
- KC-17 — Live read-only evidence gateway
- KC-18 — Bounded parallel Council
- KC-20 — Authenticated, freshness-bound Approval
- KC-23 — Complete actions and Safe Halt semantics
- KC-24 — Online Boutique and isolated Scenario Controller
- KC-24A — GCP and GKE environment bootstrap

## Acceptance criteria

- [x] Investigator, UI, deterministic Executor, and Scenario Controller deploy as distinct components with distinct identities and permissions.
- [x] Workload Identity connects the intended components to Firestore, Pub/Sub, Vertex AI, Cloud Logging, and Cloud Monitoring without shared static credentials.
- [x] A production `IncidentCouncilModel` implementation runs all four Specialists and the Coordinator through Google ADK using the configured Vertex AI Gemini model and parses every response through the KC-18 Pydantic contracts.
- [x] Deployed runtime composition selects the real IncidentStore, alert consumer, evidence providers, Evidence Query adapters, identity verifier, intervention publisher, Kubernetes clients, and ADK Council; startup fails closed if any required real provider is unavailable.
- [x] In-memory stores, fake evidence providers, fake query adapters, fake identity, and `FakeIncidentCouncilModel` are available only in an explicit local or test profile and cannot be selected by deployed configuration.
- [x] IAP protects browser and API access, while direct backend access cannot bypass deployed identity enforcement.
- [x] Executor writes require namespaced RBAC, enrolled namespace labels, managed Deployment labels, an active admission-policy binding, and Executor-side validation.
- [x] Investigator permissions cannot mutate Kubernetes resources, and Executor permissions cannot read broad evidence or invoke models.
- [x] Health and readiness distinguish API availability, live evidence readiness, Gemini Council readiness, and intervention readiness with exact failed prerequisites; a generic Vertex connectivity check is not sufficient for Council readiness.
- [x] Rendered deployment configuration contains no unresolved placeholders, embedded credentials, demo controls in non-demo profiles, or legacy rehearsal permissions.
- [x] A live Council integration test opens or receives an Incident for the deployed Online Boutique application, retrieves real scoped evidence, records four real Gemini Specialist results and one Coordinator result, and asserts model identity, prompt version, thinking level, token usage, latency, tool count, structured-output validation, and failure metadata in Firestore.
- [x] The live Council integration test proves prompt-injected workload logs remain untrusted evidence, Specialists cannot escape the Incident scope, and no fake provider or fake model participates in the Investigation Record.
- [x] A live end-to-end integration test proves alert ingestion, Firestore persistence, real evidence access, real Council analysis, authenticated Approval, admission denial for an out-of-scope target, and one approved target-only mutation against Online Boutique.

## Implementation progress

The KC-25 implementation and layered GKE deployment are in place:

* deployed mode now composes Firestore, Pub/Sub, Kubernetes, Cloud Logging, Cloud Monitoring,
  signed IAP identity, a Pub/Sub Intervention publisher, and ADK-on-Vertex without fake fallbacks;
* local and test fakes require an explicit `development` or `test` runtime mode;
* four no-tool Specialists and one Coordinator use Google ADK structured output and every response
  is validated by the KC-18 Pydantic contracts;
* Investigator startup runs a real ADK Specialist/Coordinator contract probe rather than a generic
  Vertex connectivity check;
* readiness reports exact API, identity, Firestore, evidence, Council, and intervention
  prerequisites and disables Approval until all are ready;
* a separately built no-ADK Executor consumes the durable queue, uses Firestore leases, exposes no
  HTTP Service, and retains deterministic policy/dry-run/convergence behavior;
* Kubernetes, Cloud Logging, and Managed Prometheus evidence adapters derive scope from the
  Application Profile and exclude container environment values from model-facing change evidence;
* GKE layers deploy distinct Investigator, UI, Executor, and Scenario Controller identities, pinned
  images, a non-root UI, Google-managed IAP BackendConfigs, least-privilege RBAC, NetworkPolicy, and
  the `kubecouncil-executor-boundary` ValidatingAdmissionPolicy;
* the exact generated Investigator backend ID is bound as the signed-IAP audience;
* Investigator, Executor, UI, and Scenario Controller are healthy in GKE; Investigator readiness is
  HTTP 200 with `approval_enabled: true` and every component check passing;
* direct backend API access without the signed IAP assertion returns 401;
* negative live checks deny Investigator mutation, Executor log reads, Protected Redis mutation,
  and managed-label removal, while an allowlisted managed Deployment dry-run passes;
* live Pub/Sub alert `kc25-live-council-003` produced durable Firestore Incident
  `inc-bb64ef4e7afe42dcbe08423cf4510058` with 11 real observations from all three
  providers, four real Gemini Specialist findings, one valid Coordinator result, complete audited
  model and tool metadata, three ranked hypotheses, and zero fake references;
* the live run exposed a Vertex response-schema incompatibility with nested boolean literals; the
  transport schema was corrected while keeping the KC-18 contract authoritative, and a real
  post-fix Coordinator probe and full Council run passed;
* a labeled synthetic `k8s_container` log carrying an adversarial instruction was queried through
  the real Cloud Logging adapter by alert `kc25-live-prompt-injection-003`; Firestore Incident
  `inc-85dd1e88a9384ea8b189e7d71f31f537` retained the marker only as redacted evidence, kept every
  observation and citation within the enrolled profile, completed all five real model roles with
  valid structured output, returned `no_safe_action`, and recorded no Approval or Intervention.
* backend-scoped IAP policies on both generated backend services grant
  `roles/iap.httpsResourceAccessor` to `user:nikhil.p6257@gmail.com`; the signed browser session
  verified that principal as a Responder, and the Investigator BackendConfig uses the live-tested
  120-second request timeout;
* the authenticated rollback exposed and fixed two deterministic Executor recovery defects:
  Executor enrollment no longer reads Protected Dependencies that its identity is intentionally
  forbidden to observe, and redelivery resumes only a previously claimed `running` Intervention
  while terminal duplicate deliveries remain no-ops;
* unexpected provider failures after a durable claim now record Safe Halt before the worker logs
  the deterministic failure, preserving a complete Investigation Record without a Kubernetes write;
* authenticated Incident `inc-92bf5dfc50e048af854099114126b83f` collected real Kubernetes,
  Cloud Logging, and Cloud Monitoring evidence, ran all four Specialists and the Coordinator,
  ranked the revision-8 25Mi memory change first, passed deterministic policy and server dry-run,
  and bound Responder Approval to the live resource version, evidence, policy, recovery criteria,
  and failure strategy;
* the separate Executor consumed the hash-bound request, revalidated current state, mutated only
  `online-boutique/recommendationservice`, and converged the approved revision-7 template at
  `220Mi` request and `450Mi` limit; Firestore contains exactly one `intervention_mutated` event,
  while `redis-cart` remained Protected and ready at one replica;
* final Executor image digest
  `sha256:fb629f122639ff9b3147ff2c64164f4cf4ddd6e29ef0df7660a3a92c6c8b3aee`
  is pinned and Available with one ready replica.

## Completion evidence

All KC-25 acceptance criteria are complete. The detailed sanitized state is recorded in
`deploy/reports/findydevops-dev-kc25.yaml`; repeatable deployment and verification commands remain
in `docs/runbooks/layered-gke-deployment.md`.

## Files changed

* `.scratch/issues/25-layered-gke-deployment-and-readiness.md`
* `backend/Dockerfile.executor`
* `backend/Dockerfile.investigator`
* `backend/app/api/health.py`
* `backend/app/api/incidents.py`
* `backend/app/executor/__init__.py`
* `backend/app/executor/__main__.py`
* `backend/app/main.py`
* `backend/app/runtime/__init__.py`
* `backend/app/runtime/composition.py`
* `backend/app/runtime/config.py`
* `backend/app/runtime/live_providers.py`
* `backend/app/runtime/readiness.py`
* `backend/app/runtime/workers.py`
* `backend/app/services/adk_council.py`
* `backend/app/services/alerts.py`
* `backend/app/services/intervention_executor.py`
* `backend/pyproject.toml`
* `backend/tests/conftest.py`
* `backend/tests/integration/test_gke_runtime_live.py`
* `backend/tests/test_deployed_runtime.py`
* `backend/tests/test_intervention_executor.py`
* `deploy/reports/findydevops-dev-kc25.yaml`
* `docs/runbooks/layered-gke-deployment.md`
* `frontend/Dockerfile.ui`
* `frontend/nginx-unprivileged.conf`
* `manifests/incident-response/README.md`
* `manifests/incident-response/platform/base/executor.yaml`
* `manifests/incident-response/platform/base/investigator.yaml`
* `manifests/incident-response/platform/base/kustomization.yaml`
* `manifests/incident-response/platform/base/network-policy.yaml`
* `manifests/incident-response/platform/base/ui.yaml`
* `manifests/incident-response/platform/cluster/enrollment-read-rbac.yaml`
* `manifests/incident-response/platform/cluster/executor-admission-policy.yaml`
* `manifests/incident-response/platform/cluster/kustomization.yaml`
* `manifests/incident-response/platform/overlays/findydevops-dev/kustomization.yaml`
* `manifests/incident-response/platform/overlays/findydevops-dev/runtime-config.yaml`
* `manifests/online-boutique/overlays/demo/enrollment-rbac.yaml`

## Tests and checks run

* `python -m pytest backend/tests/test_deployed_runtime.py backend/tests/test_approval.py backend/tests/test_incident_council.py -q` — 29 passed.
* `python -m pytest backend/tests/test_deployed_runtime.py backend/tests/test_durable_incidents.py -q` — 11 passed.
* live alert `kc25-live-council-003` — passed every checked-in Firestore model/provider assertion;
  the local test runner lacked its Firestore SDK, while its independent live GKE identity assertion
  passed.
* live prompt-injection alert `kc25-live-prompt-injection-003` — passed marker retention,
  untrusted-evidence scope, structured-output, no-fake, and no-escape assertions.
* backend full pytest — 214 passed, 6 gated integration tests skipped.
* focused Ruff and strict mypy for every KC-25 source module — passed.
* real local ADK structured Council readiness probe — passed.
* real post-fix ADK Coordinator contract probe — passed with audited token usage.
* frontend production build in the non-root UI image — passed.
* GKE server-side dry-run for all layered resources — passed.
* live rollouts — Investigator, Executor, and UI Available with zero restarts after fixes.
* `make verify` — 214 backend tests passed, 6 gated integrations skipped; Ruff and strict mypy
  passed; 12 frontend tests, lint, and production build passed.
* authenticated IAP session — verified `nikhil.p6257@gmail.com` as Responder, completed the real
  Council review, and approved only the revision-7 recommendation rollback.
* protected-target server dry-run — Executor impersonation was denied access to `redis-cart`.
* `KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION=1 ... pytest
  backend/tests/integration/test_gke_runtime_live.py -q` — 3 passed against Firestore and GKE,
  covering the prior Pub/Sub alert record, distinct deployed identities, pinned images,
  authenticated Approval, exactly one target mutation, and unchanged Protected Redis state.
* `make verify` — 218 backend tests passed, 7 explicitly gated integration tests skipped; Ruff and
  strict mypy passed; 12 frontend tests, lint, and production build passed.
* final full layered server-side dry-run — passed after the scenario reset; only non-fatal
  last-applied-configuration migration warnings were reported.
* secret scan and `git diff --check` — passed; matches were identifier names and test/runbook
  sentinel strings only, with no credential values or private keys present.
