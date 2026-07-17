import React from "react";
import { createRoot } from "react-dom/client";
import { Badge } from "../../hermes-mobile/web/node_modules/@nous-research/ui/src/ui/components/badge.tsx";
import { Button } from "../../hermes-mobile/web/node_modules/@nous-research/ui/src/ui/components/button.tsx";
import { Card, CardContent } from "../../hermes-mobile/web/node_modules/@nous-research/ui/src/ui/components/card.tsx";
import "../../hermes-mobile/web/src/index.css";
import "./dist/style.css";
import manifest from "./manifest.json";

const h = React.createElement;
const now = Date.now();
const isoAgo = seconds => new Date(now - seconds * 1000).toISOString();
const scenario = new URLSearchParams(window.location.search);

// Verdict contract v2 payload: explicit clean/findings/target fields. Carried
// by rework rows (rejected_by_step_id set) and by the gate activation that
// produced it — the journey view's "rework ordered by …" annotation.
const reviewVerdict = JSON.stringify({
  verdict: "request_changes",
  clean: false,
  target: "implement",
  findings: [
    { id: "F1", severity: "major", title: "Journey card drops blocked_reason on waiting approval steps", citation: "dashboard/dist/index.js:812" },
    { id: "F2", severity: "minor", title: "Attempt badge renders for activation 1 rows", citation: "dashboard/dist/index.js:820" },
  ],
});

const instances = [
  {
    id: "fac_a91d2e7c",
    board: "hermes-mobile",
    recipe_id: "ship-feature",
    recipe_version: 3,
    recipe: "ship-feature@3",
    status: "running",
    activation_count: 7,
    blocked_reason: null,
    collector_task_id: "t_collector_a91",
    parent_tasks_json: null,
    created_at: isoAgo(86400 * 2),
    updated_at: isoAgo(130),
    tokens: { charged: 184200, budget: 400000, remaining: 215800 },
    budgets: { max_activations: 20, max_step_activations: 4, max_tokens: 400000 },
    step_states: { done: 2, running: 1, waiting: 2 },
    latest_steps: [
      { step_id: "scope", step_position: 1, activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_8b32f04", blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
      { step_id: "implement", step_position: 2, activation: 2, primitive: "agent_task", state: "done", kanban_task_id: "t_a29d8e2", blocked_reason: null, finding_count: 0, rejected_by_step_id: "review", rejected_by_activation: 1, verdict_json: reviewVerdict },
      { step_id: "review", step_position: 3, activation: 2, primitive: "review_gate", state: "running", kanban_task_id: "t_c85f119", blocked_reason: null, finding_count: 0, rejected_by_step_id: "review", rejected_by_activation: 1, verdict_json: reviewVerdict },
      { step_id: "verify", step_position: 4, activation: 1, primitive: "agent_task", state: "waiting", kanban_task_id: null, blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
      { step_id: "release", step_position: 5, activation: 1, primitive: "approval_gate", state: "waiting", kanban_task_id: "t_4e2c551", blocked_reason: "Operator release approval required", finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
    ],
  },
  {
    id: "fac_3f28c651",
    board: "hermes-mobile",
    recipe_id: "release-train",
    recipe_version: 2,
    recipe: "release-train@2",
    status: "blocked",
    activation_count: 11,
    blocked_reason: "Security review requested evidence for the token-scope change.",
    collector_task_id: "t_collector_3f2",
    parent_tasks_json: JSON.stringify(["t_epic_101"]),
    created_at: isoAgo(86400 * 5),
    updated_at: isoAgo(7200),
    tokens: { charged: 298400, budget: 350000, remaining: 51600 },
    budgets: { max_activations: 16, max_step_activations: 3, max_tokens: 350000 },
    step_states: { done: 5, blocked: 1 },
    latest_steps: [
      { step_id: "collect", step_position: 1, activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_rt_0a1", blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
      { step_id: "stage", step_position: 2, activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_rt_0b2", blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
      { step_id: "security-signoff", step_position: 3, activation: 1, primitive: "review_gate", state: "blocked", kanban_task_id: "t_rt_0c3", blocked_reason: "Security review requested evidence for the token-scope change.", finding_count: 1, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: "{not json" },
      { step_id: "ship", step_position: 4, activation: 1, primitive: "agent_task", state: "waiting", kanban_task_id: null, blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
    ],
  },
  {
    id: "fac_9c02bd14",
    board: "factory-ops",
    recipe_id: "dependency-refresh",
    recipe_version: 1,
    recipe: "dependency-refresh@1",
    status: "done",
    activation_count: 6,
    blocked_reason: null,
    collector_task_id: "t_collector_9c0",
    parent_tasks_json: null,
    created_at: isoAgo(86400 * 9),
    updated_at: isoAgo(86400),
    tokens: { charged: 121900, budget: 240000, remaining: 118100 },
    budgets: { max_activations: 12, max_step_activations: 3, max_tokens: 240000 },
    step_states: { done: 6 },
    latest_steps: [
      { step_id: "refresh", step_position: 1, activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_dep_001", blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
      { step_id: "verify", step_position: 2, activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_dep_002", blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null },
    ],
  },
];

const recipes = [
  {
    id: "ship-feature", version: 3, status: "active",
    description: "Build, review, verify, and release a product change.",
    parameters: {
      request: { type: "string", required: true, default: null },
      rollout_batch: { type: "integer", required: false, default: 10 },
      expedited: { type: "boolean", required: false, default: false },
      release_lane: { type: "enum", required: true, default: "standard", values: ["standard", "urgent"] },
      approval_due_at: { type: "datetime", required: false, default: null },
    },
    optional_steps: [{ id: "announce", title: "Announce the release" }],
  },
  {
    id: "release-train", version: 2, status: "active",
    description: "Coordinate a bounded release train across verified changes.",
    parameters: { request: { type: "string", required: true, default: null } },
    optional_steps: [],
  },
];

// All activations for fac_a91d2e7c, in recipe order. The review step carries
// the Amendment-1 rework showcase: activation 1 requested changes with a v2
// verdict, activation 2 is the re-review ordered by that rejection.
const journeySteps = [
  { step_id: "scope", step_position: 1, activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_8b32f04", blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null, created_at: isoAgo(170000), updated_at: isoAgo(168000) },
  { step_id: "implement", step_position: 2, activation: 1, primitive: "agent_task", state: "rejected", kanban_task_id: "t_a29d8e1", blocked_reason: null, finding_count: 2, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null, created_at: isoAgo(160000), updated_at: isoAgo(120000) },
  { step_id: "implement", step_position: 2, activation: 2, primitive: "agent_task", state: "done", kanban_task_id: "t_a29d8e2", blocked_reason: null, finding_count: 0, rejected_by_step_id: "review", rejected_by_activation: 1, verdict_json: '{"verdict": "request_changes"', created_at: isoAgo(110000), updated_at: isoAgo(60000) },
  { step_id: "review", step_position: 3, activation: 1, primitive: "review_gate", state: "changes_requested", kanban_task_id: "t_c85f118", blocked_reason: null, finding_count: 2, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: reviewVerdict, created_at: isoAgo(130000), updated_at: isoAgo(118000) },
  { step_id: "review", step_position: 3, activation: 2, primitive: "review_gate", state: "running", kanban_task_id: "t_c85f119", blocked_reason: null, finding_count: 0, rejected_by_step_id: "review", rejected_by_activation: 1, verdict_json: reviewVerdict, created_at: isoAgo(55000), updated_at: isoAgo(1300) },
  { step_id: "verify", step_position: 4, activation: 1, primitive: "agent_task", state: "waiting", kanban_task_id: null, blocked_reason: null, finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null, created_at: isoAgo(55000), updated_at: isoAgo(55000) },
  { step_id: "release", step_position: 5, activation: 1, primitive: "approval_gate", state: "waiting", kanban_task_id: "t_4e2c551", blocked_reason: "Operator release approval required", finding_count: 0, rejected_by_step_id: null, rejected_by_activation: null, verdict_json: null, created_at: isoAgo(55000), updated_at: isoAgo(55000) },
];

const details = {
  fac_a91d2e7c: {
    ...instances[0],
    parameters_json: JSON.stringify({ request: "Ship Factory dashboard writes", rollout_batch: 10, expedited: false, release_lane: "standard", approval_due_at: null }),
    steps: journeySteps,
    activations: journeySteps.reduce((grouped, step) => {
      (grouped[step.step_id] = grouped[step.step_id] || []).push(step);
      return grouped;
    }, {}),
    decisions: [
      { id: 3, stage_id: "review", stage_type: "review_gate", seat: "verifier", outcome: "changes_requested", body: "2 findings; rework ordered on implement.", at: isoAgo(118000) },
      { id: 2, stage_id: "scope", stage_type: "policy", seat: "architect", outcome: "approved", body: "Dashboard-only implementation boundary confirmed.", at: isoAgo(168000) },
    ],
  },
};

const receiptsByInstance = {
  fac_a91d2e7c: [
    { step_id: "scope", activation: 1, kanban_task_id: "t_8b32f04", run_id: "run_scope_1", seat: "architect", executor: "codex", provider: null, resolved_model: "gpt-5.4", model: "gpt-5.4", started_at: isoAgo(170000), ended_at: isoAgo(168000), exit_code: 0, result: "done", tokens_in: 8200, tokens_out: 2100, tokens_total: 10300, duration_s: 940, access_enforcement_level: "enforced", has_log: true, has_prompt: true },
    { step_id: "implement", activation: 1, kanban_task_id: "t_a29d8e1", run_id: "run_impl_1", seat: "builder", executor: "hermes", provider: "hermes-anthropic-proxy", resolved_model: "claude-sonnet-5", model: "claude-sonnet-5", started_at: isoAgo(160000), ended_at: isoAgo(155000), exit_code: 0, result: "done", tokens_in: 36100, tokens_out: 12000, tokens_total: 48100, duration_s: 2600, access_enforcement_level: "enforced", has_log: true, has_prompt: true },
    { step_id: "implement", activation: 2, kanban_task_id: "t_a29d8e2", run_id: "run_impl_2", seat: "builder", executor: "hermes", provider: "hermes-anthropic-proxy", resolved_model: "claude-sonnet-5", model: "claude-sonnet-5", started_at: isoAgo(110000), ended_at: isoAgo(105000), exit_code: 0, result: "done", tokens_in: 28400, tokens_out: 9800, tokens_total: 38200, duration_s: 2100, access_enforcement_level: "enforced", has_log: true, has_prompt: false },
    { step_id: "review", activation: 1, kanban_task_id: "t_c85f118", run_id: "run_rev_1", seat: "verifier", executor: "claude", provider: null, resolved_model: "opus-4.6", model: "opus-4.6", started_at: isoAgo(130000), ended_at: isoAgo(118000), exit_code: 0, result: "changes_requested", tokens_in: 18200, tokens_out: 4200, tokens_total: 22400, duration_s: 1300, access_enforcement_level: "enforced", has_log: true, has_prompt: true },
    { step_id: "review", activation: 2, kanban_task_id: "t_c85f119", run_id: "run_rev_2", seat: "verifier", executor: "claude", provider: null, resolved_model: "opus-4.6", model: "opus-4.6", started_at: isoAgo(55000), ended_at: null, exit_code: null, result: null, tokens_in: 6100, tokens_out: 900, tokens_total: 7000, duration_s: 410, access_enforcement_level: "enforced", has_log: false, has_prompt: true },
  ],
};

const runArtifacts = {
  "run_scope_1:log": { run_id: "run_scope_1", kind: "log", content: "[scope] resolved the dashboard-only boundary\n[scope] wrote scope note to the collector card", truncated: false },
  "run_scope_1:prompt": { run_id: "run_scope_1", kind: "prompt", content: "You are the architect seat. Scope the dashboard journey view…", truncated: false },
  "run_impl_1:log": { run_id: "run_impl_1", kind: "log", content: "[implement] first pass at the folded card\n[implement] exit 0", truncated: false },
  "run_impl_1:prompt": { run_id: "run_impl_1", kind: "prompt", content: "You are the builder seat. Implement the folded journey card…", truncated: false },
  "run_impl_2:log": { run_id: "run_impl_2", kind: "log", content: "[implement] rework per review findings F1/F2\n[implement] blocked_reason now renders on waiting approval steps\n[implement] attempt badge gated to activation > 1\n… (log continues)", truncated: true },
  "run_rev_1:log": { run_id: "run_rev_1", kind: "log", content: "[review] 2 findings raised; verdict request_changes targeting implement", truncated: false },
  "run_rev_1:prompt": { run_id: "run_rev_1", kind: "prompt", content: "You are the verifier seat. Review the change set against the journey spec…", truncated: false },
  "run_rev_2:prompt": { run_id: "run_rev_2", kind: "prompt", content: "You are the verifier seat. Re-review the rework for findings F1/F2…", truncated: false },
};

const waiting = [
  {
    instance_id: "fac_a91d2e7c", step_id: "release", activation: 1, primitive: "approval_gate", state: "waiting",
    board: "hermes-mobile", recipe_id: "ship-feature", recipe_version: 3, step_position: 5, step_total: 5,
    blocked_reason: "Approve the production rollout after the visual conformance review.", updated_at: isoAgo(7400), instance_updated_at: isoAgo(130),
  },
  {
    instance_id: "fac_61ea73b0", step_id: "security-signoff", activation: 2, primitive: "approval_gate", state: "waiting",
    board: "factory-ops", recipe_id: "credential-rotation", recipe_version: 1, step_position: 4, step_total: 6,
    blocked_reason: "Confirm the old credential has been revoked in every environment.", updated_at: isoAgo(19000), instance_updated_at: isoAgo(19000),
  },
];

const seats = [
  { name: "architect", role: "cto", profile: "architect", executor: "codex", model: "gpt-5.4", profile_model: "gpt-5.4", reasoning: "high", reports_to: "operator", max_concurrent: 2, paused: false },
  { name: "builder", role: "engineer", profile: "dev-backend-codex", executor: "hermes", model: "claude-sonnet-5", profile_model: "claude-sonnet-5", provider_config: { provider: "hermes-anthropic-proxy", base_url: "http://127.0.0.1:18808", model: "claude-sonnet-5" }, reasoning: "medium", reports_to: "architect", max_concurrent: 3, paused: false },
  { name: "verifier", role: "qa", profile: "verifier", executor: "claude", model: "opus-4.6", profile_model: "claude-sonnet-5", model_mismatch: true, reasoning: "high", reports_to: "operator", max_concurrent: 1, paused: true },
];

const dailyCosts = [
  { day: new Date(now).toISOString().slice(0, 10), charges: 14, tokens_total: 284300 },
  { day: new Date(now - 86400000).toISOString().slice(0, 10), charges: 21, tokens_total: 416900 },
  { day: new Date(now - 172800000).toISOString().slice(0, 10), charges: 9, tokens_total: 178200 },
];

const instanceCosts = instances.map((item, index) => ({
  instance: item.id,
  board: item.board,
  recipe: item.recipe,
  charges: [7, 11, 6][index],
  tokens_total: item.tokens.charged,
}));

let registeredId = null;
let registeredPage = null;
window.__HERMES_PLUGINS__ = {
  register(id, component) {
    registeredId = id;
    registeredPage = component;
  },
};

window.__HERMES_PLUGIN_SDK__ = {
  React,
  hooks: {
    useState: React.useState,
    useEffect: React.useEffect,
    useCallback: React.useCallback,
    useMemo: React.useMemo,
  },
  components: { Badge, Button, Card, CardContent },
  utils: {},
  fetchJSON(url, options = {}) {
    if (url.endsWith("/profiles")) return Promise.resolve(["default", "architect", "dev-backend-codex", "verifier"]);
    if (options.method === "POST" && url.endsWith("/seats")) return Promise.resolve({ name: JSON.parse(options.body).name });
    if (options.method === "PUT" && url.includes("/seats/")) return Promise.resolve({ name: decodeURIComponent(url.split("/seats/")[1]) });
    if (options.method === "POST" && url.endsWith("/instances")) return Promise.resolve({ instance_id: "fac_new7c20", recipe: "ship-feature@3" });
    if (options.method === "POST" && url.endsWith("/triage")) return Promise.resolve({ task_id: "t_triage1", status: "triage", board: "hermes-mobile" });
    if (options.method === "POST" && url.endsWith("/reroute")) return Promise.resolve({ activated: false, replacement: { instance_id: "fac_a91d2e7c" } });
    if (options.method === "POST" && url.endsWith("/cancel")) return Promise.resolve({ instance_id: "fac_a91d2e7c", status: "cancelled" });
    if (options.method === "POST") return Promise.resolve({ key: "harness-decision" });
    if (url.endsWith("/status")) return Promise.resolve(scenario.get("daemon") === "stopped" ? {
      running: false, pid: null, last_tick_at: isoAgo(95), board: "hermes-mobile",
      boards: [{ board: "hermes-mobile", last_tick_at: isoAgo(95), last_tick_age_seconds: 95, stale: true }], tick_interval_seconds: 20,
      config: { recipes_enabled: true, library_path: "/operator/recipes", bare_task_recipe: "ship-feature@3" },
    } : {
      running: true, pid: 48120, last_tick_at: isoAgo(12), board: "hermes-mobile",
      boards: [{ board: "hermes-mobile", last_tick_at: isoAgo(12), last_tick_age_seconds: 12, stale: false }], tick_interval_seconds: 20,
      config: { recipes_enabled: true, library_path: "/operator/recipes", bare_task_recipe: "ship-feature@3" },
    });
    if (url.endsWith("/recipes")) return Promise.resolve(recipes.map(item => ({ ...item })));
    if (url.endsWith("/waiting")) return Promise.resolve(waiting.map(item => ({ ...item })));
    if (url.endsWith("/instances")) return Promise.resolve(instances.map(item => ({ ...item })));
    if (url.endsWith("/cancel")) return Promise.resolve({
      instance_id: "fac_a91d2e7c",
      workers: [{ task_id: "t_1d7b902", pid: 55231, executor: "codex" }],
      nonterminal_steps: ["verify", "release"],
      suppressed: ["t_1d7b902", "t_4e2c551"],
      collector: "t_collector9",
    });
    if (url.includes("/instances/") && url.endsWith("/receipts")) {
      const id = decodeURIComponent(url.split("/instances/")[1].split("/")[0]);
      return Promise.resolve((receiptsByInstance[id] || []).map(item => ({ ...item })));
    }
    if (url.includes("/runs/")) {
      const [runId, kind] = url.split("/runs/")[1].split("/");
      const artifact = runArtifacts[decodeURIComponent(runId) + ":" + kind];
      if (artifact) return Promise.resolve({ ...artifact });
      return Promise.reject(new Error("404: No " + kind + " recorded for run " + runId));
    }
    if (url.includes("/instances/")) {
      const id = decodeURIComponent(url.split("/instances/")[1]);
      return Promise.resolve(details[id] || { ...instances.find(item => item.id === id), steps: [], decisions: [] });
    }
    if (url.endsWith("/seats")) return Promise.resolve(seats.map(item => ({ ...item })));
    if (url.includes("/costs?by=day")) return Promise.resolve(dailyCosts.map(item => ({ ...item })));
    if (url.includes("/costs?by=instance")) return Promise.resolve(instanceCosts.map(item => ({ ...item })));
    return Promise.reject(new Error("404: Harness fixture missing for " + url));
  },
};

await import("./dist/index.js");

if (!registeredPage) throw new Error("Factory bundle did not register a page");
if (registeredId !== manifest.name) {
  throw new Error(
    "Bundle registered as \"" + registeredId + "\" but manifest.name is \"" + manifest.name +
    "\"; the Hermes host resolves the tab via getPluginComponent(manifest.name)"
  );
}
createRoot(document.querySelector(".factory-root")).render(h(registeredPage));

const requestedView = scenario.get("view");
if (requestedView) {
  window.setTimeout(() => {
    const tab = Array.from(document.querySelectorAll(".factory-tabs button"))
      .find(button => button.textContent.toLowerCase().startsWith(requestedView.toLowerCase()));
    if (tab) tab.click();
    const dialog = scenario.get("dialog");
    if (dialog === "run" || dialog === "triage") {
      window.setTimeout(() => {
        const label = dialog === "run" ? "Run recipe" : "New triage task";
        const button = Array.from(document.querySelectorAll("button")).find(item => item.textContent.trim() === label);
        if (button) button.click();
      }, 150);
    }
    const expand = scenario.get("expand");
    if (expand) {
      // ?view=journey&expand=review unfolds a step's attempt stack; expand=1
      // clicks the first step row.
      window.setTimeout(() => {
        const rows = Array.from(document.querySelectorAll(".factory-journey-step-row"));
        const stepButton = rows.find(button => {
          const label = button.querySelector("strong");
          return label && label.textContent.trim() === expand;
        }) || rows.find(button => expand === "1" || button.textContent.includes(expand));
        if (stepButton) stepButton.click();
      }, 250);
    }
    if (scenario.get("drawer") === "open" || dialog === "cancel") {
      window.setTimeout(() => {
        const row = document.querySelector(".factory-instance-table tbody tr");
        if (row) row.click();
        if (dialog === "cancel") {
          window.setTimeout(() => {
            const button = Array.from(document.querySelectorAll("button")).find(item => item.textContent.includes("Preview cancellation"));
            if (button) button.click();
          }, 180);
        }
      }, 150);
    }
  }, 100);
}
