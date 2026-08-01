# Weave Lane-A Activation Runbook

**State as of 2026-08-01 (all verified by execution, not assumption):**

- Recipes `weave-phase-intake` (hash 656bd2782ba6) and `weave-sprint-build`
  (hash e9fd9bb0f0b7) are PUBLISHED in `recipes/v1/` and load cleanly.
- Seat `weave-intake` (codex / gpt-5.6-sol / high) added to seats.yaml
  (backup: seats.yaml.bak-20260801-172224). All seats for both recipes resolve.
- NOT attached to any Project yet — nothing can launch. Attach = operator go.
- Weave Project `p_26321a35` currently points at the LIVE checkout
  `/Users/abbhinnav/Developer/products/weave` — never launch runs against it.

**Blocked on Abhi's consolidation** (final folder layout, Linear phase, main
branch state). After he says GO, execute in order:

## 1. Lane worktree (adjust path if consolidation moved the repo)

```bash
cd /Volumes/MainData/Developer/products/weave   # or consolidated path
git fetch origin && git worktree add ../weave-lane-a -b lane-a origin/main
```

## 2. Lane Project + board (Hermes CLI)

```bash
hermes project create weave-lane-a --name "Weave Lane A" \
  --folder /Volumes/MainData/Developer/products/weave-lane-a
hermes project bind-board weave-lane-a   # creates/binds board weave-lane-a
```

Then confirm ShipFactory sees it: `project_list` via dashboard.plugin_api →
new project id, binding=bound, primary = lane worktree. Record the fresh
project id — never reuse p_26321a35 for lane runs.

## 3. Attach recipes (OPERATOR POLICY ACTION — needs Abhi's explicit go)

```python
# PUT /v1/projects/{id}/recipes/{name} via dashboard.plugin_api
update_project_graph_recipe_v1(LANE_PROJECT_ID, "weave-phase-intake",
    GraphProjectRecipeWrite(enabled=True, is_default=False))
update_project_graph_recipe_v1(LANE_PROJECT_ID, "weave-sprint-build",
    GraphProjectRecipeWrite(enabled=True, is_default=True))
```

sprint-build = default (most-launched); phase-intake named explicitly per phase.

## 4. First run: phase-intake

- Request = the ratified phase plan text from Abhi's planning session /
  Linear phase. Self-contained; include repo-relative paths.
- Launch key: `sf-<UTCstamp>-weave-phase-intake-<8hex>` — unique, never reused.
- Launch via create_graph_run_v1 (validated handler, no shell quoting).
- Abhi decides the human box IN THE DASHBOARD (agents never decide).
- After approval: file one Linear issue per sprint from the file-report
  output (Hermes does this; boxes are network-deny). Follow the
  `linear-agent-operating-model` skill: `S<n>.<m>` naming, agent-native
  issue template, `delegate:factory` label, planned into the ACTIVE CYCLE
  (cycle = sprint per Abhi's ruling; enable cycles on the team first if off —
  needs human API key), under the phase's milestone. Run-ID backlink comment
  on every issue.

## 5. Sprint runs

- One `weave-sprint-build` run per sprint issue, sequential in v1.
- Request = full sprint brief (goal, file scope, acceptance criteria, test
  commands) pasted from the Linear issue, PLUS a `Phase context:` block —
  3-5 lines quoting the milestone's ratified intent. Correctness-review
  emits a "Scope check: CLEAN/DRIFT" line against it (recipe hash 656b+/c4fe+
  superseded by the drift-check edit; re-read hashes at attach time).
- DRIFT-BACK-TO-PLAN (Hermes bridge duty, both ends of every run):
  (a) BEFORE filing sprint issues and BEFORE launching any sprint run,
  re-read the LIVE Linear milestone + issue — Abhi may have edited the plan
  after intake; the frozen request must match current Linear or the launch
  waits. (b) BEFORE integrating an approved sprint to main, re-read the live
  issue again and confirm the approved work still matches it; mismatch →
  stop and surface to Abhi, never merge on a stale plan.
- After Abhi approves: Hermes integrates OUTSIDE the graph — verify lane
  diff, commit (author: Abhinav Bansal <abhibansal-sg@users.noreply.github.com>),
  merge to main, run full suites, reset lane worktree onto new main:
  `git -C ../weave-lane-a fetch origin && git -C ../weave-lane-a reset --hard origin/main`
  (or merge main into lane-a and continue).

## Known limits (accepted for v1, revisit after shakedown)

- Builder seat weave-builder = codex/gpt-5.6-luna, max_concurrent 4 but lane
  is sequential anyway.
- tests/lean baseline on weave main: 76 pre-existing failures
  (test_provision_weave_home ×75 + test_lean_target_launcher ×1) — reviewers
  must baseline-diff, not blame the sprint. State this in every sprint brief.
- Fast lane (spark/haiku builders) deferred until rework-rate data exists.
- No auto Linear→launch trigger: Hermes is the bridge both directions.
- Codex sandbox cannot bind TCP ports — sprints needing gateway/loopback
  tests must mark those tests operator-run in acceptance criteria.
