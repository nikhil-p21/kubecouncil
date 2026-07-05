import { FormEvent, useMemo, useState } from "react";

import "./App.css";

const steps = ["Connect", "Analyse", "Build Twin", "Pressure", "Council", "Apply", "Verify", "PR"] as const;

type StepName = (typeof steps)[number];
type PhaseState = "idle" | "active" | "done" | "failed";

type ValidationResult = {
  status: "passed" | "failed";
  errors?: string[];
  warnings?: string[];
};

type RepositorySnapshot = {
  run_id: string;
  repository_url: string;
  ref: string;
  commit_sha: string;
  deployment_path: string;
};

type CompatibilityIssue = {
  severity: "info" | "warning" | "error";
  resource_kind: string;
  resource_name: string;
  message: string;
  source: string;
};

type ResourceRequests = {
  cpu_millis: number;
  memory_mib: number;
};

type HpaBounds = {
  min_replicas: number;
  max_replicas: number;
};

type ServiceProfile = {
  name: string;
  image: string;
  current_replicas: number;
  min_replicas: number;
  max_replicas: number;
  resource_requests: ResourceRequests;
  criticality: "critical" | "important" | "optional";
  dependencies: string[];
  degradation_modes: string[];
  optional: boolean;
  config_maps: string[];
  hpa?: HpaBounds | null;
};

type DependencyEdge = {
  from_service: string;
  to_service: string;
};

type AnalysisResult = {
  run_id: string;
  source: {
    rendered_resource_count: number;
  };
  services: ServiceProfile[];
  compatibility_issues: CompatibilityIssue[];
  dependency_edges: DependencyEdge[];
};

type RehearsalState = {
  run_id: string;
  namespace: string;
  status: "planned" | "deployed" | "failed" | "deleted";
  resources: Array<{ kind: string; name: string; namespace: string }>;
  readiness: ValidationResult;
  plan: {
    safety_substitutions: string[];
  };
  message?: string | null;
};

type LoadTestResult = {
  phase: "baseline" | "pressure" | "post_change";
  request_count: number;
  success_rate: number;
  p95_latency_ms: number;
  errors: string[];
  status?: "passed" | "failed" | null;
  failure_type?: "objective" | "infrastructure" | "malformed_output" | null;
};

type CouncilAction = {
  action_type: string;
  target_service: string;
  target_namespace: string;
  parameters: Record<string, string | number | boolean>;
  reason: string;
};

type ServiceProposal = {
  service_name: string;
  proposed_actions: CouncilAction[];
  rationale: string;
};

type CouncilPlan = {
  plan_id: string;
  run_id: string;
  namespace: string;
  actions: CouncilAction[];
  validation: ValidationResult;
  status: "valid" | "invalid" | "infeasible";
  representative_proposals: ServiceProposal[];
  repair_attempted: boolean;
  infeasible_reason?: string | null;
};

type ExperimentReport = {
  run_id: string;
  plan_id: string;
  status: "successful" | "unsuccessful" | "inconclusive";
  baseline: LoadTestResult;
  pressure_before: LoadTestResult;
  pressure_after: LoadTestResult;
  validation: ValidationResult;
  applied_actions: CouncilAction[];
  rollback_guidance: string;
};

type PullRequestResult = {
  run_id: string;
  branch_name: string;
  commit_sha: string;
  pr_url: string;
  draft: boolean;
  changed_files: string[];
};

type WorkflowData = {
  repository?: RepositorySnapshot;
  analysis?: AnalysisResult;
  rehearsal?: RehearsalState;
  baseline?: LoadTestResult;
  pressure?: LoadTestResult;
  council?: CouncilPlan;
  report?: ExperimentReport;
  pullRequest?: PullRequestResult;
};

type ApiError = {
  code: string;
  message: string;
};

type ConnectionForm = {
  repositoryUrl: string;
  ref: string;
  deploymentPath: string;
};

const initialForm: ConnectionForm = {
  repositoryUrl: "file:///absolute/path/to/demo-target.git",
  ref: "main",
  deploymentPath: "deploy/overlays/production",
};

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Unexpected workflow failure";
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  const payload = (await response.json().catch(() => null)) as unknown;
  if (!response.ok) {
    const detail = isErrorPayload(payload) ? payload.detail : undefined;
    const message = detail?.message ?? response.statusText;
    throw new Error(`${detail?.code ?? "request_failed"}: ${message}`);
  }
  return payload as T;
}

function isErrorPayload(payload: unknown): payload is { detail: ApiError } {
  return (
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload &&
    typeof (payload as { detail?: unknown }).detail === "object" &&
    (payload as { detail?: unknown }).detail !== null
  );
}

function actionSummary(action: CouncilAction): string {
  const parameters = Object.entries(action.parameters)
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(", ");
  return parameters ? `${action.action_type} (${parameters})` : action.action_type;
}

function formatPercent(value?: number): string {
  if (value === undefined) {
    return "Waiting";
  }
  return `${Math.round(value * 1000) / 10}%`;
}

function formatLatency(value?: number): string {
  if (value === undefined) {
    return "Waiting";
  }
  return `${Math.round(value)} ms`;
}

function totalCpu(services: ServiceProfile[] = []): number {
  return services.reduce(
    (total, service) => total + service.resource_requests.cpu_millis * service.current_replicas,
    0,
  );
}

function phaseForStep(step: StepName, data: WorkflowData, active: StepName | null, errorStep: StepName | null): PhaseState {
  if (errorStep === step) {
    return "failed";
  }
  if (active === step) {
    return "active";
  }
  switch (step) {
    case "Connect":
      return data.repository ? "done" : "idle";
    case "Analyse":
      return data.analysis ? "done" : "idle";
    case "Build Twin":
      return data.rehearsal?.status === "deployed" ? "done" : "idle";
    case "Pressure":
      return data.baseline && data.pressure ? "done" : "idle";
    case "Council":
      return data.council ? "done" : "idle";
    case "Apply":
      return data.report ? "done" : "idle";
    case "Verify":
      return data.report?.validation.status === "passed" ? "done" : "idle";
    case "PR":
      return data.pullRequest ? "done" : "idle";
  }
}

export function App() {
  const [form, setForm] = useState<ConnectionForm>(initialForm);
  const [data, setData] = useState<WorkflowData>({});
  const [activeStep, setActiveStep] = useState<StepName | null>(null);
  const [errorStep, setErrorStep] = useState<StepName | null>(null);
  const [message, setMessage] = useState("Connect a repository to begin the rehearsal.");

  const canAnalyse = Boolean(data.repository);
  const hasBlockingCompatibility = Boolean(
    data.analysis?.compatibility_issues.some((issue) => issue.severity === "error"),
  );
  const canBuildTwin = Boolean(data.analysis) && !hasBlockingCompatibility;
  const canRunPressure = data.rehearsal?.status === "deployed";
  const canRunCouncil = Boolean(data.pressure);
  const canApply = data.council?.status === "valid";
  const canVerify = Boolean(data.report);
  const canCreatePr = data.report?.status === "successful";

  const compatibilityStatus = useMemo(() => {
    const issues = data.analysis?.compatibility_issues ?? [];
    if (issues.some((issue) => issue.severity === "error")) {
      return "Blocked";
    }
    if (issues.length > 0) {
      return "Warnings";
    }
    return data.analysis ? "Clear" : "Waiting";
  }, [data.analysis]);

  async function runStep(step: StepName, work: () => Promise<void>): Promise<void> {
    setActiveStep(step);
    setErrorStep(null);
    setMessage(`${step} is running.`);
    try {
      await work();
      setMessage(`${step} completed.`);
    } catch (error) {
      setErrorStep(step);
      setMessage(getErrorMessage(error));
    } finally {
      setActiveStep(null);
    }
  }

  async function connect(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    await runStep("Connect", async () => {
      const repository = await requestJson<RepositorySnapshot>("/api/repositories/connect", {
        method: "POST",
        body: JSON.stringify({
          repository_url: form.repositoryUrl,
          ref: form.ref,
          deployment_path: form.deploymentPath,
        }),
      });
      setData({ repository });
    });
  }

  async function analyse(): Promise<void> {
    const runId = data.repository?.run_id;
    if (!runId) {
      return;
    }
    await runStep("Analyse", async () => {
      const analysis = await requestJson<AnalysisResult>(`/api/runs/${runId}/analyse`, {
        method: "POST",
      });
      setData((current) => ({ ...current, analysis }));
      const blocking = analysis.compatibility_issues.find((issue) => issue.severity === "error");
      if (blocking) {
        throw new Error(`compatibility_failed: ${blocking.message}`);
      }
    });
  }

  async function buildTwin(): Promise<void> {
    const runId = data.repository?.run_id;
    if (!runId) {
      return;
    }
    await runStep("Build Twin", async () => {
      const rehearsal = await requestJson<RehearsalState>(`/api/runs/${runId}/rehearsal`, {
        method: "POST",
      });
      setData((current) => ({ ...current, rehearsal }));
    });
  }

  async function runScenario(): Promise<void> {
    const runId = data.repository?.run_id;
    if (!runId) {
      return;
    }
    await runStep("Pressure", async () => {
      const baseline = await requestJson<LoadTestResult>(`/api/runs/${runId}/baseline`, {
        method: "POST",
      });
      const pressure = await requestJson<LoadTestResult>(`/api/runs/${runId}/pressure`, {
        method: "POST",
      });
      setData((current) => ({ ...current, baseline, pressure }));
    });
  }

  async function runCouncil(): Promise<void> {
    const runId = data.repository?.run_id;
    if (!runId) {
      return;
    }
    await runStep("Council", async () => {
      const council = await requestJson<CouncilPlan>(`/api/runs/${runId}/council`, {
        method: "POST",
      });
      setData((current) => ({ ...current, council }));
      if (council.status === "infeasible") {
        throw new Error(`council_infeasible: ${council.infeasible_reason ?? "No valid allocation exists"}`);
      }
      if (council.status !== "valid") {
        throw new Error(`council_invalid: ${council.validation.errors?.join(", ") ?? "Plan failed validation"}`);
      }
    });
  }

  async function applyPlan(): Promise<void> {
    const runId = data.repository?.run_id;
    const planId = data.council?.plan_id;
    if (!runId || !planId) {
      return;
    }
    await runStep("Apply", async () => {
      const report = await requestJson<ExperimentReport>(
        `/api/runs/${runId}/plans/${planId}/apply`,
        { method: "POST" },
      );
      setData((current) => ({ ...current, report }));
    });
  }

  async function verify(): Promise<void> {
    const runId = data.repository?.run_id;
    if (!runId) {
      return;
    }
    await runStep("Verify", async () => {
      const report = await requestJson<ExperimentReport>(`/api/runs/${runId}/verify`, {
        method: "POST",
      });
      setData((current) => ({ ...current, report }));
    });
  }

  async function createPullRequest(): Promise<void> {
    const runId = data.repository?.run_id;
    if (!runId) {
      return;
    }
    await runStep("PR", async () => {
      const pullRequest = await requestJson<PullRequestResult>(`/api/runs/${runId}/pull-request`, {
        method: "POST",
      });
      setData((current) => ({ ...current, pullRequest }));
    });
  }

  async function reset(): Promise<void> {
    const runId = data.repository?.run_id;
    if (runId && data.rehearsal?.status === "deployed") {
      await runStep("Build Twin", async () => {
        const rehearsal = await requestJson<RehearsalState>(`/api/runs/${runId}/rehearsal`, {
          method: "DELETE",
        });
        setData((current) => ({ ...current, rehearsal }));
      });
    }
    setData({});
    setErrorStep(null);
    setMessage("Reset complete. Connect a repository to begin again.");
  }

  return (
    <main className="shell">
      <header className="masthead" aria-labelledby="title">
        <div>
          <p className="eyebrow">Kubernetes rehearsal platform</p>
          <h1 id="title">KubeCouncil</h1>
        </div>
        <div className="run-card" aria-label="Current run">
          <span>Run</span>
          <strong>{data.repository?.run_id ?? "Not connected"}</strong>
          <button className="ghost-button" type="button" onClick={() => void reset()}>
            Reset
          </button>
        </div>
      </header>

      <ol className="stepper" aria-label="Workflow steps">
        {steps.map((step) => {
          const phase = phaseForStep(step, data, activeStep, errorStep);
          return (
            <li key={step} data-state={phase}>
              <span>{step}</span>
            </li>
          );
        })}
      </ol>

      <section className={errorStep ? "status-strip error" : "status-strip"} role="status">
        <strong>{activeStep ?? errorStep ?? "Ready"}</strong>
        <span>{message}</span>
      </section>

      <div className="workspace-grid">
        <section className="section connection-section" aria-labelledby="repository-heading">
          <div className="section-heading">
            <p className="eyebrow">Repository connection</p>
            <h2 id="repository-heading">Source overlay</h2>
          </div>
          <form className="connect-form" onSubmit={(event) => void connect(event)}>
            <label>
              GitHub repository URL
              <input
                value={form.repositoryUrl}
                onChange={(event) => setForm((current) => ({ ...current, repositoryUrl: event.target.value }))}
                placeholder="https://github.com/org/repo"
              />
            </label>
            <label>
              Branch
              <input
                value={form.ref}
                onChange={(event) => setForm((current) => ({ ...current, ref: event.target.value }))}
              />
            </label>
            <label>
              Deployment path
              <input
                value={form.deploymentPath}
                onChange={(event) => setForm((current) => ({ ...current, deploymentPath: event.target.value }))}
              />
            </label>
            <button className="primary-button" type="submit" disabled={activeStep !== null}>
              Connect
            </button>
          </form>
          <dl className="facts">
            <div>
              <dt>Commit SHA</dt>
              <dd>{data.repository?.commit_sha ?? "Waiting"}</dd>
            </div>
            <div>
              <dt>Compatibility</dt>
              <dd>{compatibilityStatus}</dd>
            </div>
            <div>
              <dt>Detected services</dt>
              <dd>{data.analysis?.services.length ?? 0}</dd>
            </div>
          </dl>
          <div className="button-row">
            <button type="button" onClick={() => void analyse()} disabled={!canAnalyse || activeStep !== null}>
              Analyse
            </button>
            <button type="button" onClick={() => void buildTwin()} disabled={!canBuildTwin || activeStep !== null}>
              Build Twin
            </button>
          </div>
        </section>

        <section className="section rehearsal-section" aria-labelledby="rehearsal-heading">
          <div className="section-heading">
            <p className="eyebrow">Rehearsal creation</p>
            <h2 id="rehearsal-heading">Isolated twin</h2>
          </div>
          <dl className="facts">
            <div>
              <dt>Namespace</dt>
              <dd>{data.rehearsal?.namespace ?? "Waiting"}</dd>
            </div>
            <div>
              <dt>Rendered resources</dt>
              <dd>{data.analysis?.source.rendered_resource_count ?? "Waiting"}</dd>
            </div>
            <div>
              <dt>Readiness</dt>
              <dd>{data.rehearsal?.readiness.status ?? "Waiting"}</dd>
            </div>
          </dl>
          <ul className="plain-list">
            {(data.rehearsal?.plan.safety_substitutions ?? ["Safety substitutions appear after twin creation."]).map(
              (substitution) => (
                <li key={substitution}>{substitution}</li>
              ),
            )}
          </ul>
        </section>

        <section className="section graph-section" aria-labelledby="graph-heading">
          <div className="section-heading">
            <p className="eyebrow">Service graph</p>
            <h2 id="graph-heading">Dependency council</h2>
          </div>
          <div className="service-map">
            {(data.analysis?.services ?? []).map((service) => (
              <article className="service-node" data-criticality={service.criticality} key={service.name}>
                <span>{service.criticality}</span>
                <strong>{service.name}</strong>
                <small>
                  {service.current_replicas} replicas, {service.resource_requests.cpu_millis}m CPU
                </small>
                <small>{service.dependencies.length ? `Needs ${service.dependencies.join(", ")}` : "No dependencies"}</small>
              </article>
            ))}
            {!data.analysis && <p className="empty-state">Analyse the overlay to reveal service dependencies.</p>}
          </div>
        </section>

        <section className="section scenario-section" aria-labelledby="scenario-heading">
          <div className="section-heading">
            <p className="eyebrow">Scenario panel</p>
            <h2 id="scenario-heading">Flash sale</h2>
          </div>
          <div className="button-row">
            <button type="button" onClick={() => void runScenario()} disabled={!canRunPressure || activeStep !== null}>
              Run Baseline + Pressure
            </button>
          </div>
          <div className="metric-pair">
            <Metric label="Baseline success" value={formatPercent(data.baseline?.success_rate)} />
            <Metric label="Pressure success" value={formatPercent(data.pressure?.success_rate)} />
            <Metric label="Baseline p95" value={formatLatency(data.baseline?.p95_latency_ms)} />
            <Metric label="Pressure p95" value={formatLatency(data.pressure?.p95_latency_ms)} />
          </div>
          <p className="fine-print">Current phase: {activeStep ?? "idle"} · rehearsal state: {data.rehearsal?.status ?? "none"}</p>
        </section>

        <section className="section council-section" aria-labelledby="council-heading">
          <div className="section-heading">
            <p className="eyebrow">Council panel</p>
            <h2 id="council-heading">Representative agreement</h2>
          </div>
          <div className="button-row">
            <button type="button" onClick={() => void runCouncil()} disabled={!canRunCouncil || activeStep !== null}>
              Run Council
            </button>
          </div>
          <div className="proposal-list">
            {(data.council?.representative_proposals ?? []).map((proposal) => (
              <article className="proposal" key={proposal.service_name}>
                <strong>{proposal.service_name}</strong>
                <span>{proposal.rationale}</span>
                <small>{proposal.proposed_actions.map(actionSummary).join("; ") || "No change proposed"}</small>
              </article>
            ))}
            {!data.council && <p className="empty-state">Representatives appear after pressure data is available.</p>}
          </div>
          <p className="fine-print">
            Validation: {data.council?.validation.status ?? "waiting"} · agreement: {data.council?.status ?? "waiting"}
          </p>
        </section>

        <section className="section kubernetes-section" aria-labelledby="kubernetes-heading">
          <div className="section-heading">
            <p className="eyebrow">Kubernetes panel</p>
            <h2 id="kubernetes-heading">Applied changes</h2>
          </div>
          <div className="button-row">
            <button type="button" onClick={() => void applyPlan()} disabled={!canApply || activeStep !== null}>
              Apply Plan
            </button>
            <button type="button" onClick={() => void verify()} disabled={!canVerify || activeStep !== null}>
              Verify
            </button>
          </div>
          <ul className="change-list">
            {(data.report?.applied_actions ?? data.council?.actions ?? []).map((action) => (
              <li key={`${action.action_type}-${action.target_service}`}>
                <strong>{action.target_service}</strong>
                <span>{actionSummary(action)}</span>
                <small>{action.reason}</small>
              </li>
            ))}
            {!data.council && <li>Replica, mode and suspension changes appear after council agreement.</li>}
          </ul>
          <p className="fine-print">Rollout status: {data.report?.validation.status ?? data.rehearsal?.readiness.status ?? "waiting"}</p>
        </section>

        <section className="section results-section" aria-labelledby="results-heading">
          <div className="section-heading">
            <p className="eyebrow">Results panel</p>
            <h2 id="results-heading">Before and after</h2>
          </div>
          <div className="metric-pair">
            <Metric label="Before success" value={formatPercent(data.report?.pressure_before.success_rate ?? data.pressure?.success_rate)} />
            <Metric label="After success" value={formatPercent(data.report?.pressure_after.success_rate)} />
            <Metric label="Before p95" value={formatLatency(data.report?.pressure_before.p95_latency_ms ?? data.pressure?.p95_latency_ms)} />
            <Metric label="After p95" value={formatLatency(data.report?.pressure_after.p95_latency_ms)} />
            <Metric label="Requested CPU" value={`${totalCpu(data.analysis?.services)}m`} />
            <Metric label="Objective" value={data.report?.status ?? "waiting"} />
          </div>
        </section>

        <section className="section pr-section" aria-labelledby="pr-heading">
          <div className="section-heading">
            <p className="eyebrow">Pull-request panel</p>
            <h2 id="pr-heading">Evidence branch</h2>
          </div>
          <div className="button-row">
            <button type="button" onClick={() => void createPullRequest()} disabled={!canCreatePr || activeStep !== null}>
              Open Draft PR
            </button>
          </div>
          <dl className="facts">
            <div>
              <dt>PR status</dt>
              <dd>{data.pullRequest ? (data.pullRequest.draft ? "Draft opened" : "Needs review") : "Waiting"}</dd>
            </div>
            <div>
              <dt>Branch</dt>
              <dd>{data.pullRequest?.branch_name ?? "Waiting"}</dd>
            </div>
          </dl>
          <ul className="plain-list">
            {(data.pullRequest?.changed_files ?? ["Changed files appear after successful verification."]).map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
          {data.pullRequest && (
            <a className="pr-link" href={data.pullRequest.pr_url}>
              {data.pullRequest.pr_url}
            </a>
          )}
        </section>
      </div>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
