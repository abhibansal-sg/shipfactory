<!-- Committed 2026-07-16. Wave 4 build lane. Source: review §2.1 (WS1), §5.1 order 4. -->

# LANE BRIEF — SF-8 environment sessions: pinned manifests + app-up

Fresh clone /tmp/sf-lane-env, branch `lane/environment-sessions`. Base
includes A0+A1+SF-5. Read files fresh.

Read in order: AGENTS.md; review §2.1.1 (runtime manifest + operator config —
NORMATIVE YAML), §2.1.4 (state machine), §2.1.5 (path/process safety),
§2.1.6 (budgets), §2.1.7 (tests the build lane will miss);
shipfactory/spawn.py (worker_slot leases), store.py (resource_leases),
daemon.py (tick structure), watchdog.py (subprocess timeout pattern).

Test command: bash -c 'ulimit -n 4096; PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
Baseline: 192 passed. Green ×2, paste both counts.

## Invariants — literal
- NEVER run bootstrap synchronously inside daemon.tick() or
  shipfactory_spawn() — tick is single-threaded; a slow bootstrap stops
  every board. Environment materialization runs as a SUPERVISED CHILD
  process tracked in the runs table (A1 identity pattern: pid + start
  token), reaped by the daemon like any worker.
- Runtime manifest (.shipfactory/runtime.yaml, schema shipfactory.runtime/v1)
  is read from the instance's TRUSTED BASE COMMIT, pinned by git blob SHA —
  never from the working tree a candidate may have modified. A candidate
  diff touching runtime.yaml / bootstrap / start / verification scripts is
  tagged control-plane risk; the modified script must not execute with
  autonomous privileges in the same cycle proposing it.
- Ports from operator config range (port_min/port_max) via SF-A1
  resource_leases (kind=port). Timeouts/size caps from operator config
  only (§2.1.1 operator block verbatim). No Hermes-core changes.
- dev-pipeline@1..@5 bytes unchanged (if @5 exists in your base; do not
  publish any recipe this lane — WS4 wires environments into a recipe).

## Scope — 4 deltas
1. **Manifest**: parse+validate shipfactory.runtime/v1 (§2.1.1 verbatim:
   bootstrap.argv/tracked_inputs/network, app.start_argv/healthcheck/
   stop_signal, seed.argv). Unknown keys rejected. Blob-SHA pinning from
   base commit.
2. **Materialization**: env_sessions table (numbered migration): manifest
   blob SHA + tracked-input hashes = materialization key; bootstrap as
   supervised child (bounded by bootstrap_timeout_seconds, output capped
   max_output_bytes, network per manifest policy); states materializing →
   ready → failed with persisted logs.
3. **App sessions**: app-up as supervised child on a leased port;
   healthcheck poll until expected_status or startup_timeout_seconds;
   stop via stop_signal then KILL after shutdown_timeout_seconds; leaked-
   child reaping on daemon restart (adopt-or-kill via run identity);
   session states starting → healthy → stopping → stopped | crashed.
4. **Staleness**: tracked-input hash change invalidates materialization
   (stale, rebuild next demand — mirror SF-5 artifact staleness).

## Required regressions (§2.1.7 + A1 patterns)
Port collision (second session waits, queue-not-fail); daemon restart with
live app session (adopted, healthcheck still enforced); daemon restart with
dead session (crashed + port lease released); stale PID never killed blind
(start-token check); bootstrap timeout → failed with log; healthcheck
never-healthy → failed, port released; tracked-input change → stale;
manifest-from-candidate-tree attack → trusted-base copy used, control-plane
tag on the diff. Suite ×2.

Commits: 'Abhinav Bansal <abhibansal-sg@users.noreply.github.com>', no AI
trailers/tracker IDs. Sandbox .git failure → finish + report. Do NOT push.
Final line: LANE_RESULT: done <summary> | blocked <reason>
