# GraphRunner v1 Frontend Redesign + Recipe Designer Implementation Plan

> **For Hermes:** Use the `subagent-driven-development` skill to implement this plan
> task-by-task. The viewer redesign (Milestones A–C) must land and pass its focused
> gates before the designer (Milestone D) begins.

**Goal:** Replace the flat stacked-card representation of a running GraphRunner v1 Run
with a node-canvas operator view in the style of the referenced node-workflow demo
(dark canvas, rounded state-colored node cards, live per-box cost telemetry, labeled
result edges, red dashed rework arrows, inline insert), then add a **recipe designer
layer** that edits the *same frozen v1 grammar* through the existing validator.

**Architecture:** Extend the existing `project_direct_graph_v1` projection (the source
of truth; `shipfactory/recipe_graph.py:23`) with per-box telemetry, add a read-only
`GraphV1Canvas` React view that renders boxes-as-nodes and arrows-as-edges in the
dashboard plugin bundle, then add a constrained recipe editor that round-trips every
edit through the existing `GraphRecipe.validate()` (`shipfactory/graph_recipe.py`).
No new grammar, no new runtime fields, no dependency on legacy primitives.

**Tech stack (no new dependencies):** host-owned React via the dashboard plugin SDK
IIFE bundle (`dashboard/dist/index.js`), vanilla SVG/rect rendering, an additive
SQLite column + accessor for telemetry, the existing pure validator,
`dashboard/conformance-harness.js` for full-stack verification, pytest.

**ROM boundary (non-negotiable):** The runtime grammar is exactly
`docs/recipe-structure-v1.md:8-102` — `name / boxes / arrows`, box = `id/name/who/
instructions[/end]/start`, arrow = `from/result/to`. This plan adds **no** box or arrow
fields to that grammar. The designer produces the same YAML shape end-to-end.

---

## Current context (verified)

- GraphRunner v1 is **implemented, tested, and cut over** — migration 17 tables exist
  (`recipe_runs_v1`, `box_attempts_v1`, `route_tokens_v1`, `run_events_v1`), and the
  full suite is green through finding #137.
- The dashboard projection already returns everything a canvas needs:
  `GET /v1/runs/{run_id}/graph` → `{recipe, boxes, arrows, run:{attempts, waiting_human}}`
  (`project_direct_graph_v1`).
- The v1 Run UI today is a **flat card stack**: `GraphV1Run` renders one `<section>`
  then `graph.boxes.map(...)` as stacked `<article>` cards (`dashboard/dist/index.js:2644`).
- The legacy graph renderer (`GraphRenderer`, ~line 738) already uses a rank-based SVG
  layout with node shapes + rework-arrow markers — reusable patterns, not v1 code.
- The dashboard is a **hand-authored IIFE bundle with no build step**; the host owns
  React. `dashboard/dist/index.js` + `style.css` are committed source. Conformance
  harness drives the real HTTP endpoint for verification.

## Key gaps the redesign must close

1. **No per-box cost/duration** on the canvas face today. `box_attempts_v1` lacks
   `tokens_total` / `duration_s`. Legacy exec tasks have them (`store.py:23-24`); the
   v1 attempt → executor path must carry them back.
2. **No run-overlay layout** for the v1 projection (the current v1 view is a list,
   not a positioned canvas).
3. **No recipe authoring surface.** Recipes are hand-edited YAML only.

---

## M0 — Scaffold (do first, tiny)

### Task 0: Baseline + guard snapshot

**Objective:** Prove the suite is green and lock the bundle before touching it.

**Files:**
- Run: `ulimit -n 4096; export PYTHONPATH=…; $PY -m pytest -q` (full suite from the
  canonical prefix in `docs/plans/2026-07-29-graphrunner-v1.md`)
- Record: `dashboard/dist/index.js` and `style.css` current size + sha.

**Step 1:** Run the full suite. Record actual pass count and duration — do not predict.
**Step 2:** `git stash list` must be clean and `git status` shows only the three
untracked design-recon files from session start → confirm no in-scope uncommitted work.
**Step 3:** checkpoint note (no commit yet).

---

## Milestone A — Telemetry on the v1 attempt (data layer)

### Task 1: Add telemetry columns to `box_attempts_v1`

**Objective:** Let each box attempt carry the real tokens consumed and wall-clock
duration the demo shows on the card face — populated from real executor runs, not
synthesized.

**Files:**
- Modify: `shipfactory/store.py` — additive migration 18
- Create: `tests/test_graph_store_v1_telemetry.py`

**Step 1 (RED):** Write a migration test asserting `box_attempts_v1` gains
`tokens_total INTEGER NOT NULL DEFAULT 0` and `duration_s REAL`, that migration 17
data remains intact, and latest-schema assertion becomes 18 (mirror the migration-17
pattern; update the normative latest-version assertions the same way task 4 of the v1
plan did).

**Step 2:** Add migration 18 (non-destructive ALTER, no legacy-table writes). Update
the store functions that insert/update box attempts to accept optional
`tokens_total`/`duration_s`.

**Step 3 (GREEN):** `$PY -m pytest tests/test_graph_store_v1_telemetry.py -q`.

### Task 2: Capture telemetry at the graph-runtime boundary

**Objective:** The existing executor path already knows tokens + duration (legacy
`runs` has `tokens_total`, `duration_s`); thread it into the v1 completion event so a
`box_completed` event carries them onto the attempt.

**Files:**
- Modify: `shipfactory/graph_runtime.py` (reap callback → enqueue fields)
- Modify: `shipfactory/graph_runner.py` (apply `box_completed` → store on attempt)
- Modify: `tests/test_graph_runtime_v1.py`, `tests/test_graph_runner_v1.py`

**Step 1 (RED):** Test that a `box_completed` event with `tokens_total`/`duration_s`
reflects both onto the stored attempt; a completion without them defaults to 0/null and
never errors.

**Step 2:** Implement plumbing. Verify a successful graph box never invokes legacy
artifact sealing (unchanged invariant).

**Step 3 (GREEN):** focused runtime + runner tests.

### Task 3: Project telemetry through the v1 graph API

**Objective:** `project_direct_graph_v1` now exposes per-attempt telemetry on the
canvas.

**Files:**
- Modify: `shipfactory/recipe_graph.py` (`project_direct_graph_v1`)
- Modify: `tests/test_dashboard_graph_v1.py`

**Step 1 (RED):** assert projection includes `tokens_total` and `duration_s` on
attempts; human attempts and never-run boxes surface cleanly (0 / null).
**Step 2:** extend projection; **Step 3 GREEN.**

---

## Milestone B — Node-canvas render of a Running Run

### Task 4: Compute a stable canvas layout for a v1 recipe

**Objective:** Turn `boxes` + `arrows` into positioned nodes and edge paths without a
graph-library dependency — reuse the rank/dag patterns already present in
`GraphRenderer` (`graphRanks`, `graphGeometry`).

**Files:**
- Modify: `dashboard/dist/index.js` (pure helpers only, no render)
- Create: `dashboard/conformance/graph-layout-*` (see Task 7 harness)

**Step 1:** Pure function `graphV1Layout(boxes, arrows)` → `{nodeId: {x,y,width,height},
edgeId: path}`. Left-to-right topological rank: `start` at left, split expands
vertically, join re-converges, backward arrows route as a loop (separate lane, not
over the forward edge). End node rightmost.

**Step 2:** Deterministic for a fixed recipe (no `Date.now`, no Map iteration order
gotchas). **Step 3:** `git add` + focused JS assertion in the conformance harness.

### Task 5: Render the running node canvas

**Objective:** Replace the v1 flat card stack with a dark node-canvas view.

**Files:**
- Modify: `dashboard/dist/index.js` (`GraphV1Run`)
- Modify: `dashboard/dist/style.css`
- Create: `dashboard/conformance-harness.js` additions

**Step 1:** New `GraphV1Canvas` component fed the (already-fetched) v1 graph object:
- **Node card per box**: rounded, dark fill; a state-tinted stroke — `green` when the
  latest attempt is `completed` (✓), `spinner` when `running`, `muted` when `waiting_human`
  shows a human glyph, `red` on escalation.
- **Card face**: `box.name`, a small mono row of the live telemetry
  (`2.0k tok · 2.6s`) when present, and End badge.
- **Edges**: result label on the line; forward edges green, **rework edges (target is
  at/before source in declared order) rendered red dashed** — mirror the existing
  `factory-graph-rework-arrow` marker.
- **Inline "+"** at an edge midpoint to stage a new box (insert only; see Milestone D).
- **Human gate**: distinct node type that owns the existing decision controls
  (reuse `GraphV1ApprovalCard` internals, repositioned as the node's body).

**Step 2:** Keep `cancel` + approval **exactly as-is** in behavior; only the
presentation container changes. Keep the markdown-`React text children` rule
(finding #51) — worker text stays literal children.

### Task 6: Keep the approval & cancellation path honest

**Objective:** No behavior drift while reskinning.

**Files:**
- Modify: `tests/test_dashboard_graph_v1.py` (assert decision/cancel endpoints unchanged)
- Modify: `dashboard/conformance-harness.js`

**Step 1:** Assert endpoint contracts byte-identical (the projection shape is the only
surface that gained fields). **Step 2:** conformance harness drives a live Run: launch
`plan-build-review`, observe nodes render with real telemetry, approve at the human
gate, reach End. **Step 3:** full dashboard-focused suite green.

---

## Milestone C — Viewer verification + DNS-style guard

### Task 7: Bundle + conformance guard

**Objective:** Lock the canvas so a future bundle edit can't silently drop a node or
edge.

**Files:**
- Create: `dashboard/conformance/graphcanvas.spec.mjs`
- Modify: `dashboard/conformance-harness.vite.mjs`, `dashboard/conformance-harness.js`

**Step 1:** Spec asserts: every (box, arrow) from the endpoint appears as a rendered
node/edge; rework arrows are dashed-loop; a `waiting_human` node renders the human
gate; telemetry row renders when present. Fail the harness on any regression.
**Step 2:** Wire into the existing bundle-guard + conformance flow; run the real
`GET /v1/runs/{id}/graph` journey.

### Task 8: Prove on the real journey + review gate

**Objective:** Confirm operator behavior, not just unit green.

**Files:**
- `dashboard/conformance-evidence/` — capture real screenshots (mirror the `v3/`
  evidence convention)

**Step 1:** One real journey through the redesign: launch, watch planner→builder→
three parallel reviews→join→human gate→approve→End, capture each state.
**Step 2:** Two independent reviews on the same tree (semantic: canvas shows only
boxes/arrows/attempts/waits/End; durability: cancel/approve/decision unchanged).

---

## Milestone D — Recipe Designer (constrained grammar editor)

### Task 9: Add a read/write recipe editor endpoint surface

**Objective:** Author recipes through a POST that validates with the *same* pure
validator before persisting — nothing invalid can ever be saved.

**Files:**
- Modify: `dashboard/plugin_api.py`
- Modify: `shipfactory/graph_recipe.py` (expose a `canonical_text(recipe)` writer)
- Create: `tests/test_graph_projects_api_v1_designer.py`

**Mutable recipe lifecycle:** the designer edits **unpublished** recipes only. Existing
sha-pinned published recipes are immutable (already enforced). Save = validate →
compute new hash → write YAML into `recipes/v1/`.

**Step 1 (RED):** POST `PUT /v1/recipes/{name}/design` accepts a YAML doc; valid docs
round-trip (save → load → byte-identical); invalid docs (unknown box key, duplicate id,
no End, unreachable box, multi-End) return the exact validator error and persist
nothing.
**Step 2:** implement `canonical_text` (reverse of `validate`'s parse) + endpoint.

### Task 10: Designer surface in the dashboard

**Objective:** Reuse `GraphV1Canvas` as the live preview; edit through structured
controls that emit canonical YAML — never freeform pixel-dragging.

**Files:**
- Modify: `dashboard/dist/index.js` (new `GraphV1Designer`)
- Modify: `dashboard/dist/style.css`
- Create: `dashboard/conformance/graphdesigner.spec.mjs`

**Step 1:** Split graph into **Edit Pane** (form: reorder boxes, set `who`, live
`sha` on save) + **Preview Canvas** (the exact `GraphV1Canvas` from Milestone B).
**Step 2:** Actions constrained to the grammar: add box, connect boxes with a result
label, relabel a result, change `who`, mark `end`. Every action re-runs the local
validator-derived checks; invalid transitions are blocked with the exact validator
message. Generate an ASCII preview (`docs/recipe-structure-v1.md` style) alongside.
**Step 3:** Save writes canonical YAML; the designer never stores a second object
model — YAML string + validation result **is** the state.

### Task 11: Designer verification

**Files:**
- `dashboard/conformance/graphdesigner.spec.mjs` + harness wiring

**Step 1:** Conformance: recreate `plan-build-review` via the designer gating every
valid path; assert each rejected edit exactly matches the runtime validator's message
(proving no drift between designer and engine grammar). **Step 2:** full suite once.

---

## Files touched (recap)

- `shipfactory/store.py` (additive migration 18)
- `shipfactory/graph_recipe.py` (`canonical_text` writer)
- `shipfactory/graph_runner.py`, `graph_runtime.py` (telemetry plumbing)
- `shipfactory/recipe_graph.py` (`project_direct_graph_v1` telemetry)
- `dashboard/plugin_api.py` (designer endpoint)
- `dashboard/dist/index.js`, `dashboard/dist/style.css` (canvas + designer)
- `dashboard/conformance-harness.js`, `conformance/*.mjs`
- Tests: `test_graph_store_v1_telemetry.py` (new), `test_graph_runtime_v1.py`,
  `test_graph_runner_v1.py`, `test_dashboard_graph_v1.py`,
  `test_graph_projects_api_v1_designer.py` (new)

## Tests / validation

- Task-by-task RED/GREEN as specified; focused gate then full suite once at end of
  each milestone, per the canonical test prefix.
- Conformance harness drives the **real HTTP endpoint** — a green unit suite is not
  evidence; a live journey to End through the operator surface is.

## Risks / tradeoffs / open questions

1. **Designer scope creep (highest risk):** a freeform canvas would violate the frozen
   v1 grammar. Mitigation: designer output **is** canonical YAML through the same
   validator; no second object model.
2. **Telemetry retrofit cost:** v1 attempts lack token/duration today. Scoped to an
   additive column + runtime plumbing; a completion without telemetry still succeeds.
3. **No new deps / no build step** — intentionally. Hand-rolled SVG matches the
   existing conformance harness and Abhi's no-extraneous-dependency preference.
4. **Open:** should the "Visual Judge" glyph stay a plain `who: human` gate (my take —
   yes) or gain any automated visual check? Default: human only, per the boundary.
5. **Open:** ASCII recipe preview in the designer — include (yes for operator
   diffs/audit), or is the canvas enough?