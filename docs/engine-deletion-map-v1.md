# ShipFactory Engine Deletion Map v1

**Status:** source-grounded audit against operator-ratified Recipe Structure v1  
**Scope:** current `main`; read-only analysis, no engine changes  
**Safety line:** new Recipe Structure v1 runs use a new runner; existing runs remain pinned to the legacy runner until they finish or are explicitly cancelled.

## Verdict

The engine is overbuilt for the product now defined.

The ratified structure requires a generic graph runner: execute a box, store its work and result, follow matching arrows, split, join, pause for a human, stop at End, and escalate bounded failures (`docs/recipe-structure-v1.md:129-168`).

The current engine instead understands software-development domain objects and policies: primitive types, typed artifacts, review verdict schemas, Git identities, verification manifests, evidence bundles, surface classification, rework cones, and exceptional recovery events. Those controls are internally coherent, but they implement one opinionated development pipeline inside the engine instead of moving generic work through a graph.

**Recommendation:** do not simplify the current advancer in place. Introduce a small GraphRunner beside it, send only new v1 runs to GraphRunner, drain legacy runs unchanged, then delete the legacy semantic layer.

## Measured current shape

Measured from the checked-out source:

| Area | Current source lines | Observation |
|---|---:|---|
| Recipe core (`loader`, `instantiate`, `advancer`, `primitives`, graph projection) | 4,352 | Routing is inseparable from domain policy. |
| Artifact, verification, environment, and pytest-evidence machinery | 6,720 | The largest subsystem teaches the engine what software evidence means. |
| Execution (`spawn` plus executor adapters) | 1,402 | Mostly reusable infrastructure. |
| Selection, legacy policy, and human decisions | 1,689 | Contains both useful boundaries and duplicate workflow semantics. |
| Dashboard API and bundle | 4,531 | Exposes legacy vocabulary and schema directly. |
| Entire Python package | 18,492 | Excludes tests and dashboard JavaScript. |
| Python tests | 20,161 | The old semantic contract has more test code than production Python code. |

These numbers are an inventory, not a deletion target. Generic safety infrastructure should be preserved even when its current domain-specific caller disappears.

## Target ownership boundary

### Models and executors own

- understanding the work;
- planning, building, reviewing, verifying, notifying, or synthesizing;
- deciding the box's result label;
- producing the box's output.

### ShipFactory owns

- the frozen recipe snapshot;
- run and box-attempt state;
- delivering preceding work to a box;
- invoking the named executor safely;
- recording output and one result label;
- matching that result to arrows;
- split and active-path join bookkeeping;
- technical retry and rework-loop counters;
- protected human pauses;
- idempotency, leases, timeouts, logs, and escalation.

The current schema violates this boundary at its source: `loader.py` enumerates six domain primitives and validates their distinct parameters (`shipfactory/recipes/loader.py:29-34`, `shipfactory/recipes/loader.py:248-287`). Recipe Structure v1 needs only ordinary boxes and result-labelled arrows (`docs/recipe-structure-v1.md:12-102`).

## Keep

These mechanisms solve infrastructure problems that remain true for any graph.

| Mechanism | Current evidence | Keep as |
|---|---|---|
| Executor adapters | `shipfactory/executors/` and `shipfactory/spawn.py:18-23` | Pluggable implementation of `who`; adapters return opaque work plus a result label. |
| Durable process identity and reaping | `shipfactory/spawn.py:139-238` | Generic box-attempt supervision. |
| Worker isolation and secret-safe environment | `shipfactory/spawn.py:70-97` | Generic executor boundary. |
| Leased external-effect journal | `shipfactory/store.py:83-103`; `shipfactory/recipes/advancer.py:2422-2435` | Generic action-intent runner with probe-before-retry. |
| Resource leases | `shipfactory/store.py:104-106` | Worker slots, ports, or future executor resources. |
| Single daemon and per-board fault isolation | `shipfactory/daemon.py:519-614` | Generic scheduler loop. |
| Human-only authenticated decisions | `shipfactory/decisions.py:184-280` | Generic human-box result, bound to the exact waiting box attempt. |
| Runtime-configured seats/executors | `shipfactory/config.py`; `shipfactory/seats_admin.py` | Resolution of `who` to an executor and model. |
| Project attachment and allowed recipes | `dashboard/plugin_api.py:1326-1416`; `shipfactory/store.py:375-388` | Project owns recipes and runs. Rename policy surfaces where necessary. |
| Run receipts, logs, telemetry | `shipfactory/store.py`; `shipfactory/telemetry.py` | Generic audit history. |
| Frozen run input | Current hash checks in `shipfactory/recipes/instantiate.py:209-269` | Store the exact recipe JSON and hash directly on each new run. |

“Keep” does not mean keep every current API or column. It means preserve the invariant while removing domain coupling.

## Simplify or rewrite

| Current area | Action | Replacement |
|---|---|---|
| `shipfactory/recipes/loader.py` | **Rewrite** | Validate only `name`, one `start`, boxes (`id`, `name`, `who`, `instructions`, optional `end: true`), and arrows (`from`, `result`, `to`). Validate reachability, valid references, explicit End, and no ordinary dead ends. |
| `shipfactory/recipes/instantiate.py` | **Rewrite** | Create a Run with the exact recipe snapshot and one token at `start`. Do not create a Kanban collector. |
| `shipfactory/recipes/advancer.py` | **Replace**, not trim | A small transition loop over generic box attempts, route deliveries, joins, human pauses, End, and the two three-strike brakes. |
| `shipfactory/spawn.py` completion protocol | **Simplify** | Replace `done|blocked` plus separate review verdict sentinels with one generic envelope: output/work, result label, and optional summary. Technical failure is absence of a valid envelope. |
| `shipfactory/decisions.py` | **Simplify** | Preserve actor, nonce, waiting-attempt, and replay protection; remove task-spec/plan/change-set/evidence-specific fields. A human selects one declared outgoing result. |
| `shipfactory/recipe_graph.py` | **Rewrite** | Return declared boxes and arrows directly. Delete primitive shapes and synthetic review-verdict routers (`shipfactory/recipe_graph.py:17-27`, `shipfactory/recipe_graph.py:133-225`). |
| `shipfactory/store.py` | **Add a small v1 state model, then retire old columns** | Recipe snapshot, run, box attempt, route delivery/token, human decision, action intent, executor run. Internal table names may remain technical; public API uses Run and Box. |
| `shipfactory/watchdog.py` | **Simplify** | Detect a box attempt exceeding its deadline or a run with no schedulable progress. Escalate after the generic threshold; do not interpret review findings. |
| `shipfactory/recipes/selector*.py` | **Replace** | The project's main orchestrator chooses among recipes attached to the project. ShipFactory validates the choice and starts the run; it does not maintain a second recipe-selection workflow graph. |
| `shipfactory/cli.py` | **Simplify** | Public nouns: Project, Recipe, Run, Box, Arrow. Remove instance, activation, collector, flight, primitive, and journey commands after compatibility retirement. |
| `dashboard/plugin_api.py` and `dashboard/dist/index.js` | **Rewrite projection** | `/projects`, attached recipes, `/runs`, direct graph, box attempts, human waits, and receipts. Current endpoints expose `flights`, `instances`, `steps`, `primitives`, budgets, and optional steps (`dashboard/plugin_api.py:1419-1475`, `dashboard/plugin_api.py:1499-1544`). |
| Published recipe version policy | **Simplify** | Recipe files are editable. New runs copy current bytes; running runs never reread the file. Permanent publication immutability is unnecessary once the run owns its snapshot. |

## Delete after legacy runs drain

### Domain semantics in the engine

1. **Primitive taxonomy and activation switch**
   - Delete `agent_task`, `review_gate`, `approval_gate`, `notify`, `wait_for_event`, and `verification` as engine concepts.
   - `who` selects an executor; instructions define the job; arrows define the flow.

2. **Review-verdict machinery**
   - Delete `shipfactory/recipes/primitives.py` after generic completion lands.
   - Delete `SHIPFACTORY_VERDICT`, finding schemas, legal rework targets, review-specific input rendering, and approval blockers.
   - A review box is ordinary work whose declared results might be `approved` and `changes-needed`.

3. **Typed artifact system**
   - Delete `shipfactory/artifact_contracts.py` and `shipfactory/artifacts.py` from the engine.
   - Delete artifact kinds such as exploration, task-spec, plan, change-set, evidence-bundle, and review-story.
   - Store each box's produced work opaquely. Domain-specific structured files may still be requested in that box's instructions.

4. **Built-in software verification semantics**
   - Delete `shipfactory/verification.py`, `shipfactory/pytest_evidence.py`, and `shipfactory/pytest_runner.py` as engine-owned workflow policy.
   - Preserve any generally useful subprocess containment by extracting it into an executor adapter.
   - A deterministic test runner can be a `who`, but GraphRunner must not understand pytest counts, evidence bundles, browser surfaces, Git trees, HAR redaction, or migration directions.

5. **Verification-only environment sessions**
   - Delete `shipfactory/environments.py` from GraphRunner.
   - If recipes need an app server, a dedicated executor owns setup, health, cleanup, and its returned work. Generic process/port leases remain infrastructure.

6. **Legacy execution policy**
   - Delete `shipfactory/policy.py`, `shipfactory/hierarchy.py`, and the `shipfactory_verdict` plugin tool after non-recipe tasks migrate.
   - The current policy is a second hard-coded review → approval → land workflow (`shipfactory/policy.py:65-93`) beside recipes.

7. **Collectors and containment overlay**
   - Delete Kanban collector/root-collector tasks and parent-task containment. `instantiate.py` currently creates an inert “Journey” collector (`shipfactory/recipes/instantiate.py:120-159`).
   - A Run is the durable parent; boxes are displayed from run state.

8. **Domain-specific recovery events**
   - Delete verification retry modes, malformed-verdict releases, review admission repairs, rework-cone invalidation, and artifact-staleness recovery.
   - The current event consumer contains dedicated verification and reviewer recovery protocols (`shipfactory/recipes/advancer.py:2499-2705`). Replace them with generic retry, rework-count, human-result, cancel, and escalation events.

9. **Legacy recipe fields**
   - Delete `schema`, numeric publication `version`, `status`, `description`, `intent_tags`, `supersedes`, `parameters`, `budgets`, `steps`, `primitive`, `title`, `needs`, `optional`, `inputs`, `outputs`, and primitive-specific `params` from new recipes.
   - Preserve old YAML files byte-for-byte only for legacy-run replay and historical inspection.

10. **Legacy domain tables after archival**
    - Retire `artifacts`, artifact relations, evidence bundles/items/cases, verification actions, environment sessions, gate-specific bindings, triage selections, budget remnants, and old recipe-step semantic columns.
    - Do not destructively migrate the live database while any legacy run references them.

## Minimum GraphRunner

The replacement needs only the following durable concepts.

### 1. Recipe snapshot

```text
name, start, exact boxes, exact arrows, hash
```

A recipe can change on disk; a Run never rereads it.

### 2. Run

```text
id, project, recipe snapshot, request, status, created/updated timestamps
```

Valid terminal states are completed, escalated, cancelled, and failed infrastructure. “Blocked” is a box-attempt condition, not another workflow language.

### 3. Box attempt

```text
run, box, attempt number, input work references, executor run,
state, output work, result label, technical-failure count
```

A valid completion atomically records output and one declared result. A crash, timeout, or malformed/missing completion envelope increments the technical-failure count and retries the same box. The third failure escalates.

### 4. Route delivery

```text
source attempt, result, arrow, destination box, branch generation, state
```

Following one result may create several deliveries. A join consumes one delivery from every active incoming branch in that split generation. Looping creates a new box attempt and increments the counter for that backward arrow. The third rejected correction round escalates with the accumulated work and feedback.

### 5. Human decision

```text
waiting box attempt, declared result, actor, channel, nonce, timestamp
```

Only a human actor may write it. It uses the same result-routing path as a model completion.

### 6. Infrastructure journals

Keep executor runs, action intents, resource leases, logs, and receipts. They support the runner but do not appear in recipe grammar.

## Old-to-new mapping

| Current concept | New concept |
|---|---|
| `recipe_instances` / flight / journey | Run |
| `recipe_steps` activation | Box attempt |
| `needs` dependency | Arrow delivery and join |
| agent task | Box with an executor in `who` |
| review gate | Ordinary Box with review instructions and review result labels |
| approval gate | Human Box |
| verification primitive | Box handled by a deterministic executor |
| notify primitive | Box handled by a notification executor |
| wait-for-event primitive | Box handled by an event executor or Human Box |
| collector task | Delete; Run is the parent |
| verdict target | Arrow destination |
| rework cone | Backward arrow plus a new attempt |
| artifact/evidence input binding | Preceding box work delivered by immutable reference |
| activation budgets | Two generic three-strike counters |
| recipe version publication | Editable recipe plus frozen Run snapshot |

## Migration sequence

### Phase 0 — freeze the boundary

- Treat `docs/recipe-structure-v1.md` as the only new public recipe contract.
- No new features in the legacy semantic layer except production-critical fixes.
- Add a runtime-configured runner selector; default remains legacy until GraphRunner passes end-to-end tests.

### Phase 1 — build GraphRunner beside LegacyRunner

- Add the v1 loader and pure graph validator.
- Add v1 run/attempt/delivery persistence without altering existing tables.
- Reuse executor adapters, worker supervision, action intents, resource leases, project IDs, and telemetry.
- Implement sequence, result branch, split, active-path join, backward loop, human pause, explicit End, technical-failure escalation, and rework escalation.

### Phase 2 — prove the ratified recipe

- Execute the exact recipe in `docs/recipe-structure-v1.md` through GraphRunner.
- Prove each row in its completeness table through behavior, not function-name tests (`docs/recipe-structure-v1.md:144-158`).
- Verify restart recovery at every transition and duplicate-event idempotency.

### Phase 3 — expose the simple surface

- Add Project → Recipe → Run APIs and dashboard views.
- Display declared boxes and arrows directly; overlay attempts and human waits without synthetic nodes.
- Keep legacy views read-only for legacy runs.

### Phase 4 — migrate recipes, not active runs

- Translate each useful current recipe into v1 boxes/arrows.
- Move artifact, verification, notification, and environment requirements into box instructions and executor adapters.
- Start all new work on GraphRunner after one real project journey succeeds end to end.
- Existing legacy runs continue against their pinned old recipes and old engine.

### Phase 5 — delete the old semantic layer

Deletion gate:

- no active legacy runs;
- no project defaults pointing to legacy recipes;
- a backup and read-only archive of the legacy database exists;
- GraphRunner has completed sequence, split/join, rework, human approval, restart, retry, and escalation journeys;
- dashboard and CLI no longer call legacy endpoints.

Then remove the files, tables, routes, config, and tests listed in “Delete after legacy runs drain.”

## What not to do

- Do not make the new loader understand plans, reviews, artifacts, Git, tests, evidence, or notifications.
- Do not translate every old state and exception into GraphRunner.
- Do not mutate active runs onto new recipe bytes.
- Do not retain collectors merely to keep Kanban-shaped UI.
- Do not put branch logic inside box instructions when an arrow can state it.
- Do not big-bang replace the live engine before one real v1 run completes.

## Complete module disposition

This closes the inventory so no current production module is left implicitly unclassified.

| Module | Disposition |
|---|---|
| `shipfactory/__init__.py` | **Simplify:** register the CLI and generic run tools; remove the legacy verdict tool and policy completion hook. |
| `shipfactory/artifact_contracts.py` | **Delete after drain.** |
| `shipfactory/artifacts.py` | **Delete after drain.** |
| `shipfactory/cli.py` | **Rewrite public surface** around Project, Recipe, Run, Box, and Arrow; retain temporary read-only legacy commands. |
| `shipfactory/config.py` | **Keep and simplify:** runtime executor, runner-selection, project, retry, and timeout configuration. Remove domain profile schemas. |
| `shipfactory/daemon.py` | **Keep substrate; simplify tick:** singleton, board isolation, dispatch, reap, GraphRunner reconcile, generic watchdog. |
| `shipfactory/decisions.py` | **Keep boundary; rewrite payload** as a generic human-box result. |
| `shipfactory/environments.py` | **Delete from core after drain;** extract only generally useful executor helpers. |
| `shipfactory/executors/*` | **Keep:** adapter boundary for `who`; change completion return to the generic envelope. |
| `shipfactory/github_sync.py` | **Keep as an optional integration:** it may submit work or mirror state but must not define recipe flow. |
| `shipfactory/hierarchy.py` | **Delete with legacy policy.** Human-box authorization comes from project/operator identity. |
| `shipfactory/policy.py` | **Delete after non-recipe policy users migrate.** |
| `shipfactory/pytest_evidence.py` | **Delete from core.** |
| `shipfactory/pytest_runner.py` | **Delete from core.** A test executor may own an equivalent private helper. |
| `shipfactory/recipe_graph.py` | **Rewrite** as a direct recipe projection plus generic run overlay. |
| `shipfactory/recipes/__init__.py` | **Keep only as package seam** or remove if GraphRunner moves to a clearer module name. |
| `shipfactory/recipes/advancer.py` | **Replace with GraphRunner; delete after drain.** |
| `shipfactory/recipes/instantiate.py` | **Replace with generic Run creation; delete legacy implementation after drain.** |
| `shipfactory/recipes/loader.py` | **Replace with the v1 box-and-arrow validator; retain a private legacy loader while draining.** |
| `shipfactory/recipes/primitives.py` | **Delete after drain.** |
| `shipfactory/recipes/selector.py` | **Replace** with project-orchestrator recipe choice validation. |
| `shipfactory/recipes/selector_stage.py` | **Delete current selector workflow** after its input path targets GraphRunner directly. |
| `shipfactory/seats_admin.py` | **Keep:** manages `who` resolution. Remove role fields used only by deleted policy. |
| `shipfactory/spawn.py` | **Keep supervision; simplify recipe coupling and completion protocol.** |
| `shipfactory/store.py` | **Keep database ownership; add compact v1 tables, archive then retire legacy schema.** |
| `shipfactory/telemetry.py` | **Keep:** emit generic run, attempt, retry, escalation, and executor usage events. |
| `shipfactory/verification.py` | **Delete from core after drain;** extract only reusable executor/process containment. |
| `shipfactory/watchdog.py` | **Rewrite** as generic deadline and no-progress detection. |
| `dashboard/plugin_api.py` | **Rewrite** to new nouns and direct graph/run state; legacy endpoints become read-only during drain. |
| `dashboard/dist/index.js` | **Rewrite** around Project → Recipe → Run → boxes/arrows. |
| `dashboard/conformance-harness.js` | **Rewrite tests** against the v1 API and operator approval boundary. |
| `recipes/*.yaml` | **Preserve old bytes for replay; translate useful flows into separate v1 recipes. Never edit published legacy files.** |
| `tests/` | **Retain infrastructure invariants; replace domain-contract tests as each legacy subsystem retires.** |

## Final classification

```text
KEEP       the safety substrate
REWRITE    the graph runner and public projection
DELETE     the software-development ontology from the engine
```

The shortest safe path is a side-by-side generic runner, not another refactor of the 2,854-line domain advancer.
