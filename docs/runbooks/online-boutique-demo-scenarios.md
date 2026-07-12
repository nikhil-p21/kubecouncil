# Online Boutique demo scenarios

This runbook deploys the KC-24 Managed Application and its isolated, demo-only Scenario
Controller. It does not deploy KubeCouncil itself, attach IAP, or install the cluster-scoped
Executor admission-policy binding; those are KC-25 responsibilities.

## Safety boundary

- The application namespace is exactly `online-boutique`.
- The controller runs as `kubecouncil-demo-control/scenario-controller`.
- Its Role permits `get` and `patch` only on `recommendationservice` and `redis-cart`.
- The controller accepts only two typed scenarios and uses resource-version preconditions.
- Recommendation injection atomically changes only the `server` container memory request and
  limit from the official safe values to the same empirically unsafe value. Lowering the request
  with the limit is required because Kubernetes rejects a limit below the existing request.
- Redis injection changes only `redis-cart.spec.replicas` from one to zero.
- Reset refuses unexpected live values instead of overwriting external changes.
- Scenario names and unsafe values stay in the demo-control deployment and audit logs. They do not
  appear in the Application Profile, Alert Signals, evidence mappings, or target annotations.

## Render and validate

From the repository root:

```bash
kubectl kustomize manifests/incident-response/demo > /tmp/kc24-demo.yaml

! grep -E \
  'PROJECT_ID|:TAG|replace-me|PRIVATE KEY|GITHUB_TOKEN|GOOGLE_APPLICATION_CREDENTIALS|kubecouncil-rehearsal-manager' \
  /tmp/kc24-demo.yaml
```

The vendored Online Boutique release is `v0.10.5`; its source checksum is recorded in
`manifests/online-boutique/README.md`. The upstream external LoadBalancer is removed. The upstream
load generator remains at ten users and one spawned user per second.

Because a server-side namespace dry-run does not persist the namespace for later objects in the
same request, validate and create the namespace first:

```bash
kubectl apply \
  -f manifests/online-boutique/overlays/demo/namespace.yaml \
  --server-side --dry-run=server
kubectl apply \
  -f manifests/online-boutique/overlays/demo/namespace.yaml \
  --server-side
```

Then validate and apply the complete demo layer:

```bash
kubectl apply -k manifests/incident-response/demo --server-side --dry-run=server
kubectl apply -k manifests/incident-response/demo --server-side

kubectl wait --namespace=online-boutique \
  --for=condition=Available deployment --all --timeout=10m
kubectl rollout status deployment/scenario-controller \
  --namespace=kubecouncil-demo-control --timeout=5m
```

## Access the internal controller

The controller has no external Service. In a separate operator terminal:

```bash
kubectl port-forward \
  --namespace=kubecouncil-demo-control \
  service/scenario-controller 18080:8000
```

Check readiness:

```bash
curl --fail --silent http://127.0.0.1:18080/ready
```

## Recommendation memory scenario

Record the healthy revision, memory request, and memory limit:

```bash
kubectl get deployment recommendationservice --namespace=online-boutique \
  -o jsonpath='{.metadata.generation}{" "}{.spec.template.spec.containers[?(@.name=="server")].resources.requests.memory}{" "}{.spec.template.spec.containers[?(@.name=="server")].resources.limits.memory}{"\n"}'
```

Inject the configured demo fault:

```bash
curl --fail --silent --request POST \
  http://127.0.0.1:18080/api/demo/scenarios/recommendation_oom/inject
```

Confirm that a new rollout exists and collect only observable symptoms:

```bash
kubectl get deployment,replicaset,pods --namespace=online-boutique \
  --selector=app=recommendationservice
kubectl get events --namespace=online-boutique \
  --field-selector=involvedObject.kind=Pod \
  --sort-by=.lastTimestamp
```

The exact unsafe resource value is a deployment parameter. Calibrate
`RECOMMENDATION_UNSAFE_MEMORY_REQUEST` and `RECOMMENDATION_UNSAFE_MEMORY_LIMIT` together, republish
the controller image, and repeat the server-side dry-run. Do not add scenario annotations or alert
text that discloses the injected cause.

Reset and wait for the known-good rollout:

```bash
curl --fail --silent --request POST \
  http://127.0.0.1:18080/api/demo/scenarios/recommendation_oom/reset
kubectl rollout status deployment/recommendationservice \
  --namespace=online-boutique --timeout=5m
```

## Protected Redis scenario

Inject the outage and verify only the protected Deployment changed:

```bash
curl --fail --silent --request POST \
  http://127.0.0.1:18080/api/demo/scenarios/redis_outage/inject
kubectl get deployment redis-cart --namespace=online-boutique \
  -o jsonpath='{.spec.replicas}{"\n"}'
```

Expected replicas: `0`. Reset and verify:

```bash
curl --fail --silent --request POST \
  http://127.0.0.1:18080/api/demo/scenarios/redis_outage/reset
kubectl rollout status deployment/redis-cart \
  --namespace=online-boutique --timeout=5m
```

## Audit and isolation checks

The controller audit endpoint and structured logs are demo-control records, not Investigation
Records:

```bash
curl --fail --silent http://127.0.0.1:18080/api/demo/audit
kubectl logs --namespace=kubecouncil-demo-control deployment/scenario-controller
```

Verify negative permissions:

```bash
kubectl auth can-i patch deployment/frontend \
  --namespace=online-boutique \
  --as=system:serviceaccount:kubecouncil-demo-control:scenario-controller
kubectl auth can-i get secrets \
  --namespace=online-boutique \
  --as=system:serviceaccount:kubecouncil-demo-control:scenario-controller
kubectl auth can-i patch deployment/recommendationservice \
  --namespace=online-boutique \
  --as=system:serviceaccount:kubecouncil-system:investigator
```

All three commands must print `no`. The positive checks for the two scenario targets must print
`yes` only for the Scenario Controller identity.

## Emergency reset

If the controller is unavailable, an authenticated cluster operator may restore the two known-good
values directly:

```bash
kubectl patch deployment recommendationservice --namespace=online-boutique \
  --type=strategic \
  --patch='{"spec":{"template":{"spec":{"containers":[{"name":"server","resources":{"requests":{"memory":"220Mi"},"limits":{"memory":"450Mi"}}}]}}}}'
kubectl scale deployment redis-cart --namespace=online-boutique --replicas=1
```

This operator recovery is demo reset, never a KubeCouncil Intervention, and must not be recorded as
KubeCouncil recovery.
