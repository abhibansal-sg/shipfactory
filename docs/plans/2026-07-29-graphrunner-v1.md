# GraphRunner v1 Implementation Plan

> **For Hermes:** Use the `subagent-driven-development` skill to implement this plan task-by-task. Do not begin the legacy deletion phase until its explicit drain gate passes.

**Goal:** Execute the operator-ratified Recipe Structure v1 through a small, durable GraphRunner while existing runs remain pinned to the unchanged legacy engine.

**Architecture:** Add a pure v1 recipe validator and event-driven GraphRunner beside the legacy recipe package. New-shape recipes are snapshotted into new additive SQLite tables; a single daemon writer advances boxes and arrows, while existing executor/process infrastructure runs model boxes and enqueues completion events. Human boxes use a generic bound decision. No active legacy run is converted.

**Tech stack:** Python 3.11, stdlib dataclasses/JSON/hashlib/SQLite, PyYAML already used by the repository, Hermes Kanban/executor adapters, pytest.

---

## Non-negotiable boundaries

1. The runtime grammar is exactly `docs/recipe-structure-v1.md:8-102`:
   - recipe: `name`, `start`, `boxes`, `arrows`;
   - box: `id`, `name`, `who`, `instructions`, optional `end: true`;
   - arrow: `from`, `result`, `to`.
2. Do not add artifact, review, verification, workspace, budget, policy, or retry fields to a box.
3. A box completion contains produced work plus one result label. A result label matches `[a-z][a-z0-9-]*`.
4. `who: human` is the only human-box signal. It never invokes an executor.
5. Three consecutive technical failures on one box pause the Run. A valid result resets that box's failure streak.
6. An arrow whose destination appears at or before its source in declared box order is a rework arrow. Three traversals of the same rework arrow pause the Run with complete work/feedback history.
7. The exact parsed recipe document and canonical SHA-256 are stored on the Run. Runtime code never rereads recipe files for an existing Run.
8. All state transitions are applied by GraphRunner under `BEGIN IMMEDIATE`. CLI/dashboard/executor completion only enqueue events.
9. Existing `recipe_instances`, `recipe_steps`, `advance_events`, recipes, APIs, and daemon behavior remain available for legacy runs.
10. No legacy table or file is dropped in this implementation plan.

## Deliberate simplicity choices

- **No rollout toggle:** recipe shape selects the runner. Legacy documents containing `steps` go to LegacyRunner; v1 documents containing `boxes` and `arrows` go to GraphRunner. This is deterministic and hot-reloadable without another setting.
- **One new recipe directory:** v1 files live under the configured legacy library root's `v1/` directory. The legacy loader is non-recursive, so old publication behavior is untouched.
- **No new workflow abstraction:** use functions and small immutable dataclasses, not a node/primitive/plugin class hierarchy.
- **No Kanban collector:** the Run is the parent. Executor tasks may remain an internal transport during adaptation but never become the public containment model.
- **No big-bang dashboard rewrite:** add the new endpoints and views first; keep legacy views read-only until the drain gate.

## Target modules

### New production files

- `shipfactory/graph_recipe.py` — v1 parse, canonicalize, validate, load.
- `shipfactory/graph_runner.py` — run creation, event leasing/application, route reduction, split/join, retries, escalation, End.
- `shipfactory/graph_runtime.py` — thin bridge from ready box attempts to existing executor/process infrastructure and from reaping to GraphRunner events.

### Existing production files touched before cutover

- `shipfactory/store.py` — additive migration 17 and narrow GraphRunner persistence functions.
- `shipfactory/spawn.py` — reusable generic completion parser and graph-target reap callback; legacy path unchanged.
- `shipfactory/daemon.py` — invoke GraphRunner apply/reconcile/runtime ticks beside LegacyRunner.
- `shipfactory/decisions.py` — generic human-box decision enqueue path beside legacy gate decisions.
- `shipfactory/recipe_graph.py` — direct v1 projection beside legacy projection.
- `dashboard/plugin_api.py` — v1 recipe/run/human-decision endpoints beside legacy routes.
- `dashboard/dist/index.js` — v1 graph/run projection and human box UI.
- `shipfactory/cli.py` — v1 recipe/run commands; retain compatibility commands.
- `shipfactory/__init__.py` — register new commands only; no legacy removal.

### New focused tests

- `tests/test_graph_recipe_v1.py`
- `tests/test_graph_store_v1.py`
- `tests/test_graph_runner_v1.py`
- `tests/test_graph_runtime_v1.py`
- `tests/test_graph_human_v1.py`
- `tests/test_graph_projects_api_v1.py`
- `tests/test_dashboard_graph_v1.py`
- `tests/test_graph_daemon_v1.py`
- `tests/test_graph_journey_v1.py`

## Canonical command prefix

Run tests from `/Volumes/MainData/Developer/products/shipfactory`:

```bash
ulimit -n 4096
WT=/Volumes/MainData/Developer/worktrees/hermes-shipfactory-recipe-apis
export PYTHONPATH="$WT"
export HERMES_MOBILE_PATH="$WT"
PY=/Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python
```

Every test command below assumes this prefix is active in the persistent terminal session.

---

## Milestone A — Pure grammar and durable state

### Task 1: Lock the executable v1 recipe fixture

**Objective:** Make the ratified paper recipe available as runtime YAML without letting it drift from the document.

**Files:**
- Create: `recipes/v1/plan-build-review.yaml`
- Create: `tests/test_graph_recipe_v1.py`
- Read contract: `docs/recipe-structure-v1.md:8-102`

**Step 1: Write the failing test**

Add a test that extracts the first fenced YAML block from `docs/recipe-structure-v1.md`, parses it with `yaml.safe_load`, parses `recipes/v1/plan-build-review.yaml`, and asserts structural equality.

**Step 2: Verify RED**

```bash
$PY -m pytest tests/test_graph_recipe_v1.py::test_runtime_recipe_matches_ratified_document -q
```

Expected: FAIL because the runtime YAML does not exist.

**Step 3: Add the runtime fixture**

Copy the YAML bytes from the ratified document exactly; do not add schema, version, description, parameters, budgets, or primitive fields.

**Step 4: Verify GREEN**

```bash
$PY -m pytest tests/test_graph_recipe_v1.py::test_runtime_recipe_matches_ratified_document -q
```

Expected: `1 passed`.

**Step 5: Checkpoint**

Stage only the fixture and focused test. Commit only when implementation commits are explicitly authorized.

### Task 2: Implement the exact v1 validator

**Objective:** Accept only the ratified grammar and reject malformed or ambiguous graphs before any state is written.

**Files:**
- Create: `shipfactory/graph_recipe.py`
- Modify: `tests/test_graph_recipe_v1.py`

**Required public interface:**

```python
class GraphRecipeError(ValueError): ...

@dataclass(frozen=True)
class GraphRecipe:
    name: str
    start: str
    boxes: tuple[dict[str, object], ...]
    arrows: tuple[dict[str, object], ...]
    document: dict[str, object]
    canonical_json: str
    hash: str


def validate(document: object) -> GraphRecipe: ...
def load(path: Path) -> GraphRecipe: ...
def load_library(path: Path) -> dict[str, GraphRecipe]: ...
```

**Step 1: Write parameterized RED tests**

Cover:

- unknown top-level, box, and arrow keys;
- missing/blank/duplicate IDs, names, `who`, instructions, and results;
- invalid `to` shape or duplicate destination;
- unknown start/source/destination;
- no End or multiple Ends;
- outgoing arrow from End;
- ordinary dead end;
- duplicate `(from, result)` declarations;
- unreachable box;
- non-string/scalar surprises;
- a valid backward arrow and a valid multi-destination split.

Assert errors name the exact field or box.

**Step 2: Verify RED**

```bash
$PY -m pytest tests/test_graph_recipe_v1.py -q
```

Expected: FAIL because `shipfactory.graph_recipe` is absent.

**Step 3: Implement the minimum validator**

Use `json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` for canonical bytes and SHA-256. Preserve box and arrow declaration order in the parsed tuples. Do not introduce Pydantic or another dependency.

**Step 4: Verify GREEN**

```bash
$PY -m pytest tests/test_graph_recipe_v1.py -q
```

Expected: all focused tests pass.

### Task 3: Implement pure routing metadata

**Objective:** Precompute only what GraphRunner needs: box lookup, outgoing result routes, incoming arrows, End identity, and rework-arrow identity.

**Files:**
- Modify: `shipfactory/graph_recipe.py`
- Modify: `tests/test_graph_recipe_v1.py`

**Required methods:**

```python
recipe.box(box_id)
recipe.destinations(box_id, result) -> tuple[str, ...]
recipe.incoming(box_id) -> tuple[tuple[str, str], ...]
recipe.is_end(box_id) -> bool
recipe.is_rework_arrow(source_id, result) -> bool
```

`is_rework_arrow` uses declared box order; it must not infer meaning from labels such as `rework`, `revise`, or `rejected`.

**RED/GREEN command:**

```bash
$PY -m pytest tests/test_graph_recipe_v1.py -q
```

Add tests proving a forward result named `rework` is not counted and a backward result named `done` is counted.

### Task 4: Add migration 17 without touching legacy tables

**Objective:** Persist v1 runs and transitions additively beside schema version 16.

**Files:**
- Modify: `shipfactory/store.py:394-429`
- Create: `tests/test_graph_store_v1.py`
- Modify latest-version assertions in:
  - `tests/test_verification.py:127-131`
  - `tests/test_artifact_foundation.py:131-135`
  - `tests/test_environment_sessions.py:450-455`
  - `tests/test_gate_decisions.py:428-434`
- Preserve the exact migration-16 contract in `tests/test_project_recipe_policy.py:31-42` by
  selecting migration 16 explicitly instead of using `store._MIGRATIONS[-1]`.

**Migration 17 tables:**

```sql
recipe_runs_v1(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  board TEXT NOT NULL,
  recipe_name TEXT NOT NULL,
  recipe_hash TEXT NOT NULL,
  recipe_snapshot_json TEXT NOT NULL,
  request_text TEXT NOT NULL,
  workspace_path TEXT,
  launch_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  blocked_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
)

box_attempts_v1(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  box_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  state TEXT NOT NULL,
  executor_run_id INTEGER,
  input_work_json TEXT NOT NULL,
  output_work TEXT,
  result TEXT,
  technical_failure TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id, box_id, ordinal),
  FOREIGN KEY(run_id) REFERENCES recipe_runs_v1(id)
)

route_tokens_v1(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  source_attempt_id TEXT,
  arrow_index INTEGER,
  destination_box_id TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  work_refs_json TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  UNIQUE(run_id, source_attempt_id, arrow_index, destination_box_id),
  FOREIGN KEY(run_id) REFERENCES recipe_runs_v1(id)
)

split_groups_v1(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  parent_lineage_json TEXT NOT NULL,
  branch_ids_json TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  FOREIGN KEY(run_id) REFERENCES recipe_runs_v1(id)
)

run_events_v1(
  key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  source TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  outcome TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  applied_at TEXT,
  FOREIGN KEY(run_id) REFERENCES recipe_runs_v1(id)
)

human_box_decisions_v1(
  id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL UNIQUE,
  result TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  nonce_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  event_key TEXT NOT NULL UNIQUE,
  FOREIGN KEY(attempt_id) REFERENCES box_attempts_v1(id)
)

project_recipes_v1(
  project_id TEXT NOT NULL,
  recipe_name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, recipe_name)
)
```

Add indexes only for daemon hot paths: pending events, ready box attempts, active route tokens, and active runs.

**Step 1: Write RED migration tests**

Assert exact table/column/index shape, migration checksum replay, partial-application refusal, and preservation of all version-16 tables.

**Step 2: Verify RED**

```bash
$PY -m pytest tests/test_graph_store_v1.py tests/test_project_recipe_policy.py::test_migration_16_is_idempotent_checksum_disciplined_and_has_exact_schema -q
```

**Step 3: Add migration 17 and its partial-application detector**

Follow the additive/checksummed pattern at `shipfactory/store.py:529-548`. Do not edit migrations 1–16.

**Step 4: Verify GREEN and normative version drift**

```bash
$PY -m pytest tests/test_graph_store_v1.py tests/test_verification.py::test_schema_migration_is_normative_and_numbered tests/test_artifact_foundation.py tests/test_environment_sessions.py::test_app_identity_migration_is_durable_and_numbered tests/test_project_recipe_policy.py tests/test_gate_decisions.py -q
```

Expected: all selected tests pass, every normative latest-version assertion is 17,
and the exact migration-16 contract still verifies version 16 by explicit lookup.

### Task 5: Add narrow transactional store functions

**Objective:** Keep SQL ownership in `store.py` while preventing GraphRunner from scattering ad hoc queries.

**Files:**
- Modify: `shipfactory/store.py`
- Modify: `tests/test_graph_store_v1.py`

**Functions:**

```python
create_recipe_run_v1(...)
get_recipe_run_v1(run_id)
list_recipe_runs_v1(...)
insert_box_attempt_v1(...)
update_box_attempt_v1(...)
insert_route_token_v1(...)
consume_route_tokens_v1(...)
enqueue_run_event_v1(...)
lease_run_events_v1(...)
finish_run_event_v1(...)
record_human_box_decision_v1(...)
```

Functions participating in one transition accept an existing SQLite connection. Public wrappers may open their own connection; GraphRunner's apply path must use one transaction.

Test launch-key idempotency, duplicate token idempotency, event lease expiry, stale event discard, and connection closure.

**Focused command:**

```bash
$PY -m pytest tests/test_graph_store_v1.py -q
```

---

## Milestone B — GraphRunner behavior

### Task 6: Start a frozen Run and activate exactly one start box

**Objective:** Create a Run from a v1 recipe snapshot without collectors or legacy recipe rows.

**Files:**
- Create: `shipfactory/graph_runner.py`
- Create: `tests/test_graph_runner_v1.py`

**Interface:**

```python
def start_run(*, project_id, board, recipe, request_text, launch_key, workspace_path=None) -> dict: ...
def reconcile_run(conn, run_id: str) -> dict: ...
def enqueue_event(*, run_id, source, payload, key) -> dict: ...
def apply_events(*, owner: str, limit: int = 100) -> dict: ...
```

Starting twice with the same launch key returns the same Run. Starting stores `recipe.document` and `recipe.hash`, inserts one token for `start`, and leaves all legacy tables untouched.

**RED/GREEN command:**

```bash
$PY -m pytest tests/test_graph_runner_v1.py -q
```

### Task 7: Activate sequential boxes and route by result

**Objective:** Turn a ready token into one box attempt and a successful completion into matching destination tokens.

**Files:**
- Modify: `shipfactory/graph_runner.py`
- Modify: `tests/test_graph_runner_v1.py`

**Behavior tests:**

- one pending token creates one attempt once;
- the attempt input contains request plus immutable references to preceding output work;
- valid completion records work and result atomically;
- no matching arrow pauses with `unroutable_result`;
- duplicate completion event is spent, not replayed;
- reaching End marks the Run completed only after the End box itself completes;
- ordinary dead end cannot arise because loader rejects it.

Use direct event fixtures; do not involve subprocesses yet.

### Task 8: Implement split and active-path join

**Objective:** Execute the canonical builder → three reviews → synthesis fan-out/fan-in with durable lineage.

**Files:**
- Modify: `shipfactory/graph_runner.py`
- Modify: `tests/test_graph_runner_v1.py`

**Algorithm boundary:**

- A result with multiple destinations creates one `split_groups_v1` row and one child lineage per destination.
- Every downstream route token preserves lineage.
- A box with multiple incoming arrows is a join candidate.
- The join waits while any live sibling lineage can still reach an unsatisfied incoming source through forward arrows.
- A sibling whose chosen result routes away from the join is no longer expected.
- When all active sibling lineages that can reach the join have arrived, one attempt is created with all their work references; the split group closes and lineage collapses to the parent.
- Backward arrows never participate in forward reachability for join readiness.

**Required tests:**

1. Three direct sibling reviews finish in every permutation; synthesis starts once.
2. Duplicate delivery does not start synthesis twice.
3. One slow sibling keeps synthesis waiting.
4. A conditional sibling routed away is not awaited.
5. Restart between the second and third arrival preserves the wait.
6. Nested split lineage does not mix work from another split generation.

**Focused command:**

```bash
$PY -m pytest tests/test_graph_runner_v1.py -k 'split or join or lineage' -q
```

### Task 9: Implement both three-strike brakes

**Objective:** Bound infrastructure churn and semantic correction loops without budgets or domain policy.

**Files:**
- Modify: `shipfactory/graph_runner.py`
- Modify: `tests/test_graph_runner_v1.py`

**Technical failure:** `box_failed` events create a retry attempt with the same inputs. A valid completion resets the consecutive streak. The third consecutive failure sets Run state `escalated` and records box, attempts, and failure history.

**Rework:** when a backward arrow is traversed, count prior consumed tokens for that exact arrow index in the current Run. The third traversal sets Run state `escalated` before creating another destination attempt. Escalation output includes all outputs and result labels since the previous traversal of that arrow.

**Tests:** failures 1/2 retry, failure 3 escalates; success resets; two different backward arrows have independent counters; forward arrows with labels `revise`/`rework` do not count.

### Task 10: Make event application crash-safe and single-writer

**Objective:** Preserve the proven event lease/idempotency discipline without copying legacy event meanings.

**Files:**
- Modify: `shipfactory/graph_runner.py`
- Modify: `tests/test_graph_runner_v1.py`

**Tests:**

- two concurrent appliers cannot lease the same event;
- crash after lease expiry returns event to pending;
- event whose expected attempt/state is stale becomes `discarded` with reason;
- apply exception records `failed` and leaves Run unchanged;
- transition transaction contains no external Kanban/process call;
- permanent event key reuse is rejected.

Use `BEGIN IMMEDIATE`, bounded lease owner IDs, and deterministic keys. Reuse patterns, not payloads, from `shipfactory/recipes/advancer.py`.

---

## Milestone C — Executors and human boxes

### Task 11: Add the generic completion envelope without changing legacy parsing

**Objective:** Parse arbitrary declared result labels and treat all preceding extracted text as produced work.

**Files:**
- Modify: `shipfactory/spawn.py:696-714`
- Create: `tests/test_graph_runtime_v1.py`

**New parser:**

```python
def parse_graph_completion(text: str, exit_code: int) -> tuple[str, str]:
    """Return (result_label, produced_work); raise on technical failure."""
```

The final non-empty physical line must be exactly:

```text
SHIPFACTORY_RESULT: <label>
```

`<label>` matches the v1 result regex. Exit code must be zero. Produced work is all prior extracted text, preserved verbatim except removal of the sentinel line. Require non-empty produced work uniformly.

Keep `_parse_result` and `SHIPFACTORY_VERDICT` behavior byte-identical for legacy tasks.

**Tests:** arbitrary labels, malformed labels, extra text after sentinel, nonzero exit, missing sentinel, empty work, Unicode work, and legacy regression.

### Task 12: Add a graph-target executor adapter

**Objective:** Reuse process supervision without forcing GraphRunner through legacy primitive/task/artifact logic.

**Files:**
- Create: `shipfactory/graph_runtime.py`
- Modify: `shipfactory/spawn.py:516-693`
- Modify: `shipfactory/spawn.py:717-864`
- Modify: `tests/test_graph_runtime_v1.py`

**Implementation seam:**

Extract the current executor launch core behind an internal target record:

```python
{
  "target_kind": "legacy_task" | "graph_box",
  "target_id": task_id | attempt_id,
  "board": board,
  "workspace_path": workspace,
  ...
}
```

Legacy `shipfactory_spawn()` remains a compatibility wrapper. `graph_runtime.spawn_ready()` resolves `box.who` through live seat config, renders request + preceding work + exact instructions + completion contract, launches the adapter, and binds the durable executor run ID to the box attempt.

On reap:

- legacy target: preserve current board transition and artifact sealing path;
- graph target: parse the generic envelope and enqueue exactly one `box_completed` or `box_failed` event; never import `shipfactory.artifacts`.

**Tests:** command argv/environment parity, durable run before spawn, PID/start-token adoption, lease release, no artifact sealing for graph targets, duplicate reap idempotency, and technical failure classification.

### Task 13: Implement protected generic human decisions

**Objective:** Pause `who: human` attempts and route only an authorized human's declared result.

**Files:**
- Modify: `shipfactory/decisions.py`
- Create: `tests/test_graph_human_v1.py`

**Interface:**

```python
def enqueue_human_box_decision(*, attempt_id, result, actor_kind, actor_id, channel, nonce) -> dict: ...
```

Checks inside one writer transaction:

- attempt exists and is `waiting_human`;
- exact Run snapshot declares `who: human`;
- result has a declared outgoing arrow;
- `actor_kind == "human"`;
- nonce replay with identical tuple returns prior decision;
- nonce or attempt conflict fails closed;
- decision insert and `run_events_v1` insert commit together.

The function cannot mark the attempt or Run complete. GraphRunner applies the event on the next tick.

**Focused command:**

```bash
$PY -m pytest tests/test_graph_human_v1.py -q
```

### Task 14: Compose GraphRunner into the daemon

**Objective:** Advance v1 Runs on every daemon tick without altering LegacyRunner ordering or failure isolation.

**Files:**
- Modify: `shipfactory/daemon.py:279-351`
- Create: `tests/test_graph_daemon_v1.py`

**Tick order:**

1. reap existing executor processes;
2. apply v1 completion/human events;
3. reconcile v1 Runs;
4. spawn ready v1 attempts up to existing worker capacity;
5. run the unchanged legacy event/reconcile path;
6. run existing outbox/watchdog/sync work.

If GraphRunner fails for one board, report its error in the tick result and continue other board handling; do not suppress `--require-recipes` fail-closed behavior.

**Tests:** empty v1 tick is a no-op, graph and legacy work in the same tick, one side failing does not falsely report the other as successful, second daemon remains blocked by singleton lock.

---

## Milestone D — Operator surface

### Task 15: Project attachment and idempotent v1 launch API

**Objective:** Let a Project attach/editable v1 recipes and start a frozen Run from its primary workspace.

**Files:**
- Modify: `dashboard/plugin_api.py:1243-1253`
- Modify: `dashboard/plugin_api.py:1528-1538`
- Create: `tests/test_graph_projects_api_v1.py`
- Modify: `shipfactory/cli.py`

**Endpoints:**

```text
GET  /shipfactory/v1/recipes
GET  /shipfactory/v1/recipes/{name}
GET  /shipfactory/v1/projects/{project_id}/recipes
PUT  /shipfactory/v1/projects/{project_id}/recipes/{name}
POST /shipfactory/v1/projects/{project_id}/runs
GET  /shipfactory/v1/runs/{run_id}
GET  /shipfactory/v1/runs/{run_id}/graph
POST /shipfactory/v1/human-boxes/{attempt_id}/decision
```

The URL namespace version is API compatibility, not recipe publication version. Launch resolves the selected Hermes project's `primary_path`, validates it, reads the current v1 recipe once, and passes exact bytes/hash/workspace into `start_run`. Client retry uses a required launch key.

**CLI nouns:** `recipe list/show`, `run start/show/list`, and `run decide`. Do not expose instance, step activation, primitive, collector, or flight in new output.

**Tests:** read-only GETs never initialize/migrate DBs; project recipe allowlist; missing/disabled recipe; launch replay; recipe edit affects the next Run only; workspace comes from project primary path, not process CWD; human decision 409/422 boundaries.

### Task 16: Direct graph projection and dashboard rendering

**Objective:** Display the declared recipe directly and overlay Run state without synthetic routers or primitive shapes.

**Files:**
- Modify: `shipfactory/recipe_graph.py`
- Modify: `dashboard/dist/index.js`
- Create: `tests/test_dashboard_graph_v1.py`
- Modify: `dashboard/conformance-harness.js`

**Projection:**

```json
{
  "recipe": {"name": "...", "start": "...", "hash": "..."},
  "boxes": [{"id": "...", "name": "...", "who": "...", "instructions": "...", "end": false}],
  "arrows": [{"from": "...", "result": "...", "to": ["..."]}],
  "run": {"id": "...", "state": "...", "attempts": [], "waiting_human": []}
}
```

No diamonds, synthetic review routers, primitive metadata, budgets, needs, typed inputs, or output schemas.

Render all model work, instructions, actor fields, and result labels as React text children. The human box offers only its declared result labels and posts attempt ID + result + fresh nonce + actor/channel.

**Verification:** focused Python projection tests, dashboard bundle guard, then conformance harness.

---

## Milestone E — End-to-end proof and controlled cutover

### Task 17: Execute the ratified recipe through an in-process deterministic executor

**Objective:** Prove every structural behavior without model nondeterminism.

**Files:**
- Create: `tests/test_graph_journey_v1.py`

Drive the exact `recipes/v1/plan-build-review.yaml` with scripted outputs:

1. planner `done`;
2. plan review `revise`, planner `done`, plan review `approved`;
3. builder `done`;
4. three reviews complete in non-declaration order;
5. synthesis waits, then returns `rework`;
6. builder and reviews run in a new generation;
7. synthesis `pass`;
8. human box pauses across a simulated daemon restart;
9. human result `approved`;
10. final delivery completes End.

Assert frozen recipe bytes, exact attempt counts, combined join work, spent events, no duplicate spawn, no legacy instance/step/collector rows, and completed Run.

Add separate journeys for technical failure ×3 and same-loop traversal ×3 escalation.

### Task 18: Prove live subprocess execution and restart recovery

**Objective:** Exercise the actual spawn/reap path, not only the reducer.

**Files:**
- Modify: `tests/test_graph_journey_v1.py`

Use `FACTORY_EXECUTOR_CMD_*` with a real subprocess that reads stdin and emits work plus the final sentinel. Prove:

- prompt contains request, exact instructions, and preceding work;
- process row and worker lease exist before PID attachment;
- restart adoption requires PID/start-token match;
- completion enqueues before GraphRunner applies;
- crash/timeout becomes technical failure;
- successful graph box never invokes artifact sealing;
- executor log and output work remain auditable.

### Task 19: Run focused gates, then the full suite once

**Objective:** Demonstrate coexistence without repeatedly paying for the full suite.

**Focused gate:**

```bash
$PY -m pytest \
  tests/test_graph_recipe_v1.py \
  tests/test_graph_store_v1.py \
  tests/test_graph_runner_v1.py \
  tests/test_graph_runtime_v1.py \
  tests/test_graph_human_v1.py \
  tests/test_graph_projects_api_v1.py \
  tests/test_graph_daemon_v1.py \
  tests/test_graph_journey_v1.py -q
```

Expected: all focused tests pass.

**Legacy coexistence gate:**

```bash
$PY -m pytest \
  tests/test_recipes.py \
  tests/test_spawn.py \
  tests/test_gate_decisions.py \
  tests/test_project_recipe_policy.py \
  tests/test_graph_projection.py \
  tests/test_graph_api.py -q
```

Expected: all legacy-selected tests pass unchanged except normative schema version updates.

**Full gate:**

```bash
$PY -m pytest tests/ -q
```

Expected: full suite passes. Record the actual count and duration; do not predict them.

### Task 20: Run one real project journey before defaulting new recipes to v1

**Objective:** Verify actual operator behavior, not only tests.

**Procedure:**

1. Back up the live ShipFactory DB.
2. Restart the daemon so migration 17 and GraphRunner code are loaded.
3. Attach `plan-build-review` to a non-production test Project.
4. Start through the dashboard with a unique launch key.
5. Observe planner, branching, parallel reviews, join, human pause, decision, and End.
6. Restart the daemon once while waiting at the human box.
7. Confirm the dashboard still shows the exact Run and declared choices.
8. Approve manually; agents never press the human button.
9. Verify receipts, process cleanup, and zero mutation of legacy run rows.

Do not describe the system as ready until this journey reaches End through the operator surface.

### Task 21: Add the legacy drain report—do not delete yet

**Objective:** Make deletion eligibility observable and fail closed.

**Files:**
- Modify: `shipfactory/cli.py`
- Modify: `dashboard/plugin_api.py`
- Create: `tests/test_legacy_drain_report.py`

Report:

- active legacy instances by project/recipe;
- project defaults still pointing to legacy recipes;
- legacy-only API clients observed in access telemetry;
- last legacy activity timestamp;
- backup/archive readiness;
- each deletion-gate condition from `docs/engine-deletion-map-v1.md`.

The report is read-only. It must never cancel, migrate, or mark legacy work complete. Legacy deletion is a separate operator-ratified plan after the report is fully green.

---

## Review gates during implementation

After each milestone:

1. Run the milestone's focused tests.
2. Review the diff against `docs/recipe-structure-v1.md` for any new public field or domain primitive.
3. Review transaction boundaries: no external process/Kanban call inside a GraphRunner write transaction.
4. Review operator authority: no code path may synthesize or apply a human result.
5. Review all legacy diffs for behavior drift.
6. Commit only the milestone's intended files with Abhinav Bansal's configured public author and no AI trailer.

Before the real journey, require two independent reviews on the same integrated tree:

- **semantic review:** the engine knows only boxes, arrows, work, results, routing, joins, retries, human waits, and End;
- **durability/security review:** leases, event spending, process identity, human authorization, and legacy isolation remain fail closed.

## Stop conditions

Stop and return to the operator if any implementation step requires:

- adding a work-specific field to the recipe grammar;
- modifying a published legacy recipe;
- converting an active legacy run;
- weakening the human-only decision boundary;
- performing external effects inside a GraphRunner transaction;
- adding a second configuration file or a hardcoded rollout switch;
- making GraphRunner depend on artifacts, verification, environments, policy, or primitive modules;
- changing more legacy behavior than required for the side-by-side adapter.

## Definition of done for this plan

- The exact ratified recipe executes to End through GraphRunner.
- Sequence, result branching, split, active-path join, two backward loops, human pause, three technical failures, three same-loop corrections, and frozen snapshots are behaviorally proven.
- New public surfaces use only Project, Recipe, Run, Box, and Arrow.
- Existing legacy runs and their tests remain operational.
- One real dashboard journey survives a daemon restart and reaches End after a human decision.
- A read-only drain report exists.
- No legacy subsystem is deleted yet.

At that point, write a short, evidence-based deletion plan using the live drain report. Do not reuse this build plan as permission to remove legacy state.
