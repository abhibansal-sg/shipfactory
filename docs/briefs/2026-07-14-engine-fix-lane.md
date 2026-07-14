# LANE BRIEF — Engine-fix consolidation (shakedown findings punch list)

You are a non-interactive build lane. You run headless — NEVER stop to ask
permission; install what you need and note it in your report. Work ONLY in
your worktree: `/tmp/lane-engine-fix` (branch `lane/engine-fix` off main).

## Context
Repo: hermes-factory — a Hermes plugin. Recipe engine spec:
`docs/factory-spec.md` §17 (READ §17 and §15 FROZEN INTERFACE CONTRACT
before editing any module). Three live shakedowns produced a findings punch
list; the safety systems all worked, every bug lived in the seams. Full
transcripts: `docs/EXECUTION-CONTEXT.md` and the spec.

Test runner (bare python3 has no pytest):
```
bash -c 'ulimit -n 4096; /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
```
Baseline on main: 90 tests, all green. It must stay green.

## Scope — EXACTLY these 8 items. Anything beyond E1–E8 is a VIOLATION: stop and report instead.

**E1 — Reconciliation liveness (findings #2/#14).** `daemon.tick()` →
`apply_events()` only reconciles instances as a side-effect of pending
queued events. A kanban task that completes with no enqueued event leaves
its step frozen at running/pending forever; a step blocked by pre-daemon
spawn failures stays `worker_blocked` even after its task finishes (the
advancer observation loop at advancer.py ~:280 only watches
running/waiting). Fix as ONE rule: every tick reconciles ALL active
instances (status running/waiting_gate/cancelling), and reconciliation
re-observes blocked steps whose kanban task has since reached a terminal
state. Keep it idempotent (advance-event keys already dedupe).

**E2 — Fresh-activation healing for ghosts, without poisoned params
(findings #5/#6/#8).** Cross-DB write race can record a `kanban_task_id`
in factory.db whose kanban row never committed (ghost). Resetting the step
state silently no-ops (advance-keys consumed). Engine-legal repair is the
cone-invalidation pattern (advancer.py:253): fresh recipe_steps row,
activation+1, state pending. Implement as a reconciliation rule: step's
kanban task MISSING (sweep the board it belongs to) or
terminal-state-mismatched ⇒ open a fresh activation. The fresh activation
MUST NOT copy `workspace_path` (or other per-task workspace fields) from
the failed/ghost activation — let create_task inherit from the board's
default_workdir (board= is already passed explicitly since b96dfab).

**E3 — Approval-gate evidence bundle (finding #15).** The gate's
continue-here resume note is written at gate CREATION and never refreshed,
so operators see "No completed upstream summary was available" while the
evidence sits in task_runs. Fix: at PARK time (when the gate step enters
waiting), refresh the resume note / gate task body from upstream step
outputs: build summary + commit hash if present, each upstream verdict text
(task_runs.summary / metadata), test counts if parseable, tokens_charged vs
budget, and the step chain so far. This is the operator's case file — make
it render usefully in the dashboard Waiting card (it reads the task body +
comments).

**E4 — `recipe cancel` atomicity (finding #3).** Cancel sets
`status='cancelling'` BEFORE task-id validation can refuse — a refused
cancel strands the instance half-cancelled. Reorder: validate everything
that can refuse FIRST, then write the cancelling fence, in one txn.

**E5 — Templated-param validation deferred to bind time (finding #7).**
`load_library(seats=...)` rejects recipes whose seat field is a `${...}`
template ("unknown seat '${assignee_seat}'") because seat validation runs
before parameter binding. Fix: the validator must skip/defer any field
whose value contains `${...}` to bind time, where it IS validated.

**E6 — WAL-checkpoint health check (finding #10).** Board SQLite corrupted
live under worker kills + cross-DB writes on an external volume. Add a
best-effort periodic health pass in the daemon tick: `PRAGMA
wal_checkpoint(TRUNCATE)` (or PASSIVE if TRUNCATE contends) + `PRAGMA
quick_check` at a low cadence (e.g. every N ticks / minutes, constant in
config). On failure: log loudly + emit a telemetry event; NEVER crash the
tick (best-effort seam law).

**E7 — Executor-discipline template fix (finding #13).** The vendored GSD
discipline made a build worker BLOCK its own completed task
("review-required") instead of completing — review is the NEXT STEP in our
pipeline. In the executor/discipline template under `recipes/templates/`,
add explicitly: "Completing your kanban task IS the handoff to review — do
NOT block your task for review yourself." Respect recipe immutability: if
this text is baked into a pinned recipe version's step instructions, cut a
new recipe version (dev-pipeline@4) rather than mutating @3; if it lives in
a shared template file resolved at spawn time, edit the template and say so.

**E8 — Multi-board instantiation regression test (finding #11).** The
89-test suite stayed green all night while activate() stamped a FOREIGN
board's default_workdir into every task, because tests run single-board.
Add a test: two boards with DIFFERENT default_workdirs, global current
board = board A, instantiate a recipe on board B ⇒ created step tasks must
carry board B's workspace anchoring, never A's.

## Requirements
- Every fix gets an adversarial test reproducing the original finding
  (fail before / pass after). Name tests after findings (e.g.
  `test_finding_14_tick_reconciles_without_events`).
- Do not change §15 module signatures without updating spec §15 in the
  same commit. Additive params with defaults are fine.
- Full suite green ×2 consecutive runs at the end; print counts.
- Commit in logical units on `lane/engine-fix`, messages naming findings.

## Honesty clause
Print any clause you could not satisfy literally — say so plainly, do NOT
improvise around it. `DONE_WITH_CONCERNS` + enumerated deviations is a good
outcome. If a pre-existing test is broken on main, report it, never
silently fix or hide it.

## Final line of your output MUST be exactly one of:
`LANE_RESULT: done <one-line summary>`
`LANE_RESULT: blocked <one-line reason>`
