# PROGRAM BRIEF — Autonomy program [SUPERSEDED]

> **SUPERSEDED 2026-07-15** by the external program review at
> `docs/reviews/2026-07-15-external-program-review.md` (verdict: NO-GO as
> drafted; GO after control-plane hardening). §0 (the destination) remains
> the operator's ratified intent. The ratified build order is the review's
> §5 sequence: A0 → A1 → artifacts/recipe-v2 → WS2 serial planning →
> WS1 environments → WS4 verification → WS5 story/gate UI → WS6-A release →
> WS6-B deploy → WS3 fan-out (cap 2) → WS6-C intake (shadow) → Phase B.
> Known factual errors in this draft, caught by the review: the triage
> selector IS already wired into daemon.tick(); evidence fields in verdict
> JSON are illegal against parse_verdict(); backtick-grep grounding and
> same-instance fan-out are rejected designs.


Status: DRAFT for operator ratification. This is the program map, not a lane
brief — each workstream (WS) below becomes its own lane brief when dispatched,
with deltas capped by number per the standing scope-control law.

## 0. The destination (operator's stated end state)

The app builds itself, in a loop: it **finds** bugs and feature opportunities,
**specs** them, **plans**, **builds**, **reviews**, **verifies**, and **ships**
— including auto-merge and feature-flag flips — with no human in the loop.
The operator approval gate is a *phase*, not the philosophy: autonomy
graduates as machine-verified evidence becomes stronger than a human glance.

Reference model: a commercial cloud "autopilot for software engineering"
product (launched 2026-07-14; studied privately). Its six-bottleneck frame (machine, planning,
orchestration, testing, review, merge) is the audit lens for this program.
Where it runs on rented cloud machines + a service team, we run on this Mac + Hermes.

## 1. What we already have (do not rebuild)

| the reference product concept | ShipFactory equivalent — EXISTS |
|---|---|
| Harness with judgment baked in | Recipes + templates + AGENTS.md + findings ledger |
| Cross-lab adversarial review | verifier seat (sol/OpenAI) vs builder seat (sonnet/Anthropic), citation-gated verdicts |
| Model-per-job routing | seats.yaml (profile × model × reasoning × role) |
| Fresh context per unit of work | one worker process per step activation, by construction |
| Cost telemetry + enforcement | telemetry.jsonl + admission-debit budget fuse (BETTER: they only report) |
| Recurring intent (their roadmap) | Hermes cron + kanban triage (exists, partially wired) |
| Isolated parallel machines | worktree-per-task + multi-board daemon |

The program is therefore **six workstreams of genuinely new capability**, one
per the reference product bottleneck, ordered so each unlocks the next, ending at the
autonomous loop.

## 2. The workstreams

### WS1 — The machine: environment bootstrap + app-up (reference bottleneck 1)

Goal: a worker's worktree is a *running system*, not a code checkout.

Mechanics:
- New board metadata key `env_bootstrap` (path to a committed script, e.g.
  `scripts/env-bootstrap.sh`): venv path, PYTHONPATH, ulimit, service
  start/stop, seed data. `spawn.py` runs it after worktree creation, before
  the worker prompt; failures surface as `spawn_failed` with the script's
  stderr (NOT a silent fuse trip).
- New optional recipe param `app_up: true` on agent_task/review_gate steps:
  bootstrap must leave the app serving on a port recorded in the run row
  (`app_url`), so downstream steps (WS4 testing) can drive it.
- Snapshot analog: the bootstrap script IS the snapshot — deterministic,
  git-versioned (operator law: configs → git-versioned files, ONE fixed rule).

Kills: environment-as-prose in AGENTS.md; the class of finding #12-adjacent
"worker inherits wrong environment" failures.

### WS2 — Planning: explore → spec → adversarial plan (reference bottleneck 2)

Goal: the operator writes ONE line of intent; the machine writes the spec.
This is the biggest gap — today recipes validate a human-written spec
(plan-check judges), the reference product *generates* the plan (explore agents author).

Mechanics, three deltas:
- **D1 — symbol-grounding guard (smallest, do first):** at instantiation,
  grep the repo for every backtick-quoted symbol/path in `${request}`;
  refuse to instantiate on a miss with the miss list. Mechanically kills the
  finding #33 class (mis-specified symbol → infinite honest rework loop).
  Lives in `instantiate()` or a pre-step; NOT a new primitive.
- **D2 — `explore` step (new first step in dev-pipeline@5):** an agent_task
  whose contract is read-only reconnaissance: locate the real files/symbols,
  read direct callers, list constraints, and REWRITE `${request}` into a
  full task spec (target files, done criteria, test plan, risk notes).
  Output = spec artifact handed to plan via parent-handoff (the finding #26
  rework-context mechanism already carries verdicts; reuse it for specs).
- **D3 — adversarial plan loop:** plan-check upgrades from approve-or-park
  to draft-attack-revise: planner seat (strong model, lab A) drafts a
  plan-of-plans (ordered sub-tasks, each with test criteria); attacker seat
  (strong model, lab B) must find concrete holes with path:line citations
  or explicitly concede; max 2 rounds (budget fuse), then the surviving
  plan flows downstream. Both verdicts land in the evidence chain.
  Engine note: this is expressible today as two review_gate steps with the
  #31 guard (a review may only target an upstream agent_task — D2's explore
  step provides that target, resolving the plan-check@2 legality problem).

### WS3 — Orchestration: plan fan-out (reference bottleneck 3)

Goal: a plan-of-plans becomes N parallel build steps, not one monolith.

Mechanics:
- New primitive `fanout` (spec §17 addendum required): consumes the WS2 plan
  artifact, creates one agent_task per sub-plan with `needs` mirroring the
  plan's dependency graph, all within the SAME instance (same budget fuse,
  same collector). Cap: `max_fanout` (default 4) — this Mac is not EC2.
- Each sub-build gets its own worktree branch; a `reconcile-build` step
  (agent_task) merges sibling branches, resolves seams, runs the suite.
- Model-per-sub-task: sub-plans tagged `kind: ui|logic|test` route to seats
  by tag (seats.yaml gains an optional `handles: [tags]` column).
- Standing law → AGENTS.md: every major model release, re-audit seats.yaml
  vs profile configs (finding #12 corollary; no vendor does this for us).

Dependency: WS2 (needs a plan artifact to fan out). Highest engine risk of
the program — dispatch AFTER WS2 proves stable, as its own lane, adversarial
tests mandatory (the recipe-engine build lesson: build lane's own tests
never count).

### WS4 — Testing: watched verification + evidence (reference bottleneck 4, its crown jewel)

Goal: nothing reaches a gate (human today, machine later) without the app
having been RUN and WATCHED. "Video proof over text verdict."

Mechanics:
- verify step (dev-pipeline@5) gains a QA leg when the diff touches UI/API
  surfaces: bring the app up (WS1 `app_up`), drive the real flows with the
  gstack/browse daemon or Playwright, capture screenshots + a short screen
  recording (ffmpeg) into `runs/<task>/evidence/`; artifact paths land in
  the verdict JSON (`evidence: [paths]`).
- Gate card (dashboard `Waiting` view + plugin_api) renders the bundle:
  diff-as-story summary (most important change first — steal that framing),
  both verdict texts, test counts, screenshots/video inline, tokens_charged
  vs budget, commit hash. This closes the operator's twice-repeated
  approval-card feedback AND E3 from the engine-fix punch list — same work.
- Live preview analog: the WS1 `app_url` on the gate card is our
  "URL per branch" — clickable while the instance is parked.
- Mobile leg (LATER, flagged): Xcode simulator exists on this Mac; an iOS
  QA step is feasible but is its own lane; do not fold into the first cut.

This workstream is ALSO the autonomy bridge: the gate can only be removed
when its evidence is machine-checkable. Video + driven flows + suite green
is that evidence.

### WS5 — Review: diff-as-story (reference bottleneck 5)

Goal: the review a human (or judge model) consumes is a narrative, not a wall
of files.

Mechanics (small — mostly prompt + rendering):
- verifier template gains a `story` section in the verdict body: most
  important change first, why it's safe, what was NOT touched, tests/generated
  files folded to one line. Renders on the WS4 gate card.
- Phone surface: gate card content mirrors to the Telegram notify (the
  Finn-loop one-tap steal, backlog item 6) so approve/reject works away from
  the dashboard. Approve action still routes through the queued
  gate_decision path — single-writer law unchanged.

### WS6 — The merge + the loop closes (reference bottleneck 6 → the operator's end state)

Goal: from "operator pressed Approve" to "code is on main, worktree pruned,
next work self-generates" with zero human bookkeeping — then, phase by
phase, the Approve press itself becomes conditional.

Mechanics:
- **D1 — `merge` primitive (post-approval):** merge worktree → main, run the
  full suite on main, diff-check that BOTH source and tests came over (the
  hello_shakedown dropped-test pitfall, mechanized), prune the worktree,
  push. Failure ⇒ step blocks, instance parks — never force.
- **D2 — deploy hook:** optional `on_merged` script per board (git-versioned,
  WS1 pattern): restart a daemon, kick a launchd job, flip a flag file.
  This is our feature-flag-flip — scoped to what this Mac runs.
- **D3 — self-generating intake:** cron-driven finder seats file kanban
  triage tasks from real signals (test failures, error logs, TODO/FIXME
  scans, dashboard errors); the triage selector (EXISTS, unwired — the known
  daemon gap) routes them into dev-pipeline@5. `chosen: null` → wrap in
  bare_task_recipe (ratified direction). This closes the loop: the factory
  feeds itself.

## 3. Autonomy graduation (the gate as a dial, not a law)

The One Law stands per phase; the PROGRAM retires it deliberately:

- **Phase A (now):** every instance parks at the operator gate — but on the
  WS4 evidence bundle, not a text verdict.
- **Phase B:** `auto_approve` policy per recipe+risk class (docs-only,
  test-only, dependency bumps ≤ patch). Conditions: WS4 evidence complete,
  suite green, both adversarial verdicts approve, budget under X. Everything
  else still parks. Every auto-approval logs its full evidence bundle —
  audit trail is non-negotiable.
- **Phase C:** auto-approve is the default; the operator gate remains only
  for classes the operator names (schema migrations, deletions, spend,
  public-repo pushes). WS6-D3 intake means the loop runs unattended.
- Graduation criterion per class: N consecutive auto-approvable instances
  where the operator, reviewing after the fact, would have pressed Approve.
  Measured, not vibes — the tripwire table lives in the recipe.

## 4. Order of execution + first dispatch

Dependency-honest order:
1. **WS4 + WS5 + WS6-D1** (one lane, dev-pipeline@5): evidence-bundle gate
   with watched testing, diff-as-story, post-approval auto-merge. Highest
   operator-visible value; also E3 of the existing punch list.
2. **WS2-D1** (tiny lane or fold into 1): symbol-grounding guard.
3. **WS1**: env-bootstrap + app_up (needed in full before WS4's QA leg can
   drive non-trivial apps; the first cut of WS4 can bootstrap ShipFactory's
   own dashboard as its test subject).
4. **WS2-D2/D3**: explore + adversarial planning (dev-pipeline@6).
5. **WS6-D3**: wire the triage selector; add finder crons.
6. **WS3**: fan-out primitive (last — biggest engine change, needs spec §17
   addendum + adversarial test lane).
7. **Phase B autonomy** switch only after ≥1 full clean cycle on each of the
   above.

Standing laws that bind every lane: recipes immutable (@N+1, never edit
published); briefs cap deltas by number; build lane's tests never count as
the adversarial suite; approval gates are operator-owned until the Phase-B
policy EXPLICITLY says otherwise per class; suite green at every merge.

## 5. Explicitly out of scope (for now)

- Cloud machines / EC2 anything — this program runs on the Mac Studio.
- iOS/Android QA legs (flagged in WS4, own lane later).
- Multi-tenant / customer-facing anything.
- A token-allocator layer (far roadmap — after Phase C).
