# Projects & Visual Recipes Atomic High Sprint Plan

Status: implementation plan for the frozen contract in
`docs/specs/2026-07-27-projects-visual-recipes-spec.md`. This is a worker DAG,
not an execution log. No worker commits; the orchestrator owns every commit,
merge, rollback, runtime action, and final full-suite gate.

## Guardrails and working method

- Only the files named in the frozen contract may change. Never edit published
  recipe YAML, runtime state, Linear, Git metadata, or Hermes core in this
  sprint unless a later source-grounded blocker is explicitly approved.
- Every implementation item is TDD: a worker may create a local red test, but
  its branch must finish with the focused tests green before integration. A red
  test that needs a shared-file owner remains local and unintegrated until that
  owner implements it; no orchestrator checkpoint lands red.
- Workers do not commit because the sandbox cannot reliably write shared Git
  metadata. After each green cluster the orchestrator reviews the diff and
  makes one focused commit containing only that cluster.
- No full suite is run by this lane. Wave 4's repeated full-suite command is a
  post-handoff orchestrator gate and remains pending until the implementation
  is integrated.
- Each worker lane must emit `LANE_RESULT: done|blocked` and make a first
  artifact—a test/report header naming the lane, files, and command—within 90
  seconds of start. Workers do not attempt commits. Focused commands use the
  repo-supported environment variables below; the orchestrator supplies their
  values without adding private absolute paths to these public docs:

  `PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_daemon_run_record.py -q`
  (substitute only another named focused test file from the lane's exact file
  list when required).

  A lane that cannot produce its first artifact or focused green proof emits
  `LANE_RESULT: blocked <reason>`.

## Wave 0 — contract, preflight, and checkpoint

Owner: orchestrator. No product code or tests are edited in this wave.
Exact files: `docs/specs/2026-07-27-projects-visual-recipes-spec.md` and
`docs/plans/2026-07-27-projects-visual-recipes-implementation.md` only. First
artifact within 90 seconds: a preflight report header with `git status` and
the two-file scope. Focused command:
`git diff --check -- docs/specs/2026-07-27-projects-visual-recipes-spec.md docs/plans/2026-07-27-projects-visual-recipes-implementation.md`.
No worker commit attempt. Emit `LANE_RESULT: done` (or
`LANE_RESULT: blocked <reason>`).

| Step | Atomic action (2–5 minutes) | Proof / expected result |
|---|---|---|
| W0.1 | Read `AGENTS.md`, `docs/DECISIONS.md`, both frozen docs, and inspect the current worktree with `git status --short`. | Only the two lane docs are in scope; unrelated dirty work is preserved. |
| W0.2 | Run `git diff --check -- docs/specs/2026-07-27-projects-visual-recipes-spec.md docs/plans/2026-07-27-projects-visual-recipes-implementation.md`. | Exit 0 and no whitespace errors. |
| W0.3 | Review the exact public symbols: `dashboard/plugin_api.py`, `shipfactory/store.py`, `shipfactory/recipes/instantiate.py`, `shipfactory/daemon.py`, `dashboard/dist/index.js`, `dashboard/dist/style.css`, `dashboard/conformance-harness.js`. | No source/build command is assumed beyond symbols verified by reconnaissance. |
| W0.4 | Orchestrator stages only the two docs, reviews the diff, and creates the plan checkpoint commit. | Planned command: `git add docs/specs/2026-07-27-projects-visual-recipes-spec.md`; then `git add docs/plans/2026-07-27-projects-visual-recipes-implementation.md`; then `git commit -m "docs: freeze projects visual recipes contract"`. This lane does not execute it. |

Rollback checkpoint: before any implementation work, restore only the two docs
from the orchestrator's reviewed checkpoint if the contract changes. Do not
reset the worktree globally.

## Wave 1 — parallel green foundations with no shared-file collisions

These four lanes start together. Each worker owns exactly the files listed in
its lane, emits its first artifact within 90 seconds, runs the focused command,
ends with `LANE_RESULT: done|blocked`, and makes no commit attempt. A worker
reports `LANE_RESULT: blocked <reason>` if a shared-file dependency prevents a
green branch. The orchestrator integrates only green branches.

### W1-A — SF-21 test-only supervision determinism

Owner: Verification tests. Files: `tests/test_verification_adversarial.py`
and, only if needed, `tests/test_verification.py`. Do not edit
`shipfactory/verification.py` unless source inspection proves a pure injectable
test seam impossible; do not change production classification.

First artifact within 90 seconds: a report header or collected-test output
naming both regression oracles. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_verification.py tests/test_verification_adversarial.py -q -k 'unrelated_ambient_environ_gap or known_supervised_unreadable_child'`.

Monkeypatch/fence only the specific fake-pytest regression so an unrelated
ambient process-scope scan cannot preempt its `test_failed` oracle. Add or
preserve a separate real production-path regression where an unreadable
process-scope scan remains `test_infrastructure_error` for a known supervised
child. This is TEST-ONLY isolation, not a fail-open supervision change. End
with focused green tests, no commit attempt, and
`LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W1-B — SF-21 daemon bookkeeping

Owner: Projects. Files: `shipfactory/store.py`, `shipfactory/daemon.py`, and
`tests/test_daemon_run_record.py`. This is the sole Wave 1 owner of
`shipfactory/store.py`; project policy persistence follows this lane in W2-A.

First artifact within 90 seconds: a report header or collected-test output
naming stale closure, token mismatch, live identity, and clean stop. Focused
command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_daemon_run_record.py -q`.

Add the stale/missing row, PID start-token mismatch, live matching identity,
lock ordering, and clean-stop proof. Reconcile only after `daemon_lock()` and
before board access; reuse existing start-token/crash/start/end seams. End
with focused green tests, no commit attempt, and `LANE_RESULT: done` (or
`LANE_RESULT: blocked <reason>`).

### W1-C — pure recipe graph projection

Owner: Graph. Files: new `shipfactory/recipe_graph.py` and
`tests/test_graph_projection.py`. Do not edit `dashboard/plugin_api.py` in
Wave 1. This branch implements the pure helper and must finish green.

First artifact within 90 seconds: a report header or collected-test output
naming the projection schema and synthetic recipe. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_graph_projection.py -q`.

Implement `project_graph()` for exact source/hash metadata, stable nodes and
coalesced edges, typed fan-in, review-verdict diamonds, legal rework targets,
and visible unsupported states. End with focused green tests, no commit
attempt, and `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W1-D — CSS and deterministic conformance fixtures

Owner: CSS/conformance. Files: `dashboard/dist/style.css`,
`dashboard/conformance-harness.js`, and new
`tests/test_dashboard_graph_contract.py`. Do not edit
`dashboard/dist/index.js`; do not assert UI that is not yet rendered.

First artifact within 90 seconds: a report header or collected-test output
naming the static selectors and fixture scenarios. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_graph_contract.py -q`.

Add deterministic static fixtures for the published recipes, synthetic
parallel/join graph, Unclassified, folded rework history, and required CSS
selectors. Static-contract tests may validate fixture schema, selector names,
stable attribute declarations, and safe-text source rules only; they must not
claim the future bundle renders them. End with focused green tests, no commit
attempt, and `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

Wave 1 integration order is any order among the four green branches, followed
by the orchestrator's focused green checkpoint. W1-B must integrate before
W2-A touches `store.py`; W1-C's pure helper is consumed later by the graph
routes. Every Wave 1 integration checkpoint is green.

## Wave 2 — backend launch and frozen SVG renderer

### W2-A — project policy persistence and migration

Owner: Policy, after W1-B's store commit. Files:
`shipfactory/store.py`, `tests/test_project_recipe_policy.py`.

First artifact within 90 seconds: a report header or collected-test output
naming migration 16 and the policy persistence command. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_project_recipe_policy.py -q`. No commit attempt.

1. Add focused schema/policy tests, then add migration 16 after migration 15,
   its checksum/name entries, `project_recipe_policies`, the three nullable
   `recipe_instances` columns, and the four exact indexes from the frozen
   contract. The launch index is unique on `(project_id,
   launch_idempotency_key)` only when both are non-null; the issue index remains
   globally unique for non-null `linear_issue_id`. Do not add a board mapping or
   duplicate the instance's existing execution-board identity.
2. Add only the helpers `load_project_recipe_policy`,
   `save_project_recipe_policy`, canonical JSON validation, and project rollup
   query seams. Policy writes must survive reload/restart. Run the focused
   command; expected green plus migration checksum idempotence. End with
   `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W2-B — Projects API, rollups, and idempotent launch

Owner: Projects API. Files: `dashboard/plugin_api.py` and new
`tests/test_projects_api.py`; it may call existing store/instantiate helpers
but must not edit `dashboard/dist/index.js` or Hermes source.

First artifact within 90 seconds: a report header or collected-test output
naming the policy and launch cases. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_projects_api.py -q`.

1. Add HTTP tests for `GET /projects`, the operator-bound
   `PUT /projects/{project_id}/recipe-policy` attach/detach/default flow,
   reload/restart persistence, project recipe filtering, unbound/ambiguous
   refusal, no board request field, `POST /projects/{id}/flights`, deterministic
   replay, issue uniqueness, hidden board resolution,
   `linear_backlink.status='unavailable'`, and Unclassified rollups.
2. Run the focused command above.
3. Implement exact Pydantic models/routes and adapter calls to
   `hermes_cli.projects_db.connect_closing()`, `get_project()`/`list_projects()`,
   `load_library()`, `bind_parameters()`, and existing `instantiate()`.
   Resolve only explicit `board_slug` internally and read the Hermes
   project-to-board binding live for each new launch. Existing instances retain
   their captured project/board identity. Use deterministic instance identity
   and probe-before-effect retry handling; the canonical fingerprint and
   deterministic id include project id; do not add an in-product Linear client.
4. Rerun the focused command and then the existing dashboard write-surface
   checks: `PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_plugin.py tests/test_dashboard_write_surface.py -q`.
   Expected green: old routes retain behavior and new responses match every
   frozen field. End with focused green tests, no commit attempt, and
   `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W2-C — graph projection backend

Owner: same backend owner as W2-B, after its API changes are green. Files:
`dashboard/plugin_api.py`, `tests/test_graph_projection.py`.

First artifact within 90 seconds: a report header or collected-test output
naming the graph routes and pure helper. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_graph_projection.py -q`. No commit attempt.

1. Consume the green `shipfactory.recipe_graph.project_graph()` helper from
   W1-C and add the two read-only graph routes. Load/validate exact library or
   pinned DB bytes;
   emit declared metadata, typed edges, synthetic verdict routers, and
   explicit unsupported states.
2. Run
   `PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_graph_projection.py -q`.
   Expected green: both published fixtures and synthetic layout payload pass;
   malformed/hash-drifted documents fail closed. End with focused green tests,
   no commit attempt, and `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W2-D — native SVG renderer

Owner: Dashboard bundle owner. Exclusive file: `dashboard/dist/index.js`.
No other worker edits this file in W2–W3.

First artifact within 90 seconds: a report header or collected-test output
naming the SVG contract. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_graph_contract.py -q`. No commit attempt.

1. Add bundle/conformance assertions for one rectangle, one diamond,
   review-verdict router, coalesced edges, keyboard focus, and stable
   attributes. Run the focused command above.
2. Implement pure local rank/lane layout and native SVG React elements in the
   existing IIFE. Use API payloads, never a client workflow definition. Keep
   renderer and inspector state read-only.
3. Rerun the same focused command. Expected green: serial, synthetic join,
   review loop, skipped, unsupported, and operator-only shapes render with no
   unsafe HTML. End with focused green tests, no commit attempt, and
   `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

Wave 2 checkpoint: orchestrator integrates W2-A, W2-B/W2-C, then W2-D in that
order; W1-D already owns the CSS foundation. Commit separately: `feat: add project recipe policy storage`,
`feat: add project flights and graph projections`, and
`feat: render frozen recipe graphs`. Roll back by reverting the latest focused
commit only; preserve migration 16 and old routes if the bundle fails.

## Wave 3 — live inspector, rendered Projects flow, and conformance evidence

The required work is parallel by file family except for the committed bundle.
One exclusive Dashboard bundle owner owns `dashboard/dist/index.js` and
implements the SVG renderer, live inspector, and Projects UI serially. No
other worker touches that file. This is the only serialization beyond the two
backend hotspots.

### W3-A — live overlay and inspector

Owner: Dashboard bundle owner, after W2-D. Exclusive file:
`dashboard/dist/index.js`; backend read fields are already supplied by W2-B/C.

First artifact within 90 seconds: a report header or collected-test output
naming the overlay cases. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_graph_contract.py -q`. No commit attempt.

1. Add red DOM assertions for current versus historical activation, exact
   `next_actor`, exact `blocker`, review verdict/rework edge, receipt expansion,
   evidence status, missing log/prompt, truncation, and operator-only approval.
2. Run the focused command above.
3. Add the read-only overlay join, inspector order, folded history, and links
   to `/instances/{id}/receipts` and exact run log/prompt routes. Poll and
   replace overlay data atomically. Do not write `recipe_steps` or infer a
   model when a run receipt is absent.
4. Rerun the focused command. Expected green: every node has one current
   state, history remains within that logical node, and untrusted content is
   React text only. End with focused green tests, no commit attempt, and
   `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W3-B — rendered Projects flow

Owner: the same Dashboard bundle owner, immediately after W3-A. Exclusive file:
`dashboard/dist/index.js`; no concurrent bundle edit.

First artifact within 90 seconds: a report header or collected-test output
naming the Projects and policy-control cases. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_graph_contract.py -q`. No commit attempt.

1. Add DOM assertions for `data-project-id`, recipe selection, parameter
   binding, `data-recipe-attach`, `data-recipe-detach`,
   `data-recipe-default`, `data-project-launch`, no board field, success toast,
   returned instance identity, and absent/disabled Unclassified launch.
2. Run the focused dashboard graph contract command above.
3. Add the Projects tab, project detail, real attach/detach/set-default
   controls wired to `PUT /projects/{project_id}/recipe-policy`, attached-recipe
   form, Start button, reload persistence check, and navigation to the same
   graph instance. Reuse existing `Button`, `Card`, `Badge`, fetch/poll
   patterns, and text-child safety. Do not duplicate recipe semantics in the
   bundle. This policy control is mandatory first-sprint acceptance, not an
   optional follow-up, and it does not introduce a new auth system.
4. Rerun the focused command and existing registration/safety tests:
   `PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_plugin.py tests/test_dashboard_graph_contract.py -q`.
   Expected green: rendered Projects flow and prior plugin registration both
   pass. End with focused green tests, no commit attempt, and
   `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W3-C — conformance and screenshot harness

Owners: Conformance and CSS, parallel with W3-A/B but touching distinct files.
Files: `dashboard/conformance-harness.js`,
`tests/test_dashboard_graph_contract.py`, `dashboard/dist/style.css`, and only
new approved files below `dashboard/conformance-evidence/`.

First artifact within 90 seconds: a report header or collected-test output
naming the DOM and screenshot scenarios. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_graph_contract.py tests/test_dashboard_plugin.py -q`. No commit attempt.

1. Turn all DOM assertions green against the real IIFE bundle. Assert every
   node/edge stable attribute, keyboard selection, accessible label, safe text,
   human gate distinction, review router distinction, and no `.innerHTML` or
   `dangerouslySetInnerHTML`.
2. Run the focused command above.
   Expected green with no full-suite invocation.
3. Start the documented harness with the unresolved host root substituted:
   `"$HERMES_MOBILE_PATH/node_modules/.bin/vite" --config dashboard/conformance-harness.vite.mjs --host 127.0.0.1 --port 4179`.
   Visit `dashboard/conformance-harness.html` and its query scenarios at a
   1440x1000 viewport. The repository proves the Vite server command but does
   not identify a screenshot automation binary; do not fabricate PNG proof.
4. If the orchestrator supplies an approved browser capture tool, capture the
   five frozen scenarios and write only redacted evidence under
   `dashboard/conformance-evidence/`. If not, record screenshot proof as
   blocked/skipped with the missing tool, while DOM/unit proof remains valid.
End with focused green tests, no worker commit attempt, and
`LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

Wave 3 checkpoint: integrate W3-A then W3-B serially; integrate W3-C after the
bundle is stable. Commit `feat: add graph live inspector`,
`feat: add projects launch flow`, and `test: add rendered graph conformance`.
Rollback is pairwise for `dashboard/dist/index.js` and `dashboard/dist/style.css`;
never leave a new bundle with old incompatible CSS.

## Wave 4 — post-handoff integration and operational proof

This wave is explicitly outside the present no-full-suite/no-runtime-mutation
planning lane. It is an orchestrator gate after all focused commits are
reviewed. It must not be claimed complete from unit tests alone.

### W4-A — combined focused integration

Owner: orchestrator. Files read: the focused test files listed below; no worker
files are changed by this lane. First artifact within 90 seconds: an
integration report header naming the commit set and first focused result.
Focused command form:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_project_recipe_policy.py -q`. Repeat the same command form for each named focused file. No worker commit attempt; the orchestrator may commit only after all focused tests are green.

1. Run the focused files individually, not as a full suite:
   `tests/test_project_recipe_policy.py`, `tests/test_projects_api.py`,
   `tests/test_graph_projection.py`, `tests/test_dashboard_graph_contract.py`,
   `tests/test_dashboard_plugin.py`, `tests/test_dashboard_write_surface.py`,
   `tests/test_daemon_run_record.py`,
   `tests/test_verification.py`, and
   `tests/test_verification_adversarial.py`.
2. Use the exact focused command form above for each named file.
   Expected: every focused file green and no fixture/private-path leakage.
3. Orchestrator reviews `git diff --check`, public path references, migration
   checksum, API schema, and file ownership; then commits
   `test: integrate projects visual recipes evidence`. Emit
   `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W4-B — known-flake repetition and full suite

Owner: orchestrator. Files read: `tests/` and the two verification test files;
no production verification file is changed by this lane. First artifact within
90 seconds: a repetition report header naming the environment and oracle
classification. Focused command before the suite:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_verification.py tests/test_verification_adversarial.py -q`. No worker commit attempt.

The orchestrator, not this lane, runs the full suite enough to expose the known
load-sensitive supervision flake. Run the exact repository command repeatedly
with the approved environment:

`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/ -q`

Repeat until the agreed repetition count is recorded by the orchestrator. A
failure must distinguish the deterministic unrelated-ambient regression from a
known-child infrastructure error; no test is weakened to make the suite green
and production unreadable-scan classification remains infrastructure error.
Emit `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W4-C — real dashboard button and graph smoke

Owner: orchestrator. Files read: `dashboard/conformance-harness.html`,
`dashboard/conformance-harness.js`, `dashboard/dist/index.js`, and
`dashboard/dist/style.css`; approved evidence may be written only under
`dashboard/conformance-evidence/`. First artifact within 90 seconds: a browser
smoke report header. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_dashboard_graph_contract.py tests/test_dashboard_plugin.py -q`. No worker commit attempt.

With the documented Vite harness and an approved screenshot/browser tool:

1. Load the Projects scenario, click the real Start button identified by
   `data-project-launch`, assert the response identity, and inspect the same
   instance graph.
2. Capture bound project, human approval, rework history, synthetic join, and
   Unclassified states. Store only redacted approved evidence.
3. Report browser/runtime proof separately from Python/API proof. If the
   browser runner or screenshot tool is unavailable, report `BLOCKED`/`SKIPPED`
   rather than treating DOM fixtures as a screenshot.
Emit `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W4-D — live daemon smoke

Owner: orchestrator. Files read: `tests/test_daemon_run_record.py` and the
daemon/store runtime seams; no source files are edited by this lane. First
artifact within 90 seconds: a daemon-smoke report header. Focused command:
`PYTHONPATH="$SHIPFACTORY_HERMES_PATH" HERMES_MOBILE_PATH="$HERMES_MOBILE_PATH" "$PY" -m pytest tests/test_daemon_run_record.py -q`. No worker commit attempt.

Use the existing focused empty-board smoke contract in
`tests/test_daemon_run_record.py` first. A live daemon run is a separate
operator-authorized action and is not performed by this planning lane. When
authorized, prove singleton lock acquisition, one empty tick, durable
`record_daemon_start`/`record_daemon_tick`/`record_daemon_end`, zero worker
rows, and no approval activity. Stale-row reconciliation must be observed
only after lock acquisition and must not adopt or close a provably live PID /
start-token identity.
Emit `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

### W4-E — Linear reconciliation

Owner: orchestrator after implementation and runtime proof. Files read:
focused reports, commit SHAs, and the target Linear issue; no in-product
backlink code is added. First artifact within 90 seconds: a reconciliation
report header naming the issue, evidence sources, and intended status/comment
update. Focused command: rerun the relevant focused test command above before
reconciliation. No worker commit attempt.

Read the launch response and Factory instance row for `linear_issue_id`, then
use the authorized Linear integration to update the issue status and comment
with the implementation commit(s), focused-test results, full-suite result,
and runtime/browser evidence or explicit blocked/skipped reasons. This is an
actual post-implementation orchestrator action, not a read-only check. The
in-product Linear backlink writer remains deferred; do not claim its
`linear_backlink.status="unavailable"` field is the closeout reconciliation.
Emit `LANE_RESULT: done` (or `LANE_RESULT: blocked <reason>`).

## Merge order, ownership, and rollback matrix

1. Docs checkpoint (W0).
2. W1-A verification tests and W1-B daemon tests/code in parallel;
   orchestrator commits separately.
3. W1-C pure graph helper and W1-D CSS/fixture foundations; orchestrator
   integrates only their green branches.
4. W2-A store migration/policy; then W2-B project API and W2-C graph API in
   one backend ownership chain; then the exclusive bundle owner implements W2-D
   SVG and later W3-A/W3-B serially.
5. W3-A overlay, W3-B Projects UI, then W3-C conformance/evidence.
6. W4 focused integration, post-handoff full suite, browser proof, daemon
   smoke, and actual orchestrator Linear status/comment reconciliation.

Shared-file reasons for serialization:

- `shipfactory/store.py`: W1-B must finish daemon identity work before W2-A
  adds migration 16/checksum logic; two writers would invalidate migration
  ordering and make red tests ambiguous.
- `dashboard/plugin_api.py`: W2-B owns project routes and W2-C adds graph
  routes after that contract; concurrent route edits would collide in one
  router module.
- `dashboard/dist/index.js`: W2-D, W3-A, and W3-B are one sequential bundle
  owner; renderer/inspector/Projects UI cannot be safely cherry-picked into a
  committed IIFE in parallel.
- `dashboard/dist/style.css`: one CSS owner prevents selector and reduced-motion
  regressions while the bundle changes.

Rollback checkpoints:

- C0: docs only; stop before implementation if the frozen contract changes.
- C1: focused SF-21, daemon, graph-helper, and static-contract green tests;
  revert only the latest worker commit.
- C2: migration 16/backend; disable new routes and preserve additive columns if
  API validation fails; never downgrade or delete data.
- C3: bundle/CSS; restore both prior artifacts together if conformance fails.
- C4: evidence/integration; retain focused test evidence, mark unavailable
  browser/live/Linear proof blocked, and do not claim ship readiness.

## Final handoff checklist

- [ ] Both frozen documents are committed by the orchestrator; no worker commit
  or Git metadata edit occurred in this lane.
- [ ] `shipfactory/store.py` migration 16 has the exact table, nullable columns,
  indexes, and checksum sequence; no project-board mapping exists.
- [ ] Hermes project identity/binding is read live, board is absent from public
  launch request/response, and existing instances retain their captured
  project/board identity.
- [ ] Attach/detach/default controls use the existing local operator boundary,
  persist through reload/restart, and have DOM/button/API proof.
- [ ] One non-null issue is globally one flight; launch keys are unique only
  per `(project_id, launch_idempotency_key)` for non-null project/key, and
  retries do not duplicate collector/instance state.
  retry does not duplicate collector/instance state.
- [ ] Graph source/hash, node/edge semantics, live actor/blocker/history,
  receipts, evidence, accessibility, and unsupported states match the frozen
  schema.
- [ ] T1 ambient scanner isolation remains fail-closed for known children.
- [ ] Unclassified is visible and unlaunchable; human approval remains
  operator-only; residual cleanup uses only audited eligibility.
- [ ] Rendered button and screenshot/browser proof are separately reported,
  with missing browser tooling marked blocked/skipped.
- [ ] Post-handoff full suite and live daemon smoke are evidenced or explicitly
  blocked/skipped; the orchestrator completes Linear status/comment
  reconciliation with commit/test/runtime evidence after implementation.
