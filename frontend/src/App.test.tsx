import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

type FetchCall = {
  url: string;
  init?: RequestInit;
};

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const calls: FetchCall[] = [];
let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
  calls.length = 0;
  vi.restoreAllMocks();
});

describe("App", () => {
  it("runs the full guided happy path with mocked API integration", async () => {
    mockFetch({
      "/api/repositories/connect": repositorySnapshot(),
      "/api/runs/run-123/analyse": analysisResult(),
      "/api/runs/run-123/rehearsal": rehearsalState(),
      "/api/runs/run-123/baseline": loadResult("baseline", 0.99, 420),
      "/api/runs/run-123/pressure": loadResult("pressure", 0.72, 3100),
      "/api/runs/run-123/council": councilPlan(),
      "/api/runs/run-123/plans/plan-1/apply": experimentReport(),
      "/api/runs/run-123/verify": experimentReport(),
      "/api/runs/run-123/pull-request": pullRequestResult(),
    });
    renderApp();

    await submitConnectForm();
    await clickButton("Analyse");
    await clickButton("Build Twin");
    await clickButton("Run Baseline + Pressure");
    await clickButton("Run Council");
    await clickButton("Apply Plan");
    await clickButton("Verify");
    await clickButton("Open Draft PR");

    expect(screenText()).toContain("Draft opened");
    expect(screenText()).toContain("https://github.com/acme/shop/pull/7");
    expect(screenText()).toContain("checkout-patch.yaml");
    expect(calls.map((call) => call.url)).toContain("/api/runs/run-123/pull-request");
  });

  it("shows a useful failed compatibility state without building a twin", async () => {
    mockFetch({
      "/api/repositories/connect": repositorySnapshot(),
      "/api/runs/run-123/analyse": analysisResult([
        {
          severity: "error",
          resource_kind: "StatefulSet",
          resource_name: "database",
          message: "StatefulSet is not supported by the MVP",
          source: "rendered.yaml",
        },
      ]),
    });
    renderApp();

    await submitConnectForm();
    await clickButton("Analyse");

    expect(screenText()).toContain("Blocked");
    expect(screenText()).toContain("compatibility_failed: StatefulSet is not supported by the MVP");
    expect(buttonByName("Build Twin").disabled).toBe(true);
  });

  it("surfaces an infeasible council result as a recoverable failure", async () => {
    mockFetch({
      "/api/repositories/connect": repositorySnapshot(),
      "/api/runs/run-123/analyse": analysisResult(),
      "/api/runs/run-123/rehearsal": rehearsalState(),
      "/api/runs/run-123/baseline": loadResult("baseline", 0.99, 420),
      "/api/runs/run-123/pressure": loadResult("pressure", 0.52, 4200),
      "/api/runs/run-123/council": {
        ...councilPlan(),
        status: "infeasible",
        actions: [],
        representative_proposals: [],
        infeasible_reason: "rehearsal quota cannot satisfy checkout minimums",
      },
    });
    renderApp();

    await submitConnectForm();
    await clickButton("Analyse");
    await clickButton("Build Twin");
    await clickButton("Run Baseline + Pressure");
    await clickButton("Run Council");

    expect(screenText()).toContain("council_infeasible: rehearsal quota cannot satisfy checkout minimums");
    expect(buttonByName("Apply Plan").disabled).toBe(true);
  });

  it("resets the workflow and deletes deployed rehearsal state", async () => {
    mockFetch({
      "/api/repositories/connect": repositorySnapshot(),
      "/api/runs/run-123/analyse": analysisResult(),
      "/api/runs/run-123/rehearsal": rehearsalState(),
      "DELETE /api/runs/run-123/rehearsal": { ...rehearsalState(), status: "deleted" },
    });
    renderApp();

    await submitConnectForm();
    await clickButton("Analyse");
    await clickButton("Build Twin");
    await clickButton("Reset");

    expect(screenText()).toContain("Reset complete");
    expect(screenText()).toContain("Not connected");
    expect(calls.some((call) => call.url === "/api/runs/run-123/rehearsal" && call.init?.method === "DELETE")).toBe(true);
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

async function submitConnectForm(): Promise<void> {
  const form = document.querySelector("form");
  if (!form) {
    throw new Error("connect form was not rendered");
  }
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
}

async function clickButton(name: string): Promise<void> {
  const button = buttonByName(name);
  await act(async () => {
    button.click();
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

function mockFetch(routes: Record<string, unknown>): void {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push({ url, init });
    const method = init?.method ?? "GET";
    const methodKey = `${method} ${url}`;
    const payload = routes[methodKey] ?? routes[url];
    if (payload === undefined) {
      return jsonResponse({ detail: { code: "missing_mock", message: url } }, false);
    }
    return jsonResponse(payload, true);
  });
}

function jsonResponse(payload: unknown, ok: boolean): Response {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: ok ? "OK" : "Internal Server Error",
    json: async () => payload,
  } as Response;
}

function repositorySnapshot() {
  return {
    run_id: "run-123",
    repository_url: "file:///tmp/demo.git",
    ref: "main",
    commit_sha: "abcdef123456",
    workspace_path: "/tmp/run-123",
    deployment_path: "deploy/overlays/production",
    captured_at: "2026-07-05T00:00:00Z",
  };
}

function analysisResult(compatibility_issues: unknown[] = []) {
  return {
    run_id: "run-123",
    source: { rendered_resource_count: 28 },
    compatibility_issues,
    dependency_edges: [
      { from_service: "gateway", to_service: "checkout" },
      { from_service: "checkout", to_service: "payment" },
      { from_service: "checkout", to_service: "recommendation" },
    ],
    services: [
      service("gateway", "critical", ["checkout"], 2, 120),
      service("checkout", "critical", ["payment", "recommendation"], 2, 250),
      service("payment", "critical", [], 2, 160),
      service("recommendation", "important", [], 2, 220),
      service("analytics-worker", "optional", [], 1, 300, true),
    ],
  };
}

function service(
  name: string,
  criticality: string,
  dependencies: string[],
  current_replicas: number,
  cpu_millis: number,
  optional = false,
) {
  return {
    name,
    image: "demo:latest",
    current_replicas,
    min_replicas: optional ? 0 : 1,
    max_replicas: 6,
    resource_requests: { cpu_millis, memory_mib: 128 },
    criticality,
    dependencies,
    degradation_modes: name === "recommendation" ? ["cached"] : [],
    optional,
    config_maps: [`${name}-config`],
    hpa: { min_replicas: 1, max_replicas: 6 },
  };
}

function rehearsalState() {
  return {
    run_id: "run-123",
    namespace: "kc-rehearsal-run-123",
    status: "deployed",
    readiness: { status: "passed", errors: [], warnings: [] },
    resources: [{ kind: "Deployment", name: "checkout", namespace: "kc-rehearsal-run-123" }],
    plan: {
      safety_substitutions: ["omitted production Secrets", "disabled external ingress"],
    },
  };
}

function loadResult(phase: string, success_rate: number, p95_latency_ms: number) {
  return {
    scenario_name: "flash-sale-fixed-capacity",
    phase,
    request_count: 900,
    success_rate,
    p95_latency_ms,
    errors: [],
    status: success_rate >= 0.95 ? "passed" : "failed",
    failure_type: success_rate >= 0.95 ? null : "objective",
  };
}

function councilPlan() {
  const actions = [
    {
      action_type: "suspend_optional_deployment",
      target_service: "analytics-worker",
      target_namespace: "kc-rehearsal-run-123",
      parameters: {},
      reason: "release CPU for checkout",
    },
    {
      action_type: "set_config_mode",
      target_service: "recommendation",
      target_namespace: "kc-rehearsal-run-123",
      parameters: { mode: "cached" },
      reason: "reduce recommendation cost",
    },
    {
      action_type: "scale_deployment",
      target_service: "checkout",
      target_namespace: "kc-rehearsal-run-123",
      parameters: { replicas: 4 },
      reason: "increase checkout capacity",
    },
  ];
  return {
    plan_id: "plan-1",
    run_id: "run-123",
    namespace: "kc-rehearsal-run-123",
    actions,
    validation: { status: "passed", errors: [], warnings: [] },
    status: "valid",
    repair_attempted: false,
    representative_proposals: [
      { service_name: "checkout", proposed_actions: [actions[2]], rationale: "checkout needs replicas" },
      { service_name: "recommendation", proposed_actions: [actions[1]], rationale: "cached mode is acceptable" },
      { service_name: "analytics-worker", proposed_actions: [actions[0]], rationale: "optional during flash sale" },
    ],
  };
}

function experimentReport() {
  return {
    run_id: "run-123",
    plan_id: "plan-1",
    status: "successful",
    baseline: loadResult("baseline", 0.99, 420),
    pressure_before: loadResult("pressure", 0.72, 3100),
    pressure_after: loadResult("post_change", 0.97, 980),
    validation: { status: "passed", errors: [], warnings: [] },
    applied_actions: councilPlan().actions,
    rollback_guidance: "revert generated overlay patches",
  };
}

function pullRequestResult() {
  return {
    run_id: "run-123",
    branch_name: "kubecouncil/rehearsal-run-123",
    commit_sha: "1234567890ab",
    pr_url: "https://github.com/acme/shop/pull/7",
    draft: true,
    changed_files: ["deploy/overlays/production/checkout-patch.yaml"],
  };
}
