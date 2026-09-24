# ShipFactory: lessons worth keeping

Archived 2026-09-24. ShipFactory was a governed software factory built as a Hermes
plugin from July to August 2026. This page keeps the parts worth reusing.

## Why it was archived

- The workflow it was meant to automate now runs without it. Hermes leads, builders
  run through `delegate_task` with one worktree per packet, reviewers from a
  different model family run as separate processes, and state lives in Linear
  (Weave Cloud, September 2026).
- Stock Hermes now includes `kanban_swarm` (plan → parallel workers → verifier →
  synthesizer), `goals` (keeps working until a goal is met), `kanban_decompose`
  and `background_review`. Reviving ShipFactory would mean running a second
  scheduler beside Hermes.
- The code is 25k lines, and GraphRunner v1 is only about 2.6k of them. The rest
  is the legacy recipe engine. It depends on `kanban_db.create_blocked_task` and
  `cancel_subtree`, which Hermes 0.18.2 doesn't have. That caused 150 of the 159
  test failures on 2026-09-24. All 338 GraphRunner tests still passed.
- Its seat roster names models that are no longer in use.

## The rules worth keeping

These rules come from `docs/DECISIONS.md`. Each is independent of this codebase.

1. **A box is work and an arrow is flow (D-004).** A box has a name, an owner and
   instructions. Arrows carry all routing: a backward arrow is rework, and
   several arrows out of one box are a split.
2. **A box returns its work plus one result label (D-005).** The label picks the
   arrow. Routes are never inferred by parsing prose.
3. **Model boxes run and human boxes wait (D-007).** Only the human operator can
   decide a human box. No model, automation, or recovery path can approve on the
   operator's behalf, and a timeout never counts as approval.
4. **Three technical failures stop the path and escalate to the orchestrator
   (D-010).** The report includes all three attempts and their errors. The
   engine never invents a result.
5. **Three rejected rework rounds stop and escalate (D-011).** Technical failures
   and rejected work are counted separately. No review loop runs forever.
6. **You edit the recipe, and each run keeps a frozen copy (D-012).** A run
   stores the exact structure it started with, so editing the recipe changes
   only later runs.

## Operational lessons

- **A reviewer on a model with a 5-hour rolling quota can escalate a whole run.**
  Two runs on 2026-08-04 failed every review attempt with 429 errors. Put
  reviewers on models without short rolling quotas.
- **Executors need their CLI binaries on the daemon's launchd PATH.** One run
  failed three times with `No such file or directory: 'codex'`.
- **A long-running daemon can look alive after its venv and worktree have been
  deleted.** Judge its health from the error log, not from the PID.

## When to revive

Revive only the GraphRunner slice (`graph_recipe.py`, `graph_runner.py`,
`graph_runtime.py`, `recipe_graph.py` and the graph dashboard views), and only if
one of these happens:

- the operator wants a visual approval dashboard for fleet work instead of chat;
- someone who doesn't code needs to run recipes without the lead; or
- `kanban_swarm` can't express rework loops or human approval steps for a real
  packet.

## Where things are

- Code: `main` (`d04870a`) plus branch `wip/designer-desktop-plugin-20260808`
  (the untested visual designer and Hermes Desktop plugin).
- Retired control plane:
  `~/Library/Application Support/StraitsLab/ShipFactoryControl/retired-20260924/`
  (launchd plist, config backup, cron backup, desktop plugin, program state).
- Archived skills: `~/.hermes/skills/.archive/shipfactory-*`.
