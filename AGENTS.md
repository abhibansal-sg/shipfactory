# ShipFactory — Development Guide

Instructions for AI agents and developers working on the ShipFactory codebase.

ShipFactory is a governed software factory built on Hermes kanban: recipes
(pipelines of agent tasks + review/approval gates), seats (worker roles),
watchdogs, budgets, and cost telemetry. Specs go in; reviewed, approved,
shipped code comes out. **Nothing ships without the operator's signal.**

## The One Law

**Approval gates belong to the human operator. Agents never press Approve —
not via the dashboard, not via the API, not by completing the gate's kanban
task directly.** An agent that needs a gate cleared reports to the operator
and waits. Operator overrides of *non-approval* steps are permitted with a
logged reason (`recipe release` / audited direct SQL with a backup).

## Layout

```
shipfactory/          # the Python package (engine)
├── cli.py            # `hermes shipfactory <verb>` argparse tree + daemon entry
├── daemon.py         # tick loop: dispatch, spawn, reap, advance
├── spawn.py          # worker process spawning (worktree per task)
├── store.py          # SQLite state: $HERMES_HOME/shipfactory/shipfactory.db
├── recipes/          # engine: loader, advancer (reconcile/apply_events), primitives
├── policy.py         # review/approval policy evaluation
├── watchdog.py       # stall detection + recovery
├── seats_admin.py    # seat CRUD ($HERMES_HOME/shipfactory/seats.yaml)
└── telemetry.py      # cost/usage JSONL
recipes/              # recipe definitions (dev-pipeline@N.yaml, templates/)
dashboard/            # Hermes dashboard plugin (manifest.json, plugin_api.py, dist/)
tests/                # pytest suite — must stay green
docs/                 # spec, validation evidence, lane briefs
__init__.py           # Hermes plugin entry point (register())
```

Published recipe versions are **immutable** — `dev-pipeline@4` bytes are
sha-pinned in `tests/test_artifact_discipline.py`. To change a recipe,
publish `@N+1`; never edit a published version in place.

## Running

The package needs the Hermes repo on `PYTHONPATH` (it imports
`hermes_cli.kanban_db`) and file descriptors ≥ 4096:

```bash
cd /Volumes/MainData/Developer/products/shipfactory
ulimit -n 4096
export PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile
PY=/Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python

$PY -m shipfactory.cli daemon --board <board>     # the daemon
$PY -m pytest tests/ -q                           # the tests (all must pass)
```

State lives in `$HERMES_HOME/shipfactory/` (`shipfactory.db`, `seats.yaml`,
`runs/`, `telemetry.jsonl`). The kanban boards live in
`$HERMES_HOME/kanban/boards/<board>/kanban.db`.

## Engine invariants

- `advance_events` keys are **permanently spent** — never replay an applied
  event by re-inserting its key; enqueue a fresh event instead. Since A0
  (2026-07-15) events are additionally **leased**: `pending → leased →
  applied|discarded|failed` under `BEGIN IMMEDIATE`; an expired lease
  returns to `pending` without reinsertion; stale events are `discarded`
  with a reason, never silently indistinguishable from success.
- **External effects go through the `action_intents` journal** (A0):
  gate completions, collector completions, and notification sends execute
  OUTSIDE factory write transactions. A retry after a crash inserts a
  fresh `(logical_key, attempt+1)` intent and PROBES the target first —
  the kanban task may already be done, the message already sent. Never
  perform an external effect directly inside an event-apply transaction.
- **One daemon per machine** (A0): the daemon holds an exclusive flock on
  `$HERMES_HOME/shipfactory/daemon.lock`; a second daemon exits before
  opening any board. CLI and dashboard commands ENQUEUE only — approve/
  reject/release return a queued decision id and the daemon tick applies
  it. There is no synchronous apply path outside the daemon.
- The advancer is the **single writer** for recipe-step state. Human/API
  decisions are *queued* (`gate_decision`, `operator_release`) and applied
  on the daemon tick — never mutate `recipe_steps` outside it except as an
  audited operator override with a `.bak` of the DB first.
- Approval-gate kanban tasks are born `blocked/needs_input`; the approve
  path must `unblock_task` → `complete_task` and **check the return value**
  (finding #30 — a silently swallowed approve looked like a dead button).
- A review verdict may only target an **upstream** agent_task. A first-step
  review targeting itself is unroutable and will stall (finding #29).
- `_summary()` treats `superseded` rows as non-terminal; stale ones can pin
  an instance at `running` after its real steps finish.

## Operational pitfalls (learned the hard way)

- **Restart the daemon after any board heal / REINDEX / file swap.** Its
  long-lived SQLite connections carry stale WAL state and throw
  `disk I/O error` on spawn until restarted.
- **Never bind-mount `$HERMES_HOME` into Docker/VirtioFS** while boards are
  live — it corrupts SQLite WAL (finding #28, "ghost tasks").
- `store.py` connections must close deterministically (`_ClosingConnection`,
  finding #27) — a leaked fd per call ends in EMFILE mid-run.
- Task specs must reference **real symbols** — workers correctly refuse to
  invent code for functions that don't exist, and the rework loop will spin
  until the spec is fixed.
- Failure limit: after 2 consecutive non-success attempts the dispatcher
  auto-blocks the task. Reset `consecutive_failures` when re-readying.
- `--require-recipes` startup aborts (unreadable config, recipes disabled,
  startup-guard failure) now persist a `daemon_require_recipes_fail_closed`
  telemetry record before raising (finding #31, adversarial lane review
  §2.0.6/#10) — a fail-closed abort must leave a trace, not just a nonzero
  exit code that vanishes with the process.

## Conventions

- Git author: `Abhinav Bansal <abhibansal-sg@users.noreply.github.com>`.
  No AI co-author trailers. Public repo — no secrets, tokens, or private
  paths in commits; screenshots/evidence must be scrubbed before adding.
- Findings get numbers (#22–#31 so far). When you fix one: commit message
  cites it, and the lesson lands in this file **in the same run**.
- All tests green before claiming done. `python -m pytest tests/ -q`.
