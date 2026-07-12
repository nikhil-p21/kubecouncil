# GKE environment bootstrap runbook

This runbook provisions and verifies only the shared `findydevops-dev` environment used by
KC-24 and KC-25. It does not deploy Online Boutique, attach IAP, apply the Executor admission
policy, or enable a production deployment path.

## Safety rules

- Review `plan` output and bind `apply` to its exact SHA-256 plan hash.
- Confirm the Firestore database ID and immutable location outside the tool before creation.
- Use `reuse-only` for `kubecouncil-dev`; a missing cluster is an error, not permission to create.
- Never store access tokens, OAuth secrets, service-account keys, Kubernetes Secret values, or
  credential paths in profiles, reports, logs, or shell history.
- Never delete the project, cluster, Artifact Registry, Firestore database, shared Pub/Sub
  topics/subscriptions, or unrelated namespaces during bootstrap recovery or cleanup.
- Cleanup may remove only resources carrying both `kubecouncil.io/bootstrap-smoke=true` and
  `kubecouncil.io/environment=findydevops-dev`.

## Local preflight and planning

From the repository root:

```bash
PYTHONPATH=backend .venv/bin/python -m app.bootstrap \
  --profile deploy/profiles/findydevops-dev.yaml preflight

PYTHONPATH=backend .venv/bin/python -m app.bootstrap \
  --profile deploy/profiles/findydevops-dev.yaml plan \
  --output /tmp/kubecouncil-bootstrap-plan.json
```

Preflight verifies the active account and project, billing, APIs, cluster location and version,
Workload Identity, admission API availability, node architecture and allocatable capacity,
Artifact Registry, Firestore, Pub/Sub, service accounts, Kubernetes identities, GitHub OIDC,
published image digests, and negative IAM invariants. It reports only whether an active account
exists; it does not persist credentials.

An empty action list is the idempotent steady state. An incompatible resource always blocks
apply. In particular, a Firestore location mismatch and a missing `reuse-only` cluster cannot be
repaired automatically.

## Apply a reviewed plan

Read the generated plan, collect only the approval labels it names, then run:

```bash
PLAN_HASH="$(jq -r .plan_hash /tmp/kubecouncil-bootstrap-plan.json)"

PYTHONPATH=backend .venv/bin/python -m app.bootstrap \
  --profile deploy/profiles/findydevops-dev.yaml apply \
  --approve-plan-hash "$PLAN_HASH" \
  --approve identity \
  --allow-mutation
```

Add other approval labels only when they are present in the reviewed plan, for example
`enable-apis`, `create-shared-resources`, `github-federation`, `publish-images`, or the exact
`firestore-location:<location>` value. `create-cluster` is intentionally invalid for the checked-in
`reuse-only` profile.

If a command fails partway through, do not reverse completed shared-resource operations. Rerun
preflight and plan; already-compatible resources become `reused`, and only the remaining actions
are proposed under a new plan hash.

## Bootstrap Kubernetes identities and CI access

Because server-side dry-run does not persist a missing namespace for later resources in the same
request, an administrator validates and creates the namespaces first:

```bash
kubectl apply \
  -f manifests/incident-response/bootstrap/admin/namespaces.yaml \
  --server-side --dry-run=server
kubectl apply \
  -f manifests/incident-response/bootstrap/admin/namespaces.yaml \
  --server-side
```

Then validate and apply the complete admin layer, including the narrow CI RoleBindings:

```bash
kubectl apply -k manifests/incident-response/bootstrap/admin \
  --server-side --dry-run=server
kubectl apply -k manifests/incident-response/bootstrap/admin --server-side
```

Then validate and reconcile the namespaced, non-secret resources:

```bash
kubectl apply -k manifests/incident-response/bootstrap/namespaced \
  --server-side --dry-run=server
kubectl apply -k manifests/incident-response/bootstrap/namespaced --server-side
```

The GitHub deployer can manage only the environment ConfigMap and Kubernetes service accounts in
the two control namespaces. It cannot create namespaces, bind cluster roles, deploy application
workloads, read Secrets, or apply admission policies in this slice.

## Immutable images and GitHub Actions

The `Verify and Deploy` workflow always verifies and builds both images for `linux/amd64`. Image
publication and the namespaced bootstrap deployment run only through manual `workflow_dispatch`
boolean inputs. GitHub authenticates through the repository-restricted Workload Identity provider;
no repository service-account key is used.

After publishing, resolve each full-commit tag to a digest and use only
`repository@sha256:<digest>` in manifests and inventories. Executor and Scenario Controller image
repositories remain declared but unpublished until those deployable entrypoints exist.

## Workload Identity cloud smoke

The smoke uses a short-lived topic, subscription, ConfigMap, and Investigator pod. It mounts the
checked-in smoke module into the previously verified digest-pinned backend image, so bootstrap
verification does not depend on publishing an image before the image pipeline itself is verified.

```bash
SMOKE_ID="kc-bootstrap-smoke-$(date +%Y%m%d%H%M%S)"
SMOKE_TOPIC="projects/findydevops/topics/${SMOKE_ID}"
SMOKE_SUB="projects/findydevops/subscriptions/${SMOKE_ID}"
INVESTIGATOR_GSA="kc-investigator@findydevops.iam.gserviceaccount.com"
BACKEND_IMAGE="asia-northeast1-docker.pkg.dev/findydevops/kubecouncil/kubecouncil-backend@sha256:2cde6d17600de23b44c399eee48da82d8a299fa515bd8ed9453351451d45b331"

gcloud pubsub topics create "$SMOKE_ID" --project=findydevops \
  --labels=kubecouncil-bootstrap-smoke=true,kubecouncil-environment=findydevops-dev
gcloud pubsub subscriptions create "$SMOKE_ID" --topic="$SMOKE_ID" --project=findydevops \
  --expiration-period=1d --labels=kubecouncil-bootstrap-smoke=true,kubecouncil-environment=findydevops-dev
gcloud pubsub topics add-iam-policy-binding "$SMOKE_ID" --project=findydevops \
  --member="serviceAccount:${INVESTIGATOR_GSA}" --role=roles/pubsub.publisher
gcloud pubsub subscriptions add-iam-policy-binding "$SMOKE_ID" --project=findydevops \
  --member="serviceAccount:${INVESTIGATOR_GSA}" --role=roles/pubsub.subscriber
```

Create the owned script ConfigMap and run the digest-pinned pod:

```bash
kubectl create configmap "$SMOKE_ID" -n kubecouncil-system \
  --from-file=smoke.py=backend/app/bootstrap/smoke.py
kubectl label configmap "$SMOKE_ID" -n kubecouncil-system \
  kubecouncil.io/bootstrap-smoke=true \
  kubecouncil.io/environment=findydevops-dev

SMOKE_OVERRIDES="$(jq -cn \
  --arg config "$SMOKE_ID" \
  --arg image "$BACKEND_IMAGE" \
  --arg topic "$SMOKE_TOPIC" \
  --arg subscription "$SMOKE_SUB" '{
  spec: {
    serviceAccountName: "investigator",
    restartPolicy: "Never",
    containers: [{
      name: $config,
      image: $image,
      command: ["python", "/smoke/smoke.py"],
      env: [
        {name: "PYTHONPATH", value: "/app"},
        {name: "GOOGLE_CLOUD_PROJECT", value: "findydevops"},
        {name: "GOOGLE_CLOUD_LOCATION", value: "asia-northeast1"},
        {name: "KUBECOUNCIL_GEMINI_MODEL", value: "gemini-3.5-flash"},
        {name: "KUBECOUNCIL_FIRESTORE_DATABASE", value: "(default)"},
        {name: "KUBECOUNCIL_SMOKE_PUBSUB_TOPIC", value: $topic},
        {name: "KUBECOUNCIL_SMOKE_PUBSUB_SUBSCRIPTION", value: $subscription}
      ],
      volumeMounts: [{name: "smoke-script", mountPath: "/smoke", readOnly: true}]
    }],
    volumes: [{
      name: "smoke-script",
      configMap: {name: $config}
    }]
  }
}')"

kubectl run "$SMOKE_ID" -n kubecouncil-system \
  --image="$BACKEND_IMAGE" --restart=Never \
  --labels="kubecouncil.io/bootstrap-smoke=true,kubecouncil.io/environment=findydevops-dev" \
  --overrides="$SMOKE_OVERRIDES"

kubectl wait -n kubecouncil-system --for=jsonpath='{.status.phase}'=Succeeded \
  "pod/${SMOKE_ID}" --timeout=180s
kubectl logs -n kubecouncil-system "$SMOKE_ID"
```

The output contains only check names, pass/fail state, and error class. It never includes provider
payloads, model content, tokens, or credential paths. The Firestore smoke document is deleted in a
`finally` block.

After confirming both ownership labels, remove only the smoke resources:

```bash
kubectl delete pod "$SMOKE_ID" -n kubecouncil-system
kubectl delete configmap "$SMOKE_ID" -n kubecouncil-system
gcloud pubsub subscriptions delete "$SMOKE_ID" --project=findydevops
gcloud pubsub topics delete "$SMOKE_ID" --project=findydevops
```

## Final verification and inventory

The checked-in development profile uses the operator as both an allowed Viewer and a Responder.
When judge identities are known, add them as Viewer-only principals or supply the complete lists as
command overrides. No OAuth secret is needed:

```bash
PYTHONPATH=backend .venv/bin/python -m app.bootstrap \
  --profile deploy/profiles/findydevops-dev.yaml \
  --viewer-principal "user:nikhil.p6257@gmail.com" \
  --viewer-principal "user:judge@example.com" \
  --responder-principal "user:nikhil.p6257@gmail.com" \
  verify --server-dry-run \
  --inventory /tmp/findydevops-dev-inventory.yaml
```

Final IAP attachment is KC-25 because it requires the live ingress/backend service and OAuth
configuration. Applying the Executor admission policy or binding also remains a separate explicit
approval checkpoint.
