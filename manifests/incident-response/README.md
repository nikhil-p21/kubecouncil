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
