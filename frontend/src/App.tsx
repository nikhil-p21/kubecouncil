import { useEffect, useState } from "react";

import "./App.css";

type Outcome = "not_started" | "proposal_ready" | "needs_more_evidence" | "no_safe_action" | "inconclusive";
type InterventionOutcome = "not_started" | "monitoring" | "succeeded" | "rolled_back" | "failed" | "safe_halted";
type InterventionState = "pending" | "running" | "succeeded" | "rolled_back" | "failed" | "safe_halted";
type SpecialistRole = "health" | "logs" | "metrics" | "change";
type OperatorIdentity = { principal: string; subject: string; role: "viewer" | "responder" };

type ApprovalBinding = {
  incident_version: number;
  proposal_hash: string;
  evidence_hash: string;
  workload_resource_version: string;
  workload_generation: number;
  workload_revision: number;
  policy_hash: string;
  dry_run_hash: string;
  recovery_criteria_hash: string;
  failure_strategy_hash: string;
  expires_at: string;
};

type ApprovalReview = {
  incident_id: string;
  proposal_id: string;
  responder_principal: string;
  binding: ApprovalBinding;
};

type IncidentRecord = {
  incident: {
    incident_id: string;
    application_id: string;
    profile_version: string;
    opened_at: string;
    lifecycle: string;
    investigation_outcome: Outcome;
    intervention_outcome: InterventionOutcome;
    version: number;
    summary: string;
  };
  application_profile: {
    display_name: string;
    namespace: string;
    workloads: Array<{ reference: { name: string }; executable: boolean; protected_dependency: boolean }>;
    observability_links: Array<{ label: string; source: string; url: string }>;
  };
  evidence_window: { started_at: string; ended_at: string; captured_at: string };
  alert_signals: Array<{ notification_id: string; provider_state: "open" | "closed" }>;
  evidence: Array<{
    evidence_id: string;
    source: string;
    query: string;
    query_reference: string;
    evidence_query_id?: string | null;
    evidence_window_id: string;
    observed_at: string;
    scope: { namespace: string; name: string };
    redacted_excerpt: string;
    content_hash: string;
    truncated: boolean;
    provider_reference: string;
  }>;
  evidence_retrieval_failures: Array<{
    failure_id: string;
    source: string | null;
    query: string | null;
    scope: { name: string } | null;
    occurred_at: string;
    message: string;
  }>;
  findings: Array<{
    finding_id: string;
    specialist: SpecialistRole;
    citations: Array<{ evidence_id: string; observation: string }>;
    candidate_explanations: string[];
    confidence: number;
    contradictions: string[];
    unknowns: string[];
  }>;
  model_invocations: Array<{
    invocation_id: string;
    role: SpecialistRole | "coordinator";
    model_id: string;
    prompt_version: string;
    thinking_level: string;
    latency_ms: number;
    input_tokens: number;
    output_tokens: number;
    tool_count: number;
    output_valid: boolean;
    failure_reason: string | null;
  }>;
  hypotheses: Array<{
    hypothesis_id: string;
    rank: number;
    statement: string;
    falsification_test: string;
    confidence: number;
    citations: Array<{ evidence_id: string; observation: string }>;
  }>;
  proposal: {
    proposal_id: string;
    action: {
      action_type: "rollback_deployment" | "scale_deployment" | "restart_deployment";
      target: { namespace: string; name: string };
      revision?: number;
      replicas?: number;
    };
    expected_impact: string;
    recovery_criteria: {
      critical_journey_name?: string;
      required_stable_windows?: number;
      stabilization_window_seconds?: number;
    };
    rollback_strategy: string;
    known_risks: string[];
  } | null;
  manual_guidance: { reason: string; guidance: string; outcome: Outcome } | null;
  policy_decision: {
    status: "passed" | "rejected" | "dry_run_failed";
    checks: Array<{ code: string; passed: boolean; message: string }>;
    dry_run_diff: string | null;
    rejection_reason: string | null;
    workload_resource_version: string | null;
    workload_generation: number | null;
    workload_revision: number | null;
  } | null;
  approvals: Array<{
    approval_id: string;
    responder_principal: string;
    decision: "approved" | "rejected";
    decided_at: string;
  }>;
  interventions: Array<{
    intervention_id: string;
    target: { namespace: string; name: string };
    state: InterventionState;
    requested_at: string;
  }>;
  recovery_assessments: Array<{
    intervention_id: string;
    window_started_at: string;
    window_ended_at: string;
    observed_at: string;
    generation: number;
    observed_generation: number;
    active_revision: number;
    desired_replicas: number;
    updated_replicas: number;
    available_replicas: number;
    unavailable_replicas: number;
    oom_terminations: number;
    restart_delta: number;
    kubernetes_converged: boolean;
    symptoms_cleared: boolean;
    journey_name: string;
    criteria_satisfied: boolean;
    request_count: number;
    success_rate: number | null;
    p95_latency_ms: number | null;
    traffic_sufficient: boolean;
    availability_satisfied: boolean;
    latency_satisfied: boolean;
    synthetic_probe_used: boolean;
    synthetic_probe_successes: number | null;
    sufficient_evidence: boolean;
    stable_windows: number;
    required_stable_windows: number;
    explanation: string;
  }>;
  audit_events: Array<{
    event_id: string;
    incident_id: string;
    event_type: string;
    occurred_at: string;
    actor: string;
    cursor: number;
    details?: Record<string, string>;
  }>;
};

type ApiError = { detail?: { code?: string; message?: string } };

type EnrollmentCheck = { code: string; message: string; passed: boolean };

type ManagedApplication = {
  application_profile: {
    application_id: string;
    display_name: string;
    namespace: string;
    workloads: Array<{ reference: { name: string }; executable: boolean; protected_dependency: boolean }>;
  } | null;
  profile_load: {
    application_id: string | null;
    valid: boolean;
    errors: Array<{ location: string; message: string }>;
  };
  enrollment: { ready: boolean; failed_checks: EnrollmentCheck[] };
  health: { status: string; message: string };
  incident_count: number;
};

class ApiRequestError extends Error {}

function titleCase(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function interventionLabel(value: InterventionOutcome | InterventionState): string {
  return value === "safe_halted" ? "Safely Halted" : titleCase(value);
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
  } catch (error) {
    throw new ApiRequestError("network_error: unable to reach the incident API", { cause: error });
  }

  let payload: T | ApiError;
  try {
    payload = (await response.json()) as T | ApiError;
  } catch (error) {
    throw new ApiRequestError("invalid_response: incident API returned invalid JSON", { cause: error });
  }
  if (!response.ok) {
    const detail = (payload as ApiError).detail;
    throw new ApiRequestError(
      `${detail?.code ?? "request_failed"}: ${detail?.message ?? response.statusText}`,
    );
  }
  return payload as T;
}

export function App() {
  const [record, setRecord] = useState<IncidentRecord | null>(null);
  const [message, setMessage] = useState("Open a local fake incident to inspect the incident-response record.");
  const [opening, setOpening] = useState(false);
  const [investigating, setInvestigating] = useState(false);
  const [applications, setApplications] = useState<ManagedApplication[]>([]);
  const [applicationsMessage, setApplicationsMessage] = useState("Loading Enrollment readiness.");
  const [identity, setIdentity] = useState<OperatorIdentity | null>(null);
  const [approvalReview, setApprovalReview] = useState<ApprovalReview | null>(null);
  const [deciding, setDeciding] = useState(false);

  useEffect(() => {
    void loadIdentity();
    void loadManagedApplications();
  }, []);

  useEffect(() => {
    if (
      !record ||
      identity?.role !== "responder" ||
      record.incident.lifecycle !== "awaiting_approval" ||
      record.approvals.length
    ) {
      setApprovalReview(null);
      return;
    }
    void loadApprovalReview(record.incident.incident_id);
  }, [identity?.role, record?.incident.incident_id, record?.incident.lifecycle, record?.approvals.length]);

  useEffect(() => {
    if (!record || typeof EventSource === "undefined") {
      return;
    }
    const cursor = Math.max(0, ...record.audit_events.map((event) => event.cursor));
    const source = new EventSource(
      `/api/incidents/${record.incident.incident_id}/events?after=${cursor}`,
    );
    const receiveTimelineEvent = (message: MessageEvent<string>) => {
      let event: IncidentRecord["audit_events"][number];
      try {
        event = JSON.parse(message.data) as IncidentRecord["audit_events"][number];
      } catch (error) {
        console.error("Ignored malformed timeline event", error);
        return;
      }
      setRecord((current) => {
        if (!current || current.audit_events.some((item) => item.cursor === event.cursor)) {
          return current;
        }
        return { ...current, audit_events: [...current.audit_events, event] };
      });
      if (event.event_type.startsWith("recovery_")) {
        void requestJson<IncidentRecord>(`/api/incidents/${event.incident_id}`)
          .then((updated) => setRecord(updated))
          .catch((error: unknown) => console.error("Recovery state refresh failed", error));
      }
    };
    source.addEventListener("timeline", receiveTimelineEvent as EventListener);
    return () => source.close();
  }, [record?.incident.incident_id]);

  async function loadManagedApplications(): Promise<void> {
    try {
      const loaded = await requestJson<ManagedApplication[]>("/api/applications");
      setApplications(loaded);
      setApplicationsMessage(loaded.length ? "Enrollment readiness is current." : "No Application Profiles loaded.");
    } catch (error) {
      console.error("Managed Application readiness load failed", error);
      setApplicationsMessage(
        error instanceof ApiRequestError ? error.message : "Unable to load Managed Applications.",
      );
    }
  }

  async function loadIdentity(): Promise<void> {
    try {
      setIdentity(await requestJson<OperatorIdentity>("/api/identity/me"));
    } catch (error) {
      setMessage(error instanceof ApiRequestError ? error.message : "Unable to verify operator identity.");
    }
  }

  async function loadApprovalReview(incidentId: string): Promise<void> {
    try {
      setApprovalReview(
        await requestJson<ApprovalReview>(`/api/incidents/${incidentId}/approval-review`),
      );
    } catch (error) {
      setApprovalReview(null);
      setMessage(
        error instanceof ApiRequestError ? error.message : "Unable to load current Approval context.",
      );
    }
  }

  async function openIncident(): Promise<void> {
    setOpening(true);
    setMessage("Opening a fake incident.");
    try {
      const created = await requestJson<IncidentRecord>("/api/incidents", {
        method: "POST",
        body: JSON.stringify({ summary: "recommendationservice OOMKilled during checkout" }),
      });
      setRecord(created);
      setMessage("Fake incident opened. Its redacted Evidence Window is ready for the Council.");
    } catch (error) {
      if (error instanceof ApiRequestError) {
        setMessage(error.message);
      } else {
        console.error("Unexpected fake incident open failure", error);
        setMessage("Unable to open the fake incident.");
      }
    } finally {
      setOpening(false);
    }
  }

  async function runCouncil(incidentId: string): Promise<void> {
    setInvestigating(true);
    setMessage("Four bounded Specialists are investigating in parallel.");
    try {
      const investigated = await requestJson<IncidentRecord>(
        `/api/incidents/${incidentId}/investigate`,
        { method: "POST" },
      );
      setRecord(investigated);
      setMessage("Council investigation completed with one consolidated outcome.");
    } catch (error) {
      setMessage(
        error instanceof ApiRequestError ? error.message : "Unable to complete the Council investigation.",
      );
    } finally {
      setInvestigating(false);
    }
  }

  async function decideProposal(decision: "approved" | "rejected"): Promise<void> {
    if (!record || !approvalReview) {
      return;
    }
    setDeciding(true);
    setMessage(`${titleCase(decision)} decision is being freshness-checked.`);
    try {
      const decided = await requestJson<IncidentRecord>(
        `/api/incidents/${record.incident.incident_id}/approval-decisions`,
        {
          method: "POST",
          body: JSON.stringify({ decision, reviewed_binding: approvalReview.binding }),
        },
      );
      setRecord(decided);
      setApprovalReview(null);
      setMessage(`Proposal ${decision} with an immutable Responder audit event.`);
    } catch (error) {
      setMessage(
        error instanceof ApiRequestError ? error.message : "Unable to record the proposal decision.",
      );
      await loadApprovalReview(record.incident.incident_id);
    } finally {
      setDeciding(false);
    }
  }

  async function closeIncident(incidentId: string, expectedVersion: number): Promise<void> {
    try {
      const closed = await requestJson<IncidentRecord>(`/api/incidents/${incidentId}/close`, {
        method: "POST",
        body: JSON.stringify({ expected_version: expectedVersion }),
      });
      setRecord(closed);
      setMessage("Incident closed by the authenticated Responder.");
    } catch (error) {
      setMessage(error instanceof ApiRequestError ? error.message : "Unable to close the Incident.");
    }
  }

  return (
    <main className="incident-shell">
      <header className="incident-masthead">
        <p className="eyebrow">KubeCouncil / Incident response</p>
        <h1>Operations desk</h1>
        <p className="lede">Enrollment readiness comes before the narrow, auditable incident path.</p>
        {identity ? (
          <p className="identity-chip">
            Authenticated {titleCase(identity.role)} · {identity.principal}
          </p>
        ) : (
          <p className="identity-chip pending">Verifying signed operator identity…</p>
        )}
      </header>

      <ManagedApplications applications={applications} message={applicationsMessage} />

      <section className="incident-control" aria-live="polite">
        <div>
          <strong>Local fake-backed workflow</strong>
          <p>{message}</p>
        </div>
        {identity?.role === "responder" ? (
          <button className="primary-button" disabled={opening} onClick={() => void openIncident()}>
            {opening ? "Opening…" : "Open fake incident"}
          </button>
        ) : null}
      </section>

      {record ? (
        <IncidentDetail
          investigating={investigating}
          identity={identity}
          approvalReview={approvalReview}
          deciding={deciding}
          onClose={closeIncident}
          onDecision={decideProposal}
          onInvestigate={runCouncil}
          record={record}
        />
      ) : (
        <EmptyIncidentState />
      )}
    </main>
  );
}

function ManagedApplications({ applications, message }: { applications: ManagedApplication[]; message: string }) {
  return (
    <section className="managed-applications" aria-live="polite">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Managed Applications</p>
          <h2>Enrollment readiness</h2>
        </div>
        <p>{message}</p>
      </div>
      <div className="application-list">
        {applications.map((application) => {
          const profile = application.application_profile;
          const name = profile
            ? profile.display_name
            : application.profile_load.valid
              ? application.profile_load.application_id ?? "Managed Application"
              : "Invalid Application Profile";
          return (
            <article className="application-card" key={application.profile_load.application_id ?? name}>
              <div className="application-title">
                <div>
                  <h3>{name}</h3>
                  <p>{profile ? `Namespace: ${profile.namespace}` : "Profile could not be loaded."}</p>
                </div>
                <span className={application.enrollment.ready ? "readiness ready" : "readiness blocked"}>
                  {application.enrollment.ready ? "Enrolled" : "Not ready"}
                </span>
              </div>
              <p className="health-placeholder">Health: {titleCase(application.health.status)} · {application.health.message}</p>
              <p className="incident-history">Incident history: {application.incident_count}</p>
              {profile ? (
                <ul className="plain-list workload-list">
                  {profile.workloads.map((workload) => (
                    <li key={workload.reference.name}>
                      {workload.reference.name}
                      {workload.protected_dependency ? " · protected dependency · observe only" : " · Managed Workload"}
                    </li>
                  ))}
                </ul>
              ) : null}
              {!profile ? (
                <ul className="profile-errors">
                  {application.profile_load.errors.map((error) => (
                    <li key={`${error.location}-${error.message}`}>
                      {error.location}: {error.message}
                    </li>
                  ))}
                </ul>
              ) : null}
              {!application.enrollment.ready ? (
                <ul className="readiness-failures">
                  {application.enrollment.failed_checks.map((check) => (
                    <li key={`${check.code}-${check.message}`}>{check.message}</li>
                  ))}
                </ul>
              ) : null}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function EmptyIncidentState() {
  return (
    <section className="incident-empty">
      <h2>No incident selected</h2>
      <p>
        The fake path opens an Online Boutique recommendationservice OOM incident. It records an immutable audit
        entry and keeps lifecycle, investigation, and intervention outcomes separate.
      </p>
    </section>
  );
}

function IncidentDetail({
  approvalReview,
  deciding,
  identity,
  investigating,
  onClose,
  onDecision,
  onInvestigate,
  record,
}: {
  approvalReview: ApprovalReview | null;
  deciding: boolean;
  identity: OperatorIdentity | null;
  investigating: boolean;
  onClose: (incidentId: string, expectedVersion: number) => Promise<void>;
  onDecision: (decision: "approved" | "rejected") => Promise<void>;
  onInvestigate: (incidentId: string) => Promise<void>;
  record: IncidentRecord;
}) {
  const { incident, application_profile: profile } = record;
  return (
    <section className="incident-grid">
      <article className="incident-card incident-summary">
        <p className="eyebrow">{profile.display_name}</p>
        <h2>{incident.summary}</h2>
        <p className="incident-id">{incident.incident_id}</p>
        {incident.investigation_outcome === "not_started" && identity?.role === "responder" ? (
          <button disabled={investigating} onClick={() => void onInvestigate(incident.incident_id)}>
            {investigating ? "Investigating…" : "Run Council"}
          </button>
        ) : null}
        {incident.lifecycle !== "closed" && identity?.role === "responder" ? (
          <button className="quiet-button" onClick={() => void onClose(incident.incident_id, incident.version)}>
            Close incident
          </button>
        ) : null}
        <dl className="status-dimensions">
          <div>
            <dt>Lifecycle</dt>
            <dd>Lifecycle: {titleCase(incident.lifecycle)}</dd>
          </div>
          <div>
            <dt>Investigation</dt>
            <dd>Investigation: {titleCase(incident.investigation_outcome)}</dd>
          </div>
          <div>
            <dt>Intervention</dt>
            <dd>Intervention: {interventionLabel(incident.intervention_outcome)}</dd>
          </div>
        </dl>
      </article>

      <article className="incident-card">
        <h2>Enrollment boundary</h2>
        <p>Namespace: {profile.namespace}</p>
        <ul className="plain-list">
          {profile.workloads.map((workload) => (
            <li key={workload.reference.name}>
              {workload.reference.name}
              {workload.protected_dependency ? " · protected dependency" : ""}
              {workload.executable ? " · executable" : " · observe only"}
            </li>
          ))}
        </ul>
        <div className="observability-links">
          <h3>Cloud observability</h3>
          <p>Open the provider-owned view for deeper investigation.</p>
          <ul className="plain-list">
            {profile.observability_links.map((link) => (
              <li key={`${link.source}-${link.url}`}>
                <a href={link.url} rel="noreferrer" target="_blank">
                  {link.label}
                </a>
              </li>
            ))}
          </ul>
        </div>
      </article>

      <article className="incident-card incident-timeline">
        <div className="timeline-heading">
          <h2>Audit timeline</h2>
          <span>Live · reconnectable</span>
        </div>
        <ol className="plain-list">
          {record.audit_events.map((event) => (
            <li key={event.event_id}>
              <strong>{event.event_type}</strong> · {event.actor} · {new Date(event.occurred_at).toLocaleString()}
              {event.details?.specialist ? ` · ${titleCase(event.details.specialist)} Specialist` : ""}
              {event.details?.reason ? ` · ${event.details.reason}` : ""}
              {event.details?.restoration ? ` · Restoration ${event.details.restoration}` : ""}
            </li>
          ))}
        </ol>
      </article>

      <article className="incident-card incident-evidence">
        <h2>Initial Evidence Window</h2>
        <p className="evidence-window">
          {new Date(record.evidence_window.started_at).toLocaleString()} — {new Date(record.evidence_window.ended_at).toLocaleString()}
        </p>
        <p className="evidence-window">Captured: {new Date(record.evidence_window.captured_at).toLocaleString()}</p>
        <ul className="plain-list evidence-list">
          {record.evidence.map((evidence) => (
            <li key={evidence.evidence_id}>
              <strong>
                {titleCase(evidence.source)} · {titleCase(evidence.query)} · {evidence.scope.name}
              </strong>
              <span>{evidence.redacted_excerpt}</span>
              <small>
                Scope: {evidence.scope.namespace}/{evidence.scope.name} · Observed: {new Date(evidence.observed_at).toLocaleString()}
                <br />
                Query: {evidence.query_reference} · Hash: {evidence.content_hash}
                {evidence.evidence_query_id ? ` · Audit query: ${evidence.evidence_query_id}` : ""}
                <br />
                {evidence.provider_reference}
                {evidence.truncated ? " · truncated to the Evidence Budget" : ""}
              </small>
            </li>
          ))}
        </ul>
        <h3>Evidence retrieval failures</h3>
        {record.evidence_retrieval_failures.length ? (
          <ul className="plain-list evidence-failures">
            {record.evidence_retrieval_failures.map((failure) => (
              <li key={failure.failure_id}>
                {failure.message}
                {failure.query ? ` · ${titleCase(failure.query)}` : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p className="evidence-window">None. Every initial retrieval completed safely.</p>
        )}
      </article>

      {record.recovery_assessments.length || incident.lifecycle === "monitoring" || incident.lifecycle === "resolved" ? (
        <RecoveryDetail record={record} />
      ) : null}

      {record.interventions.length || incident.intervention_outcome !== "not_started" ? (
        <InterventionDetail record={record} />
      ) : null}

      {incident.investigation_outcome !== "not_started" ? (
        <CouncilDetail
          approvalReview={approvalReview}
          deciding={deciding}
          identity={identity}
          onDecision={onDecision}
          record={record}
        />
      ) : null}
    </section>
  );
}

function InterventionDetail({ record }: { record: IncidentRecord }) {
  const latest = record.interventions.at(-1);
  const action = record.proposal?.action;
  const actionDetail = action
    ? action.action_type === "rollback_deployment"
      ? `Rollback to revision ${action.revision}`
      : action.action_type === "scale_deployment"
        ? `Scale to ${action.replicas} replicas`
        : "Controlled restart"
    : "No executable action recorded";
  return (
    <article className="incident-card intervention-detail">
      <div className="timeline-heading">
        <h2>Intervention execution</h2>
        <span>{interventionLabel(record.incident.intervention_outcome)}</span>
      </div>
      <p>{actionDetail}</p>
      {latest ? (
        <p>
          {latest.target.namespace}/{latest.target.name} · Executor state {interventionLabel(latest.state)}
        </p>
      ) : (
        <p>The approved mutation is awaiting a durable Executor record.</p>
      )}
      {record.incident.intervention_outcome === "monitoring" ? (
        <small>Mutation converged; deterministic recovery and stabilization are still in progress.</small>
      ) : null}
      {record.incident.intervention_outcome === "rolled_back" ? (
        <small>The failed scale was restored to its prior replica count after a fresh policy check.</small>
      ) : null}
      {record.incident.intervention_outcome === "failed" ? (
        <small>The action failed and requires operator escalation; no inverse action was invented.</small>
      ) : null}
      {record.incident.intervention_outcome === "safe_halted" ? (
        <small>Safety became stale or ambiguous, so the Executor stopped all further writes.</small>
      ) : null}
    </article>
  );
}

function RecoveryDetail({ record }: { record: IncidentRecord }) {
  const latest = record.recovery_assessments.at(-1);
  return (
    <article className="incident-card recovery-detail">
      <div className="timeline-heading">
        <h2>Recovery verification</h2>
        <span>{latest ? `${latest.stable_windows}/${latest.required_stable_windows} stable windows` : "Awaiting evidence"}</span>
      </div>
      <p>
        Mutation success remains in monitoring until Kubernetes, workload symptoms, and the Critical Journey stay healthy for the full Stabilization Window.
      </p>
      {record.recovery_assessments.length ? (
        <ol className="plain-list recovery-windows">
          {record.recovery_assessments.map((assessment) => (
            <li key={`${assessment.intervention_id}-${assessment.window_ended_at}`}>
              <strong>
                Window {assessment.stable_windows}/{assessment.required_stable_windows} · {assessment.criteria_satisfied ? "criteria satisfied" : "stabilization reset"}
              </strong>
              <span>
                Kubernetes {assessment.kubernetes_converged ? "converged" : "not converged"} · revision {assessment.active_revision} · generation {assessment.observed_generation}/{assessment.generation}
              </span>
              <span>
                Replicas {assessment.updated_replicas} updated, {assessment.available_replicas} available, {assessment.unavailable_replicas} unavailable of {assessment.desired_replicas} desired
              </span>
              <span>
                Workload symptoms {assessment.symptoms_cleared ? "cleared" : "still present"} · OOM terminations {assessment.oom_terminations} · restart delta {assessment.restart_delta}
              </span>
              <span>
                {titleCase(assessment.journey_name)} · {assessment.request_count} requests · availability {assessment.availability_satisfied ? "pass" : "insufficient"} · latency {assessment.latency_satisfied ? "pass" : "insufficient"}
              </span>
              {assessment.synthetic_probe_used ? (
                <span>Synthetic availability fallback · {assessment.synthetic_probe_successes} successful probes · latency remains insufficient</span>
              ) : null}
              <small>
                {new Date(assessment.window_started_at).toLocaleString()} — {new Date(assessment.window_ended_at).toLocaleString()} · {assessment.explanation}
              </small>
            </li>
          ))}
        </ol>
      ) : (
        <p className="evidence-window">No completed recovery window has been recorded.</p>
      )}
    </article>
  );
}

const specialistRoles: SpecialistRole[] = ["health", "logs", "metrics", "change"];

function CouncilDetail({
  approvalReview,
  deciding,
  identity,
  onDecision,
  record,
}: {
  approvalReview: ApprovalReview | null;
  deciding: boolean;
  identity: OperatorIdentity | null;
  onDecision: (decision: "approved" | "rejected") => Promise<void>;
  record: IncidentRecord;
}) {
  return (
    <article className="incident-card council-detail">
      <div className="timeline-heading">
        <h2>Council investigation</h2>
        <span>{titleCase(record.incident.investigation_outcome)}</span>
      </div>
      <div className="specialist-grid">
        {specialistRoles.map((role) => {
          const finding = record.findings.find((item) => item.specialist === role);
          const failure = [...record.model_invocations]
            .reverse()
            .find((item) => item.role === role && !item.output_valid);
          return (
            <section className="specialist-card" key={role}>
              <h3>{titleCase(role)} Specialist</h3>
              {finding ? (
                <>
                  <p className="confidence">{Math.round(finding.confidence * 100)}% confidence</p>
                  <ul className="plain-list compact-list">
                    {finding.candidate_explanations.map((explanation) => (
                      <li key={explanation}>{explanation}</li>
                    ))}
                  </ul>
                  <h4>Disagreements and unknowns</h4>
                  <ul className="plain-list compact-list">
                    {[...finding.contradictions, ...finding.unknowns].map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                  <small>
                    {finding.citations.map((citation) => citation.evidence_id).join(", ")}
                  </small>
                </>
              ) : (
                <p className="specialist-failure">
                  {failure?.failure_reason ?? "No validated Specialist Finding was recorded."}
                </p>
              )}
            </section>
          );
        })}
      </div>

      <section className="hypothesis-list">
        <h3>Ranked Root Cause Hypotheses</h3>
        {record.hypotheses.length ? (
          <ol className="plain-list">
            {record.hypotheses.map((hypothesis) => (
              <li key={hypothesis.hypothesis_id}>
                <strong>
                  Rank {hypothesis.rank} · {Math.round(hypothesis.confidence * 100)}% confidence
                </strong>
                <span>{hypothesis.statement}</span>
                <small>Falsify by: {hypothesis.falsification_test}</small>
              </li>
            ))}
          </ol>
        ) : (
          <p>No hypothesis passed structured validation.</p>
        )}
      </section>

      <CouncilOutcome
        approvalReview={approvalReview}
        deciding={deciding}
        identity={identity}
        onDecision={onDecision}
        record={record}
      />
    </article>
  );
}

function CouncilOutcome({
  approvalReview,
  deciding,
  identity,
  onDecision,
  record,
}: {
  approvalReview: ApprovalReview | null;
  deciding: boolean;
  identity: OperatorIdentity | null;
  onDecision: (decision: "approved" | "rejected") => Promise<void>;
  record: IncidentRecord;
}) {
  const proposal = record.proposal;
  if (proposal) {
    const action = proposal.action;
    const actionDetail =
      action.action_type === "rollback_deployment"
        ? `revision ${action.revision}`
        : action.action_type === "scale_deployment"
          ? `${action.replicas} replicas`
          : "controlled rollout";
    const policy = record.policy_decision;
    const policyPassed = policy?.status === "passed";
    const recovery = proposal.recovery_criteria;
    return (
      <section className={`council-outcome ${policyPassed ? "" : "policy-blocked"}`}>
        <p className="eyebrow">
          {policyPassed
            ? "Proposal Ready · Policy Passed"
            : policy
              ? "Manual Guidance · Policy Rejected"
              : "Proposal Pending Deterministic Policy"}
        </p>
        <h3>
          {titleCase(action.action_type)} · {action.target.name} · {actionDetail}
        </h3>
        <dl className="proposal-review">
          <div>
            <dt>Expected impact</dt>
            <dd>{proposal.expected_impact}</dd>
          </div>
          <div>
            <dt>Recovery Criteria</dt>
            <dd>
              {recovery.critical_journey_name
                ? `${titleCase(recovery.critical_journey_name)} · ${recovery.required_stable_windows} consecutive ${recovery.stabilization_window_seconds}-second windows`
                : "Use the Application Profile recovery contract."}
            </dd>
          </div>
          <div>
            <dt>Known risks</dt>
            <dd>{proposal.known_risks.length ? proposal.known_risks.join(" ") : "No additional risks declared."}</dd>
          </div>
          <div>
            <dt>Rollback behavior</dt>
            <dd>{proposal.rollback_strategy}</dd>
          </div>
        </dl>
        {policy ? (
          <div className="policy-result">
            <h4>Deterministic Policy Checks</h4>
            <ul className="plain-list compact-list">
              {policy.checks.map((check) => (
                <li className={check.passed ? "policy-pass" : "policy-fail"} key={check.code}>
                  {check.passed ? "Pass" : "Reject"} · {titleCase(check.code)} · {check.message}
                </li>
              ))}
            </ul>
            {policy.dry_run_diff ? (
              <p className="dry-run-diff">
                <strong>Server dry-run diff:</strong> {policy.dry_run_diff}
              </p>
            ) : null}
            {policy.rejection_reason ? (
              <p className="policy-rejection">Not executable: {policy.rejection_reason}</p>
            ) : null}
            {policy.workload_resource_version ? (
              <small>Bound workload resource version: {policy.workload_resource_version}</small>
            ) : null}
          </div>
        ) : (
          <p className="policy-rejection">Not approvable until deterministic policy and dry-run complete.</p>
        )}
        {record.approvals.length ? (
          <div className="approval-decision">
            <strong>
              {titleCase(record.approvals[0].decision)} by {record.approvals[0].responder_principal}
            </strong>
            <span>{new Date(record.approvals[0].decided_at).toLocaleString()}</span>
          </div>
        ) : policyPassed && identity?.role === "responder" && approvalReview ? (
          <div className="approval-panel">
            <h4>Freshness-bound Approval</h4>
            <p>
              Authenticated Responder · {approvalReview.responder_principal}
            </p>
            <p>
              Resource version {approvalReview.binding.workload_resource_version} · generation {approvalReview.binding.workload_generation} · revision {approvalReview.binding.workload_revision}
            </p>
            <small>Proposal hash: {approvalReview.binding.proposal_hash}</small>
            <small>Evidence hash: {approvalReview.binding.evidence_hash}</small>
            <small>Policy hash: {approvalReview.binding.policy_hash}</small>
            <small>Dry-run hash: {approvalReview.binding.dry_run_hash}</small>
            <small>Recovery hash: {approvalReview.binding.recovery_criteria_hash}</small>
            <small>Failure strategy hash: {approvalReview.binding.failure_strategy_hash}</small>
            <small>Review expires: {new Date(approvalReview.binding.expires_at).toLocaleString()}</small>
            <div className="approval-actions">
              <button disabled={deciding} onClick={() => void onDecision("approved")}>
                Approve proposal
              </button>
              <button className="reject-button" disabled={deciding} onClick={() => void onDecision("rejected")}>
                Reject proposal
              </button>
            </div>
          </div>
        ) : null}
      </section>
    );
  }
  if (record.manual_guidance) {
    return (
      <section className="council-outcome safe-refusal">
        <p className="eyebrow">Safe Refusal · {titleCase(record.manual_guidance.outcome)}</p>
        <h3>{record.manual_guidance.reason}</h3>
        <p>{record.manual_guidance.guidance}</p>
      </section>
    );
  }
  return (
    <section className="council-outcome inconclusive">
      <p className="eyebrow">Inconclusive</p>
      <p>The Council did not produce an executable action.</p>
    </section>
  );
}
