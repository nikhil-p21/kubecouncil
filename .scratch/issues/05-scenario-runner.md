# `.scratch/issues/05-scenario-runner.md`

---

id: KC-05
status: DONE
depends_on: [KC-04]
------------------

## Objective

Run repeatable baseline and pressure scenarios against the rehearsal twin.

## Build

Implement a k6 Kubernetes Job runner.

Create the scenario:

```yaml
name: flash-sale-fixed-capacity
baseline_virtual_users: 5
pressure_virtual_users: 40
duration_seconds: 45
objective:
  success_rate_minimum: 0.95
  p95_latency_ms_maximum: 2000
```

The runner must:

1. create a Job inside the rehearsal namespace;
2. wait for completion;
3. capture logs;
4. parse machine-readable k6 output;
5. return `LoadTestResult`;
6. delete completed Jobs;
7. persist run state.

Create APIs:

```text
POST /api/runs/{run_id}/baseline
POST /api/runs/{run_id}/pressure
GET  /api/runs/{run_id}/results
```

## Acceptance criteria

* Baseline passes reliably.
* Pressure test exposes a measurable failure.
* Results include request count, success rate, P95 latency and errors.
* Three consecutive runs are directionally consistent.
* Test failures are distinguished from infrastructure failures.

## Tests

* k6 output parsing.
* Job status transitions.
* timeout handling.
* malformed log handling.
* result objective evaluation.

## Commit

`feat: run measurable rehearsal pressure scenarios`

## Implementation summary

Implemented repeatable baseline and pressure scenario execution with:

* the fixed `flash-sale-fixed-capacity` scenario definition;
* `KubectlK6LoadTestRunner`, which creates a k6 ConfigMap and Kubernetes Job inside only rehearsal namespaces;
* Job completion waiting, log capture, k6 JSON summary parsing and Job/ConfigMap cleanup;
* objective evaluation that distinguishes failed objectives from infrastructure and malformed-output failures;
* persisted scenario state through `RunStore`;
* `POST /api/runs/{run_id}/baseline`;
* `POST /api/runs/{run_id}/pressure`;
* `GET /api/runs/{run_id}/results`;
* focused parser, command transition, timeout, malformed-log, namespace guard and API persistence tests.

## Files changed

* `backend/app/api/runs.py`
* `backend/app/domain/__init__.py`
* `backend/app/domain/models.py`
* `backend/app/scenarios/k6.py`
* `backend/tests/test_domain_models.py`
* `backend/tests/test_scenarios.py`
* `.scratch/issues/05-scenario-runner.md`

## Commands and tests run

* `cd backend && python -m pytest tests/test_scenarios.py tests/test_domain_models.py tests/test_fakes.py`
* `cd backend && python -m pytest`
* `cd backend && python -m ruff check .`
* `cd backend && python -m mypy app`
* `cd backend && python -m compileall app`
* `make verify`

## Remaining limitations

* Real k6 execution requires a configured Kubernetes context and pull access to the configured k6 image.
* The runner targets the internal `gateway` Service at `http://gateway`, matching the MVP demo target.
* Three-run consistency is supported by repeatable endpoints and persisted phase results, but no aggregate consistency report is introduced in this phase.
