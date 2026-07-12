# Online Boutique managed application

The demo overlay vendors GoogleCloudPlatform Online Boutique `v0.10.5` from its official release
manifest. The vendored file SHA-256 is
`3e4d7b4764a14bcf1b67251c044cdc2c72464548362413ed89c2a8613b43521f`.

The overlay removes the external LoadBalancer, retains the upstream steady load generator, enrolls
the `online-boutique` namespace, labels only executable application Deployments as managed, and
keeps `redis-cart` observable as a Protected Dependency. Demo fault values and scenario names are
not stored in this application layer.

Render with:

```bash
kubectl kustomize manifests/online-boutique/overlays/demo
```
