# KubeCouncil Deployment Manifests

These manifests are intentionally credential-free. Before applying them, create:

* a GKE Workload Identity mapping for the `kubecouncil` ServiceAccount;
* a `kubecouncil-deployment-config` ConfigMap containing `GOOGLE_CLOUD_PROJECT`;
* a `kubecouncil-credentials` Secret containing `GITHUB_TOKEN`, or configure a GitHub App integration in the backend.

Replace `PROJECT_ID` and `TAG` through your deployment pipeline or a Kustomize overlay.

The backend ServiceAccount can create and delete namespaces because rehearsals are isolated in `kc-rehearsal-*` namespaces. Application code still enforces the same prefix guard before every Kubernetes write.
