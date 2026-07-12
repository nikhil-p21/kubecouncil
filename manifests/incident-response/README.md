# Incident-response deployment inputs

`bootstrap/` contains only non-secret, idempotent environment resources shared by the
KC-24 Online Boutique and KC-25 KubeCouncil deployments. It creates or reconciles the two
control namespaces, their distinct Workload Identity Kubernetes service accounts, and a
sanitized environment ConfigMap with immutable backend and frontend image references.

It intentionally contains no workload RBAC, admission-policy binding, ingress, OAuth
configuration, Kubernetes Secret, Online Boutique resource, or legacy rehearsal authority.
Those resources belong to their later implementation slices and require their own safety
review.

Render locally:

```bash
kubectl kustomize manifests/incident-response/bootstrap
```

Validate against the selected cluster without persisting changes:

```bash
kubectl apply -k manifests/incident-response/bootstrap --server-side --dry-run=server
```

## KC-25 platform layers

`platform/overlays/findydevops-dev` composes the bootstrap identities, Online Boutique demo,
Investigator/API, no-ADK Executor, non-root UI, custom-OAuth IAP BackendConfigs, least-privilege
Enrollment reads, an environment-specific Google-managed TLS certificate, and the Executor
admission boundary. The operator must provision `kubecouncil-iap-oauth` with `client_id` and
`client_secret` keys because external Google accounts cannot use the organization-restricted
Google-managed OAuth client. The public demo hostname is scoped to this overlay; no TLS key, OAuth
client secret, or Kubernetes Secret is stored in the repository. See
`docs/runbooks/layered-gke-deployment.md` for apply, readiness, IAP, negative enforcement, and live
Council verification.
