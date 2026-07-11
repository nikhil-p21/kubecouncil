import { useEffect, useState } from "react";

import "./App.css";

type Outcome = "not_started" | "proposal_ready" | "needs_more_evidence" | "no_safe_action" | "inconclusive";
type InterventionOutcome = "not_started" | "monitoring" | "succeeded" | "rolled_back" | "failed" | "safe_halted";
type SpecialistRole = "health" | "logs" | "metrics" | "change";

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
    action: {
      action_type: "rollback_deployment" | "scale_deployment" | "restart_deployment";
      target: { namespace: string; name: string };
      revision?: number;
      replicas?: number;
    };
    expected_impact: string;
    rollback_strategy: string;
  } | null;
  manual_guidance: { reason: string; guidance: string; outcome: Outcome } | null;
  audit_events: Array<{
    event_id: string;
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

  useEffect(() => {
    void loadManagedApplications();
  }, []);

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

  return (
    <main className="incident-shell">
      <header className="incident-masthead">
        <p className="eyebrow">KubeCouncil / Incident response</p>
        <h1>Operations desk</h1>
        <p className="lede">Enrollment readiness comes before the narrow, auditable incident path.</p>
      </header>

      <ManagedApplications applications={applications} message={applicationsMessage} />

      <section className="incident-control" aria-live="polite">
        <div>
          <strong>Local fake-backed workflow</strong>
          <p>{message}</p>
        </div>
        <button className="primary-button" disabled={opening} onClick={() => void openIncident()}>
          {opening ? "Opening…" : "Open fake incident"}
        </button>
      </section>

      {record ? (
        <IncidentDetail
          investigating={investigating}
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
  investigating,
  onInvestigate,
  record,
}: {
  investigating: boolean;
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
        {incident.investigation_outcome === "not_started" ? (
          <button disabled={investigating} onClick={() => void onInvestigate(incident.incident_id)}>
            {investigating ? "Investigating…" : "Run Council"}
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
            <dd>Intervention: {titleCase(incident.intervention_outcome)}</dd>
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

      {incident.investigation_outcome !== "not_started" ? <CouncilDetail record={record} /> : null}
    </section>
  );
}

const specialistRoles: SpecialistRole[] = ["health", "logs", "metrics", "change"];

function CouncilDetail({ record }: { record: IncidentRecord }) {
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

      <CouncilOutcome record={record} />
    </article>
  );
}

function CouncilOutcome({ record }: { record: IncidentRecord }) {
  const proposal = record.proposal;
  if (proposal) {
    const action = proposal.action;
    const actionDetail =
      action.action_type === "rollback_deployment"
        ? `revision ${action.revision}`
        : action.action_type === "scale_deployment"
          ? `${action.replicas} replicas`
          : "controlled rollout";
    return (
      <section className="council-outcome">
        <p className="eyebrow">Proposal Ready</p>
        <h3>
          {titleCase(action.action_type)} · {action.target.name} · {actionDetail}
        </h3>
        <p>{proposal.expected_impact}</p>
        <small>Failure strategy: {proposal.rollback_strategy}</small>
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
