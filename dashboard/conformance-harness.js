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
    created_at: isoAgo(86400 * 2),
    updated_at: isoAgo(130),
    tokens: { charged: 184200, budget: 400000, remaining: 215800 },
    budgets: { max_activations: 20, max_step_activations: 4, max_tokens: 400000 },
    step_states: { done: 3, running: 1, waiting: 1 },
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
    created_at: isoAgo(86400 * 5),
    updated_at: isoAgo(7200),
    tokens: { charged: 298400, budget: 350000, remaining: 51600 },
    budgets: { max_activations: 16, max_step_activations: 3, max_tokens: 350000 },
    step_states: { done: 5, blocked: 1 },
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
    created_at: isoAgo(86400 * 9),
    updated_at: isoAgo(86400),
    tokens: { charged: 121900, budget: 240000, remaining: 118100 },
    budgets: { max_activations: 12, max_step_activations: 3, max_tokens: 240000 },
    step_states: { done: 6 },
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

const details = {
  fac_a91d2e7c: {
    ...instances[0],
    parameters_json: JSON.stringify({ request: "Ship Factory dashboard writes", rollout_batch: 10, expedited: false, release_lane: "standard", approval_due_at: null }),
    steps: [
      { step_id: "scope", activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_8b32f04", blocked_reason: null },
      { step_id: "implement", activation: 1, primitive: "agent_task", state: "done", kanban_task_id: "t_a29d8e1", blocked_reason: null },
      { step_id: "review", activation: 1, primitive: "review_gate", state: "done", kanban_task_id: "t_c85f118", blocked_reason: null },
      { step_id: "verify", activation: 1, primitive: "agent_task", state: "running", kanban_task_id: "t_1d7b902", blocked_reason: null },
      { step_id: "release", activation: 1, primitive: "approval_gate", state: "waiting", kanban_task_id: "t_4e2c551", blocked_reason: "Operator release approval required" },
    ],
    decisions: [
      { id: 3, stage_id: "review", stage_type: "review_gate", seat: "verifier", outcome: "approved", body: "Tests and theme-token audit verified.", at: isoAgo(5400) },
      { id: 2, stage_id: "scope", stage_type: "policy", seat: "architect", outcome: "approved", body: "Dashboard-only implementation boundary confirmed.", at: isoAgo(86000) },
    ],
  },
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
