# KubeCouncil Demo Target

This directory is an external-style Kubernetes repository for KubeCouncil to analyse.

It contains one configurable FastAPI application image that runs five logical services:

* `gateway`
* `checkout`
* `payment`
* `recommendation`
* `analytics-worker`

The request path is:

```text
gateway -> checkout -> payment
                    -> recommendation
```

`analytics-worker` consumes CPU but is not part of checkout. `recommendation` supports
`MODE=live` and `MODE=cached`; cached mode lowers latency and CPU pressure.

## Render

```bash
kubectl kustomize demo-target/deploy/overlays/production
```

## GKE Image

The base manifests use the local image name `kubecouncil-demo:latest` only as the Kustomize
image selector. Before applying the production overlay to GKE, replace it with a pullable
registry image:

```bash
docker build -t "$DEMO_IMAGE" demo-target/app
docker push "$DEMO_IMAGE"
cd demo-target/deploy/overlays/production
kustomize edit set image "kubecouncil-demo=$DEMO_IMAGE"
kubectl kustomize .
```

For Artifact Registry, `DEMO_IMAGE` should be a fully qualified image reference such as:

```text
us-docker.pkg.dev/PROJECT_ID/REPOSITORY/kubecouncil-demo:TAG
```

Do not deploy the `replace-me` placeholder from the committed overlay. KubeCouncil rehearsal
overlays must set the same image replacement before creating GKE workloads; otherwise GKE nodes
will not be able to pull the demo application.

## Pressure Profile

The production overlay starts close to the rehearsal quota:

* requested CPU is approximately `3000m`;
* quota is `3200m`;
* checkout has limited headroom during a flash sale;
* analytics holds `600m` even though it is optional;
* recommendation runs in `live` mode with higher CPU and latency.

Baseline traffic uses 5 virtual users and should pass. Pressure traffic uses 40 virtual users
and is expected to degrade checkout because checkout, payment and recommendation compete for
limited CPU.

## Manual Optimized Configuration

For a successful rehearsal, apply these source-level changes in a separate branch:

* set `analytics-worker` replicas to `0`;
* set `recommendation-config` `MODE` to `cached`;
* lower recommendation CPU request from `300m` to `150m`;
* raise checkout replicas from `2` to `3` if quota allows.

These changes release optional/background capacity and shift it to the checkout path while
preserving payment availability.
