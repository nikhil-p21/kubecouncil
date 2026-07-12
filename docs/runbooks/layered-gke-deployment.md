# Layered GKE deployment and readiness

This runbook deploys the KC-25 incident-response runtime. It preserves the KC-24 Online Boutique
and Scenario Controller, and adds three separately permissioned components: Investigator/API,
deterministic Executor, and UI.

## Safety boundaries

* Investigator uses `kubecouncil-system/investigator`; it can read enrolled evidence and cannot
  mutate Online Boutique.
* Executor uses `kubecouncil-system/executor`; it has no HTTP Service, ADK package, model tools,
  workload logs, Secrets, or Protected Dependency writes.
* Scenario Controller remains `kubecouncil-demo-control/scenario-controller` and can patch only
  its two demo targets.
* Executor writes require namespaced RBAC, the enrolled namespace, an unchanged
  `kubecouncil.io/managed: "true"` label, the `kubecouncil-executor-boundary` admission binding,
  and deterministic Executor revalidation.
* Approval remains disabled until `/ready` reports every identity, Firestore, evidence, Council,
  and intervention prerequisite ready.

Do not delete the GKE cluster, Firestore database, Pub/Sub resources, Artifact Registry, Online
Boutique namespace, or shared bootstrap identities while following this runbook.

## Render and validate

```bash
kubectl kustomize manifests/incident-response/platform/overlays/findydevops-dev
kubectl apply -k manifests/incident-response/platform/overlays/findydevops-dev \
  --server-side --dry-run=server
```

Scan the rendered output for unresolved values, rehearsal authority, and credential material.
The overlay pins every image by digest and contains no Kubernetes Secret.

## Publish separated images

```bash
docker buildx build --platform linux/amd64 -f backend/Dockerfile.investigator \
  -t "$REGISTRY/kubecouncil-backend:$IMMUTABLE_TAG" --push backend
docker buildx build --platform linux/amd64 -f backend/Dockerfile.executor \
  -t "$REGISTRY/kubecouncil-executor:$IMMUTABLE_TAG" --push backend
docker buildx build --platform linux/amd64 -f frontend/Dockerfile.ui \
  -t "$REGISTRY/kubecouncil-frontend:$IMMUTABLE_TAG" --push frontend
```

Resolve each tag to a digest and update the three platform Deployments before apply. The Executor
Dockerfile deliberately installs no `investigator` extra and starts only `python -m app.executor`.

## Apply and verify

Applying the cluster layer creates a `ValidatingAdmissionPolicy`; review it before applying.

```bash
kubectl apply -k manifests/incident-response/platform/overlays/findydevops-dev --server-side
kubectl rollout status deployment/investigator -n kubecouncil-system --timeout=8m
kubectl rollout status deployment/executor -n kubecouncil-system --timeout=8m
kubectl rollout status deployment/kubecouncil-ui -n kubecouncil-system --timeout=8m
```

Readiness must return HTTP 200 with `approval_enabled: true`. A direct request to an `/api` path
without `X-Goog-IAP-JWT-Assertion` must return 401.

## IAP attachment

The two `BackendConfig` resources use the operator-provisioned `kubecouncil-iap-oauth` Secret.
External Google accounts require a custom OAuth client; the Google-managed client is restricted to
the project organization. Create the Secret out of band with `client_id` and `client_secret` keys,
never commit either value, and rotate the client secret after any accidental disclosure:

```bash
kubectl create secret generic kubecouncil-iap-oauth -n kubecouncil-system \
  --from-literal=client_id="$IAP_CLIENT_ID" \
  --from-literal=client_secret="$IAP_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
```

The `findydevops-dev` overlay also attaches a Google-managed TLS certificate for
`34-49-191-209.sslip.io`; the hostname resolves directly to the reserved demo Ingress address and
keeps certificate material out of Kubernetes and source control. Wait for both HTTPS and the
certificate before browser testing:

```bash
kubectl wait --namespace=kubecouncil-system \
  managedcertificate/kubecouncil-iap-certificate \
  --for=jsonpath='{.status.certificateStatus}'=Active --timeout=60m
curl --head https://34-49-191-209.sslip.io/
```

After GKE creates the Investigator backend service, set:

```text
KUBECOUNCIL_IAP_AUDIENCE=/projects/PROJECT_NUMBER/global/backendServices/BACKEND_SERVICE_ID
```

Grant `roles/iap.httpsResourceAccessor` only on the two generated backend services. For the public
judging window, `allAuthenticatedUsers` is intentionally granted on both backends so any Google
account can sign in. Remove those two unconditional bindings manually on October 10, 2026; do not
leave the demo public after that date. Verify both backends show IAP enabled and the custom OAuth
client attached before browser testing. The raw IP is not the browser endpoint: IAP redirects HTTP
to HTTPS, so use the certificate hostname above.

## Negative enforcement checks

Run impersonated server dry-runs:

* Investigator Deployment patch: denied by RBAC.
* Executor pod-log read: denied by RBAC.
* Executor `redis-cart` patch: denied by RBAC.
* Executor managed-target no-op patch: accepted by server dry-run.
* Executor removal of `kubecouncil.io/managed`: denied by admission policy.

## Live Council test

Publish one typed `AlertNotification` to `kc-alert-signals`, wait for the Investigator to consume it,
then run:

```bash
KUBECOUNCIL_RUN_GKE_RUNTIME_INTEGRATION=1 \
KUBECOUNCIL_LIVE_NOTIFICATION_ID=NOTIFICATION_ID \
KUBECOUNCIL_PROJECT_ID=findydevops \
python -m pytest backend/tests/integration/test_gke_runtime_live.py -q
```

The test asserts three real evidence sources, four Specialist findings, one Coordinator invocation,
complete model metadata, no fake provider reference, distinct identities, ready replicas, and pinned
image digests.

## Recovery from partial failure

* A 503 `/ready` response lists the exact failed prerequisite. Do not enable Approval manually.
* Empty Pub/Sub pull deadlines are normal and must not restart the Executor.
* If IAP is enabled but access is denied, add only the reviewed backend-scoped IAM binding.
* If a candidate image fails, pin the previous known digest and reapply; do not use mutable tags.
