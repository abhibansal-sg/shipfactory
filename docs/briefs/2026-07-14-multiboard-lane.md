# LANE BRIEF — Multi-board daemon (one daemon, N boards)

You are a non-interactive build lane. You run headless — NEVER stop to ask
permission. Work ONLY in your worktree: `/tmp/lane-multiboard` (branch
`lane/multiboard` off main). NOTE: this lane dispatches AFTER the
engine-fix lane merges — your base includes the reconciliation-liveness
rework; read `daemon.py` and `factory/recipes/advancer.py` fresh, do not
assume pre-merge line numbers.

## Context
Repo: hermes-factory. Today `python -m shipfactory.cli daemon --board <one>`
serves exactly one board; the API layer is already multi-board (each
recipe instance row carries its own `board`). The dashboard header chip
reads the daemon run-row. Finding #20 (cosmetic, fold in): the chip can
show the GLOBAL current board while the daemon serves a different one.

Spec: `docs/factory-spec.md` §15 (frozen interfaces) + §17 (recipe engine).
Test runner:
```
bash -c 'ulimit -n 4096; /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
```

## Scope — EXACTLY these 4 items. Anything beyond M1–M4 is a VIOLATION: stop and report.

**M1 — `--boards a,b,c` (and repeatable `--board`) on the daemon verb.**
One daemon process, one tick loop, iterating boards. Backward compatible:
single `--board x` behaves exactly as today. Each board keeps its OWN
kanban connection (per-board kanban.db files); factory.db stays the single
shared store. Per-board failure isolation: one board's tick error (locked
db, corruption) logs + telemetry-events and moves on — it must NEVER kill
the loop or starve the other boards (best-effort seam law).

**M2 — Daemon run-row schema.** The liveness row
(`runs` table, task_id='__shipfactory_daemon__') currently records one board.
Record `boards: [..]` + per-board `last_tick_at`. Keep the old `board` key
populated (first board) for one release so existing readers don't break;
note the deprecation in the row.

**M3 — Status chip truthfulness (finding #20).** GET `/status` +
the header chip must report the boards the DAEMON is actually serving
(from the run-row), never `get_current_board()`. Multi-board display:
chip shows count + tooltip/expansion lists each board with its
last-tick age; a board whose tick is stale (> 3× tick interval) renders
in the warning state.

**M4 — Tests.** (a) two boards, instances on both, one tick advances
both; (b) board A's connection poisoned (e.g. mock raising
OperationalError) while board B still advances and A's failure is
telemetry-logged; (c) run-row carries both boards' tick times;
(d) single-board invocation unchanged (regression). Full suite green ×2;
print counts.

## Constraints
- Kanban stays the ONLY scheduler; you are multiplexing observation, not
  inventing a scheduler.
- Do not change §15 signatures without updating spec §15 in the same
  commit; additive params with defaults preferred.
- Dispatch/claim paths must remain per-board correct: every
  create_task/claim call already carries explicit board= (finding #11 law)
  — verify, don't assume, and add board= where a call relies on the global
  current board.
- UI work follows the host-utilities conformance law (no hardcoded hex,
  no literal font names, shared request() helper).

## Honesty clause
Print any clause you could not satisfy literally — say so plainly, do NOT
improvise around it. `DONE_WITH_CONCERNS` + enumerated deviations is a
good outcome.

## Final line of your output MUST be exactly one of:
`LANE_RESULT: done <one-line summary>`
`LANE_RESULT: blocked <one-line reason>`
