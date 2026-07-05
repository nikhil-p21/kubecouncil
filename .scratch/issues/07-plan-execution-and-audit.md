# `.scratch/issues/07-plan-execution-and-audit.md`

---

id: KC-07
status: DONE
depends_on: [KC-06]
------------------

## Objective

Apply validated plans to the rehearsal twin and prove whether they work.

## Build

Implement `CouncilPlanExecutor`.

Before applying actions:

* save replicas;
* save HPA bounds;
* save resource requests;
* save relevant ConfigMap values.

Apply actions one at a time.

After each action:

* record the change;
* wait for rollout when applicable;
* stop on failure.

After the plan:

1. rerun the same pressure scenario;
2. compare baseline, pressure-before and pressure-after;
3. run the Auditor Agent;
4. produce `ExperimentReport`.

Automatic rollback conditions:

* rollout failure;
* success rate decreases;
* critical journey fails;
* resource quota violation;
* auditor detects a severe regression.

Create APIs:

```text
POST /api/runs/{run_id}/council
POST /api/runs/{run_id}/plans/{plan_id}/apply
POST /api/runs/{run_id}/verify
POST /api/runs/{run_id}/rollback
```

## Acceptance criteria

* Successful plans visibly change rehearsal workloads.
* The same pressure test runs before and after.
* The result includes a clear metric comparison.
* Failed plans roll back to the snapshot.
* No repository changes occur in this phase.

## Tests

* Action ordering.
* snapshot creation.
* partial failure rollback.
* metric comparison.
* unsuccessful plan rejection.
* successful experiment report.

## Commit

`feat: execute and audit council plans in rehearsal`

## Implementation summary

Implemented plan execution and audit for rehearsal namespaces:

* added reversible workload snapshot models for replicas, HPA bounds, resource requests and ConfigMap values;
* extended the Kubernetes interface and fake adapter with snapshot, apply-action and rollback operations;
* implemented guarded kubectl mutations for allowlisted council actions only inside `kc-rehearsal-*` namespaces;
* added `CouncilPlanExecutor` to validate plans, snapshot state, apply actions one at a time, rerun the pressure test, run the auditor and roll back unsafe outcomes;
* added run APIs for council generation, plan application, verification result retrieval and rollback;
* persisted council plans, snapshots, post-change results and experiment reports in the run store;
* fixed project-wide mypy verification by casting YAML output and adding a narrow `yaml` missing-stub override.

No repository source changes or pull-request generation were added in this phase.

## Files changed

* `backend/app/api/runs.py`
* `backend/app/domain/__init__.py`
* `backend/app/domain/fakes.py`
* `backend/app/domain/interfaces.py`
* `backend/app/domain/models.py`
* `backend/app/kubernetes/client.py`
* `backend/app/rehearsal/executor.py`
* `backend/app/scenarios/k6.py`
* `backend/pyproject.toml`
* `backend/tests/test_domain_models.py`
* `backend/tests/test_fakes.py`
* `backend/tests/test_plan_execution.py`
* `.scratch/issues/07-plan-execution-and-audit.md`

## Commands and tests run

* `cd backend && python -m pytest tests/test_plan_execution.py`
* `cd backend && python -m pytest tests/test_plan_execution.py tests/test_council.py tests/test_scenarios.py tests/test_domain_models.py tests/test_fakes.py`
* `cd backend && python -m pytest tests/test_plan_execution.py tests/test_domain_models.py tests/test_fakes.py`
* `cd backend && python -m compileall app`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app/rehearsal/executor.py app/kubernetes/client.py app/domain`
* `cd backend && python -m pytest`
* `cd backend && python -m mypy app`
* `make verify`

## Remaining limitations

* Real Kubernetes, k6 and Gemini/Vertex execution were not exercised; tests use fakes and existing command-runner seams.
* `set_config_mode` applies to the conventional `<service>-config` ConfigMap and `MODE` key used by the MVP demo target.
* `POST /api/runs/{run_id}/verify` returns the persisted experiment report produced by plan application; it does not start an additional independent verification run.
