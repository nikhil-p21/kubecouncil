# KubeCouncil — Codex Project Instructions

## Project goal

Build KubeCouncil, a multi-agent Kubernetes rehearsal platform.

KubeCouncil connects to a GitHub repository containing Kubernetes manifests, creates a safe rehearsal twin in GKE, runs a pressure scenario, lets service representative agents negotiate resource allocation, verifies the proposed configuration, and opens an automated draft pull request containing only the validated deployment changes.

## Core product workflow

1. Connect to a GitHub repository.
2. Clone a specified branch into an ephemeral workspace.
3. Locate and render a Kustomize deployment.
4. Analyse the rendered Kubernetes resources.
5. Build structured service profiles.
6. Generate an isolated rehearsal overlay.
7. Validate the overlay before deployment.
8. Deploy it to a temporary GKE namespace.
9. Run baseline and pressure tests.
10. Run the multi-agent council.
11. Validate the council plan deterministically.
12. Apply approved actions only to the rehearsal namespace.
13. Repeat the pressure test.
14. Compare before and after results.
15. Generate repository changes from the successful plan.
16. Validate the repository again.
17. Commit the changes to a new branch.
18. Push the branch.
19. Open a draft GitHub pull request.
20. Clean up the rehearsal namespace and workspace.

## Runtime stack

* Python 3.12
* FastAPI
* Pydantic
* Google ADK
* Gemini
* Kubernetes Python client
* GKE
* Kustomize
* k6
* React
* TypeScript
* GitHub REST API or GitHub CLI
* pytest

## Repository layout

```text
kubecouncil/
├── AGENTS.md
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── agents/
│   │   ├── domain/
│   │   ├── repositories/
│   │   ├── kubernetes/
│   │   ├── rehearsal/
│   │   ├── scenarios/
│   │   ├── pull_requests/
│   │   └── services/
│   └── tests/
├── frontend/
├── demo-target/
├── manifests/
├── load-tests/
├── scripts/
├── docs/
└── .scratch/
    └── issues/
```

## Architecture rules

Use interfaces around external systems.

Required interfaces:

* `RepositoryProvider`
* `KubernetesClient`
* `ManifestRenderer`
* `RunStore`
* `LoadTestRunner`
* `PullRequestProvider`
* `CouncilRunner`

Provide fake or local implementations so most tests do not require GitHub, Gemini or GKE.

Keep domain models independent from FastAPI, GitHub and Kubernetes SDK objects.

Do not pass free-form prose between agents when structured data is possible.

All agent outputs must be parsed into Pydantic models.

## Supported deployment format

The MVP supports Kustomize only.

A target repository must provide a `kustomization.yaml` at the configured deployment path.

Do not add Helm support unless every required phase is already complete.

## Supported Kubernetes resources

Allowed:

* Namespace
* Deployment
* Service
* ConfigMap
* HorizontalPodAutoscaler
* PodDisruptionBudget
* ResourceQuota
* NetworkPolicy
* Job

Unsupported in the MVP:

* StatefulSet
* PersistentVolume
* PersistentVolumeClaim
* CustomResourceDefinition
* production Secret copying
* cluster-scoped resources
* arbitrary admission controllers
* production database cloning

Unsupported resources must produce a compatibility report rather than silently being ignored.

## Safety invariants

These rules must never be bypassed by an LLM:

1. KubeCouncil must never modify the source or production namespace.
2. Kubernetes writes are allowed only in namespaces beginning with `kc-rehearsal-`.
3. Production Secrets must never be copied.
4. The agent may use only explicitly allowlisted actions.
5. Critical services may not be suspended.
6. Services may not be scaled below their configured minimum.
7. Total resource requests must remain within rehearsal quota.
8. Repository changes may modify only allowlisted deployment fields.
9. Pull requests must always be opened as drafts.
10. KubeCouncil must never merge its own pull request.
11. Failed or inconclusive rehearsals must not produce configuration PRs.
12. Every applied action must be recorded and reversible.

## Allowed council actions

* `scale_deployment`
* `set_hpa_bounds`
* `set_resource_requests`
* `set_config_mode`
* `suspend_optional_deployment`
* `restore_deployment`

Do not give agents arbitrary shell or kubectl access.

## Pull-request rules

The successful rehearsal plan must be translated into source-controlled Kubernetes changes.

The pull request must contain:

* objective;
* scenario;
* baseline metrics;
* post-change metrics;
* modified services;
* reasons for each modification;
* validation results;
* known limitations;
* rollback guidance.

Rehearsal-only resources such as temporary namespaces, mock services and synthetic credentials must not be added to the production overlay.

## Coding rules

* Use Python type annotations.
* Use Pydantic for external and agent-facing contracts.
* Keep functions small and focused.
* Avoid global mutable state.
* Use dependency injection for external systems.
* Add descriptive error types.
* Do not catch broad exceptions without re-raising or logging context.
* Do not introduce a dependency without a clear need.
* Prefer deterministic code over LLM calls wherever possible.

## Testing rules

Every issue must add or update tests.

Minimum required test categories:

* unit tests for domain logic;
* manifest fixture tests;
* repository-provider tests using temporary local Git repositories;
* fake Kubernetes adapter tests;
* agent structured-output parsing tests;
* safety-policy tests;
* one end-to-end rehearsal smoke test.

No test should require real GitHub or GKE unless explicitly marked `integration`.

## Commands

Backend:

```bash
cd backend
python -m pytest
python -m ruff check .
python -m mypy app
```

Frontend:

```bash
cd frontend
npm test
npm run lint
npm run build
```

Full verification:

```bash
make verify
```

## Issue execution rules

Read all files under `.scratch/issues/`.

Select the lowest-numbered issue with:

* `status: AFK`
* all dependencies completed.

Implement only that issue.

Before finishing:

1. run relevant tests;
2. run formatting and lint checks;
3. update the issue status;
4. record files changed;
5. record tests executed;
6. create one focused Git commit.

Do not start the next phase in the same task.

When blocked by credentials, cloud access or a product decision, mark the issue `BLOCKED` and describe the exact manual action required.

## Definition of done

A task is complete only when:

* the implementation exists;
* tests pass;
* unsafe failure paths are handled;
* public interfaces are documented;
* the issue file is updated;
* a focused commit has been created.
