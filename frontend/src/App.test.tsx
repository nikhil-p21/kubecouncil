import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("opens and displays a fake incident with independent status dimensions", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/identity/me") {
        return jsonResponse(responderIdentity());
      }
      if (path === "/api/incidents") {
        return jsonResponse(incidentRecord());
      }
      if (path === "/api/applications") {
        return jsonResponse([managedApplication()]);
      }
      return jsonResponse([]);
    });
    renderApp();

    await act(async () => {});
    await act(async () => {
      buttonByName("Open fake incident").click();
    });

    expect(screenText()).toContain("recommendationservice OOMKilled during checkout");
    expect(screenText()).toContain("Lifecycle: Open");
    expect(screenText()).toContain("Investigation: Not Started");
    expect(screenText()).toContain("Intervention: Not Started");
    expect(screenText()).toContain("incident_opened");
    expect(screenText()).toContain("Initial Evidence Window");
    expect(screenText()).toContain("Cloud Logging · Pod Logs · recommendationservice");
    expect(screenText()).toContain("Scope: online-boutique/recommendationservice");
    expect(screenText()).toContain("Query: recommendationservice-logs · Hash: evidence-hash");
    expect(screenText()).toContain("fake://logging/recommendationservice");
    expect(screenText()).toContain("Evidence retrieval failures");
    expect(screenText()).toContain("redaction failed; evidence was not retained");
    expect(screenText()).toContain("Cloud observability");
    expect(linkByName("Online Boutique logs").href).toBe(
      "https://console.cloud.google.com/logs/query",
    );
  });

  it("shows Enrollment readiness and keeps a protected dependency observe-only", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      return jsonResponse(path === "/api/identity/me" ? responderIdentity() : [managedApplication()]);
    });
    renderApp();

    await act(async () => {});

    expect(screenText()).toContain("Online Boutique");
    expect(screenText()).toContain("Enrolled");
    expect(screenText()).toContain("redis-cart · protected dependency · observe only");
    expect(screenText()).toContain("Incident history: 1");
  });

  it("shows exact profile validation failures without claiming Enrollment", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/identity/me") {
        return jsonResponse(responderIdentity());
      }
      return jsonResponse([
        {
          application_profile: null,
          profile_load: {
            application_id: "broken-profile",
            valid: false,
            errors: [{ location: "workloads", message: "Field required" }],
          },
          enrollment: {
            ready: false,
            failed_checks: [{ code: "profile_valid", passed: false, message: "Field required" }],
          },
          health: { status: "unknown", message: "Health evidence has not been connected yet." },
          incident_count: 0,
        },
      ]);
    });
    renderApp();

    await act(async () => {});

    expect(screenText()).toContain("Invalid Application Profile");
    expect(screenText()).toContain("Not ready");
    expect(screenText()).toContain("Field required");
  });

  it("replays timeline events from the last cursor without duplicates", async () => {
    const eventSources: MockEventSource[] = [];
    vi.stubGlobal(
      "EventSource",
      class extends MockEventSource {
        constructor(url: string) {
          super(url);
          eventSources.push(this);
        }
      },
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/identity/me") {
        return jsonResponse(responderIdentity());
      }
      return jsonResponse(path === "/api/incidents" ? incidentRecord() : [managedApplication()]);
    });
    renderApp();

    await act(async () => {});
    await act(async () => buttonByName("Open fake incident").click());
    expect(eventSources[0]?.url).toBe("/api/incidents/inc-123/events?after=1");

    await act(async () => {
      eventSources[0]?.emit({
        event_id: "audit-2",
        event_type: "specialist_started",
        occurred_at: "2026-07-11T00:00:01Z",
        actor: "investigator",
        cursor: 2,
      });
      eventSources[0]?.emit({
        event_id: "audit-2",
        event_type: "specialist_started",
        occurred_at: "2026-07-11T00:00:01Z",
        actor: "investigator",
        cursor: 2,
      });
    });

    expect(screenText().match(/specialist_started/g)).toHaveLength(1);
  });

  it("runs the Council and shows findings, disagreements, hypotheses, and outcome", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/identity/me") {
        return jsonResponse(responderIdentity());
      }
      if (path === "/api/incidents") {
        return jsonResponse(incidentRecord());
      }
      if (path === "/api/incidents/inc-123/investigate") {
        return jsonResponse(investigatedIncidentRecord());
      }
      if (path === "/api/incidents/inc-123/approval-review") {
        return jsonResponse(approvalReview());
      }
      return jsonResponse([managedApplication()]);
    });
    renderApp();

    await act(async () => {});
    await act(async () => buttonByName("Open fake incident").click());
    await act(async () => buttonByName("Run Council").click());

    expect(screenText()).toContain("Council investigation");
    expect(screenText()).toContain("Health Specialist");
    expect(screenText()).toContain("Workload restarts align with the rollout");
    expect(screenText()).toContain("Disagreements and unknowns");
    expect(screenText()).toContain("Temporal correlation is not proof of causation");
    expect(screenText()).toContain("Rank 1 · 92% confidence");
    expect(screenText()).toContain("lower memory limit caused OOM terminations");
    expect(screenText()).toContain("Proposal Ready");
    expect(screenText()).toContain("Rollback Deployment · recommendationservice · revision 7");
    expect(screenText()).toContain("Policy Passed");
    expect(screenText()).toContain("Pass · Target Executable");
    expect(screenText()).toContain(
      "Server dry-run diff: Deployment/recommendationservice: revision 8 -> 7",
    );
    expect(screenText()).toContain("Recovery Criteria");
    expect(screenText()).toContain("Checkout · 2 consecutive 60-second windows");
    expect(screenText()).toContain("Known risks");
    expect(screenText()).toContain("Rollback behavior");
  });

  it("shows freshness-bound review context and submits one Responder decision", async () => {
    const requests: Array<{ path: string; init?: RequestInit }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        const path = typeof input === "string" ? input : input.toString();
        requests.push({ path, init });
        if (path === "/api/identity/me") {
          return jsonResponse(responderIdentity());
        }
        if (path === "/api/incidents") {
          return jsonResponse(incidentRecord());
        }
        if (path === "/api/incidents/inc-123/investigate") {
          return jsonResponse(investigatedIncidentRecord());
        }
        if (path === "/api/incidents/inc-123/approval-review") {
          return jsonResponse(approvalReview());
        }
        if (path === "/api/incidents/inc-123/approval-decisions") {
          return jsonResponse(approvedIncidentRecord());
        }
        return jsonResponse([managedApplication()]);
      },
    );
    renderApp();

    await act(async () => {});
    await act(async () => buttonByName("Open fake incident").click());
    await act(async () => buttonByName("Run Council").click());
    await act(async () => {});

    expect(screenText()).toContain("Authenticated Responder");
    expect(screenText()).toContain("responder@example.com");
    expect(screenText()).toContain("Resource version rv-8 · generation 8 · revision 8");
    expect(screenText()).toContain("Proposal hash: proposal-hash");
    expect(screenText()).toContain("Evidence hash: evidence-window-hash");

    await act(async () => buttonByName("Approve proposal").click());

    const decision = requests.find(
      (request) => request.path === "/api/incidents/inc-123/approval-decisions",
    );
    expect(JSON.parse(decision?.init?.body as string)).toEqual({
      decision: "approved",
      reviewed_binding: approvalReview().binding,
    });
    expect(screenText()).toContain("Approved by responder@example.com");
  });

  it("shows recovery convergence, traffic sufficiency, and stabilization progress", async () => {
    const eventSources: MockEventSource[] = [];
    vi.stubGlobal(
      "EventSource",
      class extends MockEventSource {
        constructor(url: string) {
          super(url);
          eventSources.push(this);
        }
      },
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/identity/me") {
        return jsonResponse(responderIdentity());
      }
      if (path === "/api/incidents") {
        return jsonResponse(monitoringIncidentRecord());
      }
      if (path === "/api/incidents/inc-123") {
        return jsonResponse(recoveredIncidentRecord());
      }
      return jsonResponse([managedApplication()]);
    });
    renderApp();

    await act(async () => {});
    await act(async () => buttonByName("Open fake incident").click());

    expect(screenText()).toContain("Recovery verification");
    expect(screenText()).toContain("Awaiting evidence");
    await act(async () => {
      eventSources[0]?.emit({
        event_id: "audit-recovery-2",
        incident_id: "inc-123",
        event_type: "recovery_stabilized",
        occurred_at: "2026-07-11T00:06:00Z",
        actor: "deterministic-recovery-verifier",
        cursor: 2,
      });
    });

    expect(screenText()).toContain("Lifecycle: Resolved");
    expect(screenText()).toContain("Intervention: Succeeded");
    expect(screenText()).toContain("Recovery verification");
    expect(screenText()).toContain("2/2 stable windows");
    expect(screenText()).toContain("Kubernetes converged · revision 7 · generation 10/10");
    expect(screenText()).toContain("Replicas 2 updated, 2 available, 0 unavailable of 2 desired");
    expect(screenText()).toContain("Workload symptoms cleared · OOM terminations 0 · restart delta 0");
    expect(screenText()).toContain("Checkout · 120 requests · availability pass · latency pass");
  });

  it("keeps Viewer identity read-only", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/identity/me") {
        return jsonResponse({ principal: "viewer@example.com", subject: "viewer-1", role: "viewer" });
      }
      return jsonResponse([managedApplication()]);
    });
    renderApp();

    await act(async () => {});

    expect(screenText()).toContain("Authenticated Viewer");
    expect(document.body.textContent).not.toContain("Open fake incident");
  });
});

function renderApp(): void {
  container = document.createElement("div");
  document.body.appendChild(container);
  act(() => {
    root = createRoot(container as HTMLDivElement);
    root.render(<App />);
  });
}

function buttonByName(name: string): HTMLButtonElement {
  const button = Array.from(document.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === name,
  );
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`button not found: ${name}`);
  }
  return button;
}

function linkByName(name: string): HTMLAnchorElement {
  const link = Array.from(document.querySelectorAll("a")).find(
    (candidate) => candidate.textContent?.trim() === name,
  );
  if (!(link instanceof HTMLAnchorElement)) {
    throw new Error(`link not found: ${name}`);
  }
  return link;
}

function screenText(): string {
  return document.body.textContent ?? "";
}

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => payload,
  } as Response;
}

function incidentRecord() {
  return {
    incident: {
      incident_id: "inc-123",
      application_id: "online-boutique",
      profile_version: "v1",
      opened_at: "2026-07-11T00:00:00Z",
      lifecycle: "open",
      investigation_outcome: "not_started",
      intervention_outcome: "not_started",
      version: 0,
      summary: "recommendationservice OOMKilled during checkout",
    },
    application_profile: {
      application_id: "online-boutique",
      display_name: "Online Boutique",
      version: "v1",
      namespace: "online-boutique",
      workloads: [],
      observability_links: [
        {
          label: "Online Boutique logs",
          source: "cloud_logging",
          url: "https://console.cloud.google.com/logs/query",
        },
      ],
      critical_journeys: [],
      evidence_budget: {},
      recovery_criteria: {},
    },
    evidence_window: {
      started_at: "2026-07-10T23:30:00Z",
      ended_at: "2026-07-11T00:00:00Z",
      captured_at: "2026-07-11T00:00:00Z",
    },
    alert_signals: [],
    evidence: [
      {
        evidence_id: "evidence-1",
        source: "cloud_logging",
        query: "pod_logs",
        query_reference: "recommendationservice-logs",
        evidence_window_id: "window-1",
        observed_at: "2026-07-11T00:00:00Z",
        scope: { namespace: "online-boutique", name: "recommendationservice" },
        redacted_excerpt: "OOMKilled with token=<redacted>",
        content_hash: "evidence-hash",
        truncated: true,
        provider_reference: "fake://logging/recommendationservice",
      },
    ],
    evidence_retrieval_failures: [
      {
        failure_id: "failure-1",
        source: "cloud_logging",
        query: "pod_logs",
        scope: { name: "recommendationservice" },
        occurred_at: "2026-07-11T00:00:00Z",
        message: "redaction failed; evidence was not retained",
      },
    ],
    evidence_queries: [],
    findings: [],
    hypotheses: [],
    proposal: null,
    manual_guidance: null,
    policy_decision: null,
    approvals: [],
    interventions: [],
    recovery_assessments: [],
    audit_events: [
      {
        event_id: "audit-1",
        incident_id: "inc-123",
        event_type: "incident_opened",
        occurred_at: "2026-07-11T00:00:00Z",
        actor: "local-operator",
        cursor: 1,
        details: {},
      },
    ],
  };
}

function investigatedIncidentRecord() {
  const record = incidentRecord();
  return {
    ...record,
    incident: {
      ...record.incident,
      lifecycle: "awaiting_approval",
      investigation_outcome: "proposal_ready",
      version: 2,
    },
    findings: [
      {
        finding_id: "finding-health",
        incident_id: "inc-123",
        specialist: "health",
        citations: [
          { evidence_id: "evidence-1", observation: "Pod restarted after OOMKilled." },
        ],
        candidate_explanations: ["Workload restarts align with the rollout."],
        confidence: 0.86,
        contradictions: ["Temporal correlation is not proof of causation."],
        unknowns: ["Recovery has not been verified."],
      },
    ],
    model_invocations: [
      {
        invocation_id: "model-health",
        incident_id: "inc-123",
        role: "health",
        model_id: "gemini-3.5-flash",
        prompt_version: "incident-specialist-v1",
        thinking_level: "medium",
        latency_ms: 120,
        input_tokens: 180,
        output_tokens: 70,
        tool_count: 0,
        output_valid: true,
        failure_reason: null,
      },
    ],
    hypotheses: [
      {
        hypothesis_id: "hypothesis-1",
        incident_id: "inc-123",
        rank: 1,
        statement: "The lower memory limit caused OOM terminations.",
        falsification_test: "Rollback and verify recovery.",
        confidence: 0.92,
        citations: [{ evidence_id: "evidence-1", observation: "OOMKilled" }],
      },
    ],
    proposal: {
      proposal_id: "proposal-1",
      incident_id: "inc-123",
      action: {
        action_type: "rollback_deployment",
        target: { namespace: "online-boutique", name: "recommendationservice", kind: "Deployment" },
        revision: 7,
      },
      expected_impact: "Restore the known healthy memory configuration.",
      recovery_criteria: {
        critical_journey_name: "checkout",
        required_stable_windows: 2,
        stabilization_window_seconds: 60,
      },
      rollback_strategy: "Enter Safe Halt if recovery is ambiguous.",
      evidence_hash: "evidence-hash",
      known_risks: ["A rollout may temporarily reduce available capacity."],
    },
    policy_decision: {
      incident_id: "inc-123",
      proposal_id: "proposal-1",
      status: "passed",
      checks: [
        {
          code: "target_executable",
          passed: true,
          message: "Target is an executable Managed Workload.",
        },
      ],
      evaluated_at: "2026-07-11T00:00:02Z",
      workload_resource_version: "rv-8",
      workload_generation: 8,
      workload_revision: 8,
      patch: {},
      dry_run_diff: "Deployment/recommendationservice: revision 8 -> 7",
      rejection_reason: null,
    },
  };
}

function responderIdentity() {
  return {
    principal: "responder@example.com",
    subject: "accounts.google.com:1234",
    role: "responder",
  };
}

function approvalReview() {
  return {
    incident_id: "inc-123",
    proposal_id: "proposal-1",
    responder_principal: "responder@example.com",
    binding: {
      incident_version: 2,
      proposal_hash: "proposal-hash",
      evidence_hash: "evidence-window-hash",
      workload_resource_version: "rv-8",
      workload_generation: 8,
      workload_revision: 8,
      policy_hash: "policy-hash",
      dry_run_hash: "dry-run-hash",
      recovery_criteria_hash: "recovery-hash",
      failure_strategy_hash: "failure-hash",
      expires_at: "2026-07-11T00:05:00Z",
    },
  };
}

function approvedIncidentRecord() {
  const record = investigatedIncidentRecord();
  return {
    ...record,
    incident: { ...record.incident, version: 3 },
    approvals: [
      {
        approval_id: "approval-1",
        responder_principal: "responder@example.com",
        decision: "approved",
        decided_at: "2026-07-11T00:03:00Z",
      },
    ],
  };
}

function recoveredIncidentRecord() {
  const record = approvedIncidentRecord();
  const assessment = {
    incident_id: "inc-123",
    intervention_id: "intervention-1",
    window_started_at: "2026-07-11T00:04:00Z",
    window_ended_at: "2026-07-11T00:05:00Z",
    observed_at: "2026-07-11T00:05:00Z",
    generation: 10,
    observed_generation: 10,
    active_revision: 7,
    desired_replicas: 2,
    updated_replicas: 2,
    available_replicas: 2,
    unavailable_replicas: 0,
    oom_terminations: 0,
    restart_delta: 0,
    kubernetes_converged: true,
    symptoms_cleared: true,
    journey_name: "checkout",
    criteria_satisfied: true,
    request_count: 120,
    success_rate: 0.995,
    p95_latency_ms: 800,
    traffic_sufficient: true,
    availability_satisfied: true,
    latency_satisfied: true,
    synthetic_probe_used: false,
    synthetic_probe_successes: null,
    sufficient_evidence: true,
    stable_windows: 1,
    required_stable_windows: 2,
    explanation: "Recovery criteria are satisfied.",
  };
  return {
    ...record,
    incident: {
      ...record.incident,
      lifecycle: "resolved",
      intervention_outcome: "succeeded",
    },
    recovery_assessments: [
      assessment,
      {
        ...assessment,
        window_started_at: "2026-07-11T00:05:00Z",
        window_ended_at: "2026-07-11T00:06:00Z",
        observed_at: "2026-07-11T00:06:00Z",
        stable_windows: 2,
      },
    ],
  };
}

function monitoringIncidentRecord() {
  const record = approvedIncidentRecord();
  return {
    ...record,
    incident: {
      ...record.incident,
      lifecycle: "monitoring",
      intervention_outcome: "monitoring",
    },
    recovery_assessments: [],
  };
}

class MockEventSource {
  readonly url: string;
  private listener: ((message: MessageEvent<string>) => void) | null = null;

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(_type: string, listener: EventListener): void {
    this.listener = listener as (message: MessageEvent<string>) => void;
  }

  close(): void {}

  emit(payload: object): void {
    this.listener?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

function managedApplication() {
  return {
    application_profile: {
      application_id: "online-boutique",
      display_name: "Online Boutique",
      namespace: "online-boutique",
      workloads: [
        {
          reference: { name: "recommendationservice" },
          executable: true,
          protected_dependency: false,
        },
        {
          reference: { name: "redis-cart" },
          executable: false,
          protected_dependency: true,
        },
      ],
    },
    profile_load: { application_id: "online-boutique", valid: true, errors: [] },
    enrollment: { ready: true, failed_checks: [] },
    health: { status: "unknown", message: "Health evidence has not been connected yet." },
    incident_count: 1,
  };
}
