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
});

describe("App", () => {
  it("opens and displays a fake incident with independent status dimensions", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request) => {
      const path = typeof input === "string" ? input : input.toString();
      if (path === "/api/incidents") {
        return jsonResponse(incidentRecord());
      }
      if (path === "/api/applications") {
        return jsonResponse([managedApplication()]);
      }
      return jsonResponse([]);
    });
    renderApp();

    await act(async () => {
      buttonByName("Open fake incident").click();
    });

    expect(screenText()).toContain("recommendationservice OOMKilled during checkout");
    expect(screenText()).toContain("Lifecycle: Open");
    expect(screenText()).toContain("Investigation: Not Started");
    expect(screenText()).toContain("Intervention: Not Started");
    expect(screenText()).toContain("incident_opened");
  });

  it("shows Enrollment readiness and keeps a protected dependency observe-only", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse([managedApplication()]));
    renderApp();

    await act(async () => {});

    expect(screenText()).toContain("Online Boutique");
    expect(screenText()).toContain("Enrolled");
    expect(screenText()).toContain("redis-cart · protected dependency · observe only");
    expect(screenText()).toContain("Incident history: 1");
  });

  it("shows exact profile validation failures without claiming Enrollment", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse([
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
      ]),
    );
    renderApp();

    await act(async () => {});

    expect(screenText()).toContain("Invalid Application Profile");
    expect(screenText()).toContain("Not ready");
    expect(screenText()).toContain("Field required");
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
      critical_journeys: [],
      evidence_budget: {},
      recovery_criteria: {},
    },
    evidence: [],
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
        details: {},
      },
    ],
  };
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
