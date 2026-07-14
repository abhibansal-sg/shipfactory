# Hermes Factory Plugin — Complete Specification v1.0

Date: 2026-07-12. Ratified by: Abhi. Author: Hermes (operator).
Repo: ~/Developer/products/hermes-factory (standalone plugin repo, installed
into ~/.hermes/plugins/headframe/).

## 0. Mission

A production factory (teams, hierarchy, review/approval pipeline, cost
telemetry, watchdogs) built ON Hermes primitives — kanban store/dispatcher,
profiles, plugin hooks, webhooks, dashboard — with harnesses (codex,
claude-code, hermes-native) as swappable executors per seat. Paperclip
(MIT, source on disk at
~/Developer/infra/paperclip/node_modules/@paperclipai/) is the DESIGN
REFERENCE and semantic donor; its Postgres/Drizzle/JWT/realtime infra is
NOT ported. Boards = companies. GitHub Issues = external board of record.

HARD RULES
- ZERO Hermes core modifications. Everything via: plugin register(ctx),
  ctx.register_tool, ctx.register_cli_command, VALID_HOOKS
  (kanban_task_claimed/completed/blocked), kanban_db.dispatch_once(spawn_fn=),
  kanban CLI, hermes webhook, profile dirs.
- Python 3.11 stdlib + Hermes's own modules only. No new pip deps.
  (sqlite3, subprocess, json, threading, http via hermes webhook — no
  requests/flask/etc.)
- All state in get_hermes_home()/factory/ (SQLite factory.db + JSONL logs).
  NEVER hardcode ~/.hermes.
- Every module ships tests (pytest, stdlib mocks). Tests must not touch the
  network or a real kanban board — use tmp_path + monkeypatch.
- Match Hermes plugin conventions: plugin.yaml + __init__.py register(ctx).
  See ~/Developer/products/hermes-mobile/plugins/ and
  hermes_cli/plugins.py for the loader contract.

## 1. Architecture (one diagram)

```
GitHub Issues <--sync--> kanban board (per company) <-- factory daemon
                                                          |  dispatch_once(spawn_fn=headframe_spawn)
                                                          v
                                            executor layer (per-seat config)
                                            | hermes | codex | claude |
                                                          |
                                       run record + token telemetry (factory.db)
                                                          |
                              execution-policy gate (review/approval stages)
                                                          |
                                    watchdogs + monitors (recovery ladder)
```

## 2. Module inventory & file map

```
factory/
  __init__.py           # plugin entry: register(ctx) — tools, CLI, hooks
  plugin.yaml           # name: factory, kind: general
  config.py             # load/validate factory config + seat registry
  store.py              # factory.db SQLite schema + migrations + accessors
  spawn.py              # headframe_spawn(task, workspace, board) -> pid
  daemon.py             # dispatch loop: dispatch_once(spawn_fn=...) + watchdog tick
  policy.py             # execution-policy engine (review/approval stages)
  telemetry.py          # token/cost parsing + JSONL + rollups
  watchdog.py           # monitors + subtree watchdogs + recovery ladder
  hierarchy.py          # reportsTo chain, roles, permission checks
  github_sync.py        # two-way GitHub Issues <-> kanban sync
  cli.py                # `hermes headframe <verb>` argparse tree
  executors/
    __init__.py         # registry: get_executor(name)
    base.py             # Executor ABC: build_cmd, parse_usage, inject_identity
    hermes_exec.py      # hermes -p <profile> chat -q (delegate to kanban default)
    codex_exec.py       # codex exec -s workspace-write w/ AGENTS.md injection
    claude_exec.py      # claude -p headless w/ context injection
  dashboard/
    server.py           # stdlib http.server page (board/seats/costs) — Phase-agnostic minimal
    templates.py        # inline HTML (no framework)
tests/
  test_config.py test_store.py test_spawn.py test_policy.py
  test_telemetry.py test_watchdog.py test_hierarchy.py
  test_github_sync.py test_executors.py test_cli.py test_daemon.py
docs/
  factory-spec.md       # THIS file
  harvest-map.md        # PC module -> factory module mapping (Lane B writes)
```

## 3. Seat model (config)

`$HERMES_HOME/headframe/seats.yaml`:
```yaml
company: straits-lab-eng          # kanban board slug
seats:
  verifier:
    profile: verifier             # hermes profile name (identity: AGENTS.md, skills)
    executor: claude              # hermes | codex | claude
    model: sonnet-5               # passed to executor
    reasoning: adaptive
    reports_to: hermes-cos
    role: qa
    max_concurrent: 2
  dev-backend:
    profile: dev-backend
    executor: codex
    model: gpt-5.6
    reasoning: medium
    reports_to: architect
    role: engineer
hierarchy_gates:
  landers: [release]              # only these seats may set done
  verdicts: [verifier]            # only these seats may APPROVE/REQUEST_CHANGES
```
config.py validates: profile exists (hermes_cli.profiles.profile_exists),
executor known, reports_to acyclic, roles from PC's AGENT_ROLES list.

## 4. Executor layer (spawn.py + executors/)

`headframe_spawn(task, workspace, board) -> Optional[int]`:
1. Look up task.assignee in seat registry. Unknown seat -> return None
   (dispatcher skips, same semantics as skipped_nonspawnable).
2. executor=hermes -> delegate to kanban_db._default_spawn unchanged.
3. executor=codex|claude -> Executor.build_cmd():
   - Inject identity: copy/point profile's AGENTS.md + context into workspace
     (codex: AGENTS.md at workspace root; claude: CLAUDE.md or --append-system-prompt).
   - Build task prompt from kanban build_worker_context(conn, task_id) EXACT
     same context the hermes worker gets (import from hermes_cli.kanban_db).
   - Spawn subprocess.Popen (fire-and-forget, new session), redirect output
     to $HERMES_HOME/headframe/runs/<task_id>-<ts>.log, return pid.
4. Record run row in factory.db (seat, task, executor, model, pid, started_at).
5. On child exit (daemon reaps via pid polling — same pattern as kanban's
   _record_worker_exit): parse usage from log (telemetry.py), finalize run row,
   and call kanban complete/block on the task IF the harness did not (codex/
   claude workers cannot call kanban tools — the DAEMON transitions the task
   from the harness's exit code + a DONE/BLOCKED sentinel line the prompt
   instructs the harness to print as its last output:
   `HEADFRAME_RESULT: done|blocked <one-line summary>`).
   Missing sentinel + exit 0 = blocked with reason "no result sentinel"
   (case-law: exit-0 is not success).

parse_usage per executor:
- codex: "tokens used\n<N>" (and -o json sidecar when present; read both).
- claude: --output-format json usage fields; fallback regex on stream-json.
- hermes: sessions DB usage if reachable else none.

## 5. Execution policy (policy.py) — PC harvest

Port PC's semantics (shared/dist/types/issue.d.ts, services/
issue-execution-policy.d.ts) onto kanban:
- Policy JSON stored per-task in factory.db (kanban schema untouched):
  {mode, commentRequired, stages:[{id, type: review|approval,
   approvalsNeeded:1, participants:[seat...]}]}
- Default policy per board in seats.yaml (our pipeline:
  review:verifier -> approval:architect -> land:release).
- ENFORCEMENT (two hooks, zero core changes):
  a. kanban_task_completed hook: when a worker completes a task that has
     unsatisfied stages -> factory REOPENS it (kanban comment + status back
     to ready assigned to next stage participant) and logs a decision row.
     Completion only sticks when the policy state machine says all stages
     passed.
  b. Stage verdicts are recorded via `hermes headframe verdict <task> \
     --stage review --outcome approve|request_changes --body ...` (tool +
     CLI; the verdict body must pass governor-style citation check:
     file:line regex OR clean-approve, port the verdict-proof regex from
     hermes-loop/contracts/governor.mjs).
- decisions table: (task_id, stage_id, stage_type, seat, outcome, body, at).

## 6. Watchdogs & monitors (watchdog.py) — PC harvest

- monitor: per-task row {task_id, next_check_at, timeout_at, max_attempts,
  attempt_count, notes, recovery_policy: wake_owner|create_recovery_task|
  escalate_to_board, scheduled_by}.
- daemon tick (same loop as dispatch): monitors past next_check_at ->
  recovery ladder: (1) wake_owner = enqueue a kanban comment + re-ready the
  task for its assignee; (2) create_recovery_task = file a new kanban task
  assigned to owner's reports_to; (3) escalate_to_board = task to the top
  of the chain (no reports_to).
- subtree watchdog (PC task-watchdogs): {root_task_id, agent, instructions,
  last_fingerprint}. Fingerprint = sha256 of sorted (task_id, status,
  updated_at) of the subtree (kanban deps as tree). Fingerprint unchanged ->
  skip (zero cost). Changed AND stopped-leaves exist (leaf tasks not
  in_progress, no pending verdict) -> spawn the watchdog seat once with
  instructions + stopped-leaf list.

## 7. Telemetry (telemetry.py)

- runs table: (id, task_id, seat, executor, model, started_at, ended_at,
  exit_code, tokens_in, tokens_out, tokens_total, duration_s, result).
- JSONL mirror at $HERMES_HOME/headframe/telemetry.jsonl (append-only).
- Rollups (pure SQL): by seat/day, by executor, by task; `hermes headframe
  costs [--by seat|executor|task] [--since 7d]` prints a table.
- This answers "is PC burning more than Hermes-native": same work, measured
  per run from day one.

## 8. GitHub sync (github_sync.py)

- One-way-each-direction explicit sync (NOT a hidden daemon thread):
  `hermes headframe sync [--board X] [--repo owner/name]` +
  optional daemon flag --sync-interval.
- gh CLI (already authed) via subprocess; NO tokens in config.
- Mapping: GH issue <-> kanban task; labels seat:<name> -> assignee;
  label priority:P1..P4 -> priority; milestone -> goal tag in task metadata;
  GH closed <-> kanban done (done only via policy engine — a GH close on an
  ungated task marks it done; on a gated task it comments "pending stages").
- State: sync table (gh_number, task_id, etag/updated_at both sides,
  last_synced_at). Conflict rule: newest updated_at wins, loser recorded in
  a conflict log line. Never delete on either side.
- Webhook mode: `hermes webhook` subscription for issues/issue_comment/pr
  events -> targeted single-issue sync (event-driven wake). Document the
  exact `hermes webhook add` command in README; do not implement webhook
  transport itself (Hermes owns it).

## 9. Hierarchy (hierarchy.py)

- From seats.yaml: chain(seat) -> [seat, manager, ...root], acyclic
  validated. escalation_target(seat) = reports_to or board root.
- Gates: may_land(seat), may_verdict(seat) from hierarchy_gates. policy.py
  and cli.py consult these — a verdict from a non-verdict seat is rejected
  at write time (the PR#138 lane-breach class, enforced in code).
- org chart: `hermes headframe org` prints the tree ASCII.

## 10. Dashboard — HERMES DASHBOARD TAB (§10-v2, operator law 2026-07-13)

SUPERSEDED DECISION, said out loud: the standalone dashboard server
(factory/dashboard/, port 18820) was built to the original §10 and is now
RETIRED. Abhi ruled (twice — 2nd time 2026-07-13, first during an earlier
session): factory UI lives INSIDE the Hermes dashboard via the first-class
plugin-dashboard extension system. No second server, port, token, or
maintenance surface. LAW FOR ALL FUTURE LANES: any factory UI work targets
plugins/headframe/dashboard/{manifest.json,plugin_api.py,dist/} —
the same mechanism the kanban plugin tab uses. The standalone server code
may be deleted on sight after the tab ships; hermes headframe dashboard CLI
verb prints a pointer to the dashboard URL instead of serving.

Tab spec (v1 views, priority order):
1. WAITING GATES inbox — every blocked(needs_input) gate across instances,
   approve/reject buttons wired to the same advancer event path as the CLI.
   This is the operator's daily surface; it ships first.
2. INSTANCES — recipe id@version, per-step states/activations, tokens
   charged vs budgets, blocked reasons, cancel(--dry-run)/reroute actions.
3. SEATS + COSTS — ported from the standalone views, reading factory.db.
4. Board views are NOT duplicated — link to the existing kanban tab.
manifest: {"name":"factory","label":"Factory","tab":{"path":"/factory",
"position":"after:kanban"},"entry":"dist/index.js","api":"plugin_api.py"}.
plugin_api.py = FastAPI router reading factory.db + recipe tables (same
data layer the CLI verbs read; no new state).

## 11. CLI (cli.py) — registered via ctx.register_cli_command

`hermes headframe <verb>`: init (write seats.yaml skeleton + db), seats,
org, daemon (runs dispatch+watchdog+optional sync loop; --once for one
tick), verdict, policy (show/set per task), monitor (add/list), watchdog
(add/list), costs, sync, dashboard, runs, pause/resume <seat>.

## 12. Plugin wiring (__init__.py)

register(ctx):
- ctx.register_cli_command("factory", ...) full argparse tree.
- Hooks: kanban_task_completed -> policy.on_complete;
  kanban_task_blocked -> watchdog.on_block (monitor bump);
  kanban_task_claimed -> telemetry.on_claim (run row precreate).
- Tools (service-gated on factory.db existing): headframe_verdict,
  headframe_costs, headframe_monitor_add — JSON-string returns per Hermes tool
  contract.

## 13. Testing contract (every lane)

- pytest, tmp_path-isolated HERMES_HOME (monkeypatch.setenv), no network,
  no real kanban DB writes outside tmp.
- Each module: happy path + the specific failure the design guards
  (sentinel missing, cycle in reports_to, verdict without citation,
  fingerprint unchanged skip, conflict both-updated, usage parse of real
  captured codex/claude output samples — include fixture strings).
- A tests/test_e2e_smoke.py: init -> create task on a tmp board (real
  kanban_db API against tmp db) -> headframe_spawn with executor=hermes
  stubbed spawn -> complete -> policy reopen -> verdict -> done. Proves the
  whole loop headless.

## 14. What is explicitly OUT

No Postgres, no JWT/auth service, no realtime bus, no plugin-sandbox port,
no secrets vault (Hermes .env owns secrets), no Linear, no new chat
surface, no core patches, no npm anything.

## 15. FROZEN INTERFACE CONTRACT (all lanes code against THIS, exactly)

store.py MUST export (sqlite3, factory.db at get_hermes_home()/factory/factory.db):
  init_db() -> None                       # idempotent CREATE TABLE IF NOT EXISTS
  record_run_start(task_id, seat, executor, model, pid) -> run_id:int
  record_run_end(run_id, exit_code, tokens_in, tokens_out, duration_s, result) -> None
  get_policy(task_id) -> dict|None ; set_policy(task_id, policy: dict) -> None
  record_decision(task_id, stage_id, stage_type, seat, outcome, body) -> None
  decisions_for(task_id) -> list[dict]
  add_monitor(task_id, next_check_at, timeout_at, max_attempts, recovery_policy, notes, scheduled_by) -> None
  due_monitors(now_iso) -> list[dict] ; bump_monitor(task_id) -> None ; clear_monitor(task_id) -> None
  add_watchdog(root_task_id, agent, instructions) -> None
  watchdogs() -> list[dict] ; set_watchdog_fingerprint(root_task_id, fp) -> None
  seat_paused(seat) -> bool ; set_seat_paused(seat, paused: bool) -> None
  costs_rollup(by: str, since_days: int) -> list[dict]
  sync_get(gh_number) -> dict|None ; sync_upsert(gh_number, task_id, gh_updated, k_updated) -> None

config.py MUST export:
  load_seats(path=None) -> FactoryConfig  # dataclass: company:str, seats: dict[str, Seat], hierarchy_gates: dict
  Seat dataclass: name, profile, executor, model, reasoning, reports_to, role, max_concurrent
  validate(cfg) raises FactoryConfigError with the precise problem

executors/base.py MUST export:
  class Executor(ABC): name: str
    build_cmd(self, seat: Seat, prompt: str, workspace: str) -> list[str]
    parse_usage(self, log_text: str) -> dict  # {tokens_in,tokens_out,tokens_total} zeros if unknown
    identity_files(self, seat: Seat, workspace: str) -> None  # write AGENTS.md/CLAUDE.md into workspace
  get_executor(name: str) -> Executor  (in executors/__init__.py)

policy.py MUST export:
  on_complete(task_id, board, assignee, summary) -> dict  # {"action": "allow"|"reopen", "next_stage": ...}
  record_verdict(task_id, stage_id, outcome, body, seat) -> dict  # raises on citation-gate fail / gate breach
  citation_ok(body: str) -> bool   # port of governor.mjs verdict-proof regex

hierarchy.py MUST export:
  chain(cfg, seat) -> list[str] ; may_land(cfg, seat) -> bool ; may_verdict(cfg, seat) -> bool
  validate_acyclic(cfg) -> None (raises)

telemetry.py MUST export:
  parse_usage(executor_name: str, log_text: str) -> dict (delegates to executor)
  append_jsonl(record: dict) -> None ; on_claim(task_id, board, assignee, **kw) -> None

spawn.py MUST export:
  headframe_spawn(task, workspace: str, *, board=None) -> int|None   # signature matches kanban spawn_fn
  reap_finished() -> list[dict]  # poll recorded pids, finalize runs, transition tasks via kanban CLI

Sentinel (executor prompts MUST instruct, daemon MUST parse):
  last line of harness output: `HEADFRAME_RESULT: done|blocked <summary>`

All timestamps ISO8601 UTC strings. All public functions get docstrings.

## 16. VALIDATION PASS (post-build hardening — Nous-sweeper standard, 2026-07-12)

Built because the 4-lane build tested lanes against STUBS. This pass applies
the three lessons from NousResearch's review of our PR #62496:
(1) tests must drive REAL entry points, (2) every call-site needs an
execution-context answer (what runs where; what does a SLOW callee do),
(3) docs must match shipped behavior exactly.

### Lane V1 — REAL-PATH E2E (owner: terra)
Files: tests/test_e2e_smoke.py, tests/test_e2e_cli.py, docs/VALIDATION.md
- test_e2e_smoke.py: NO stubs of factory modules. tmp HERMES_HOME; create a
  REAL kanban board via hermes_cli.kanban_db (sys.path to
  ~/Developer/products/hermes-mobile); real headframe.store init_db; real
  seats.yaml via factory.config; add a task; run kanban dispatch_once with
  the REAL headframe_spawn but executor command swapped to a /bin/sh stub
  harness that prints tokens + HEADFRAME_RESULT sentinel (spawn a REAL
  subprocess — only the AI harness binary is faked); reap_finished();
  assert: run row finalized w/ parsed tokens, task transitioned; then a
  policy: set a review stage, complete the task, assert policy.on_complete
  reopens; record_verdict w/ citation, assert satisfied; done.
  Cover BOTH sentinel outcomes and the missing-sentinel=blocked rule.
- test_e2e_cli.py: run factory/cli.py verbs via subprocess (real argv, real
  tmp HERMES_HOME): init, seats, org, policy show, costs, runs, pause,
  resume, daemon --once (empty board OK). Assert exit codes + key output.
  This also VERIFIES README's quickstart is truthful — fix README if not.
- docs/VALIDATION.md: what was proven, command transcript, gaps left.

### Lane V2 — ADVERSARIAL / EXECUTION-CONTEXT (owner: luna, high)
Files: tests/test_adversarial.py, docs/EXECUTION-CONTEXT.md, minimal
production fixes in factory/*.py where a finding demands one (smallest
diff, comment with #16-V2 tag).
- EXECUTION-CONTEXT.md: for EVERY call site in daemon.py, spawn.py,
  dashboard/server.py, github_sync.py answer: what executes here (main
  loop / thread / subprocess)? what does a SLOW callee do? what does a
  crashed callee do? Table format, file:line cites.
- test_adversarial.py, minimum:
  a. SLOW gh: github_sync tick with subprocess.run stub sleeping > its
     timeout — assert daemon tick completes bounded (add a timeout to gh
     calls if missing: that IS the expected fix).
  b. Hung executor: spawned pid alive past claim TTL — assert reap doesn't
     block and TTL semantics hold.
  c. Dashboard under a slow store accessor — server thread must not wedge
     other requests indefinitely (document if single-threaded by design).
  d. Malformed HEADFRAME_RESULT lines (garbage, multiple, mid-log) — parser
     must be last-line-wins, never crash.
  e. store.py under concurrent writers (2 threads x 50 ops) — no
     sqlite 'database is locked' unhandled (WAL or busy_timeout = fix).
  f. citation_ok fuzz: empty, unicode, 10MB body, regex-metachar payloads.
- Every finding = case-law style entry in EXECUTION-CONTEXT.md: pattern,
  file:line, fix or explicit accepted-risk.

Gate: BOTH lanes' suites + the original 37 ALL green together via
~/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q.

## 17. RECIPE EXECUTION (v2 — CONVERGED LAW, 2026-07-13)

Two-round adversarial validation (round 1: NO-GO, ~40 findings, 2 live bugs
confirmed at source; round 2: 10 operator positions argued, 5 material
corrections adopted). Operator adjudication of round-2 pushbacks: POS-1
CONCEDED to validator (auto_decompose defaults True at
kanban_watchers.py:50 — exclusive ownership + fail-closed startup required);
POS-3 CONCEDED (recorded-usage ceilings unbounded without cap wiring;
admission-debit model adopted; validator honestly could not prove the PC
burn attribution and it stays OUT of the spec); POS-4 CONCEDED (cancelling
+ blocked states); POS-7 CONCEDED (completion collectors — entry-root
linking releases parents at start not finish); POS-9 CONCEDED (grow build-1
with the proven prerequisites). POS-2/5/6/8/10 converged as argued.

### §17-v2 Recipe execution

#### 17.1 Authority and deployment

Kanban SHALL remain the only task scheduler, claimer, dependency gate, TTL owner, and retry engine. Factory SHALL load recipes, instantiate task graphs, advance control steps, reconcile missed events, enforce recipe budgets, and record decisions. Recipe code MUST NOT implement a scheduler or retry loop.

Recipes SHALL be the sole flow authority on recipe-enabled boards. Legacy policy templates and the legacy reopen state machine SHALL NOT run there.

A recipe-enabled deployment MUST set `kanban.auto_decompose: false`. Factory startup MUST refuse recipe triage routing while Hermes auto-decompose is enabled. Factory SHALL lease triage work before invoking any selector.

`workflow_template_id` and `current_step_key` MAY be compatibility annotations but SHALL NOT be authoritative. All recipe state SHALL live in `factory.db`.

#### 17.2 Board configuration

`seats.yaml` SHALL contain:

```yaml
recipes:
  enabled: true
  library_path: /absolute/path/to/recipes
  bare_task_recipe: bare-task-default@1
  notify_target: telegram:home
  board_day_token_ceiling: 500000
  dispatcher_max_in_progress: 4
  execution_profiles:
    standard:
      max_runtime_seconds: 1800
      max_retries: 2
      token_allowance: 50000
```

Every referenced execution profile and seat MUST exist at startup. Factory SHALL pass `dispatcher_max_in_progress` to `dispatch_once`. Recipe YAML SHALL NOT declare concurrency, executor, model, or arbitrary retry values. Seats own executor and model; execution profiles own runtime, retry, and admission bounds.

#### 17.3 Recipe YAML schema

Published recipes SHALL use this exact shape:

```yaml
schema: headframe.recipe/v1
id: dev-pipeline
version: 1
status: active
description: Build, verify, review, approve, and land a change.
intent_tags: [software-change]
supersedes: null

parameters:
  request:
    type: string
    required: true
  approval_due_at:
    type: datetime
    required: false
    default: null

budgets:
  max_activations: 12
  max_step_activations: 3
  max_tokens: 300000

steps:
  - id: build
    primitive: agent_task
    title: Build the change
    needs: []
    optional: false
    params:
      seat: dev-backend
      instructions: |
        Implement the requested change.
        Return tested, reviewable work.
      execution_profile: standard
      workspace: worktree

  - id: review
    primitive: review_gate
    title: Review the change
    needs: [build]
    optional: false
    params:
      seat: verifier
      instructions: |
        Review correctness, regressions, and evidence.
      execution_profile: standard
      workspace: worktree
```

Top-level keys SHALL be exactly `schema`, `id`, `version`, `status`, `description`, `intent_tags`, `supersedes`, `parameters`, `budgets`, and `steps`. Unknown keys SHALL fail validation.

`id` and step IDs MUST match `[a-z][a-z0-9-]{0,63}`. `version` MUST be a positive integer. `status` MUST be `active` or `deprecated`. `supersedes` MUST be null or `id@version`.

Parameter types SHALL be `string`, `integer`, `boolean`, `enum`, or `datetime`. An enum MUST declare `values`. Missing required parameters, unknown parameters, and type mismatches SHALL fail closed.

String fields MAY contain `${parameter_name}` substitutions. Missing substitutions SHALL fail validation. Non-string parameters MAY appear only as a complete scalar substitution.

Every step SHALL contain exactly `id`, `primitive`, `title`, `needs`, `optional`, and `params`. `needs` is the only ordering syntax. Empty `needs` permits parallel execution.

`skip_steps` is an instantiation argument, not an expression language. Only steps with `optional: true` MAY be skipped. `review_gate` and `approval_gate` MUST NOT be optional. Skipped nodes SHALL be dependency-spliced to their effective upstream requirements.

Parallel write-capable `agent_task` steps MUST use separate `worktree` workspaces. Unordered shared-workspace steps MUST all be `readonly`. Validation SHALL reject every other parallel shared-workspace graph.

A published `id@version` SHALL be immutable. Factory SHALL normalize the document, compute SHA-256, and persist the normalized document in `recipe_versions`. Loading different content for an existing `id@version` SHALL fail. Instances SHALL pin `id`, `version`, and hash.

Nested runtime recipes SHALL NOT exist in v1. Every instantiated recipe graph SHALL contain primitive steps only.

#### 17.4 Primitive registry

Exactly five primitives SHALL ship:

1. `agent_task`

   Required params: `seat`, `instructions`, `execution_profile`, `workspace`. `workspace` is `worktree`, `shared`, or `readonly`. Activation creates an assigned kanban task with compiled runtime and retry limits. Successful completion marks the activation done and creates a new output revision. A worker block marks the activation and instance blocked.

2. `review_gate`

   Required params are the same as `agent_task`. The worker’s last line MUST be:

   `HEADFRAME_VERDICT: {"outcome":"approve","body":"..."}`

   or:

   `HEADFRAME_VERDICT: {"outcome":"request_changes","target_step":"step-id","body":"..."}`

   The body MUST pass citation validation. A change request MUST name one transitive upstream `agent_task`. The advancer SHALL invalidate the dependency cone from that target through the rejecting gate, insert new activations for affected steps, and bind all later decisions to the new upstream revision vector.

3. `approval_gate`

   Required params: `approvers`, `instructions`. Activation creates an unassigned kanban task parked as `blocked(kind=needs_input)`, clears all claim/PID state, and writes a notification outbox row. `recipe approve` completes it. `recipe reject` blocks the instance for operator disposition; it SHALL NOT choose a producer or rerun work automatically.

4. `notify`

   Required params: `target`, `message`. Activation writes one uniquely keyed outbox row. The daemon SHALL deliver it with `hermes send --to <target> <message>`. Success completes the step. Failure reschedules the outbox with bounded backoff and no LLM call.

5. `wait_for_event`

   Required param: `event`. Optional param: `due_at`. Activation creates an unassigned task parked as `blocked(kind=needs_input)` with no claim/PID. A matching idempotent event completes it. If `event: timer`, `due_at` is required and the daemon emits the successful timer event. For every other event, expiry at `due_at` blocks the instance with `event_timeout`.

No primitive MAY poll with a model.

#### 17.5 Persistence

Factory SHALL persist:

- `recipe_versions(id, version, hash, status, normalized_yaml)`.
- `recipe_instances(id, board, collector_task_id, recipe_id, recipe_version, recipe_hash, status, parameters_json, activation_count, tokens_charged, blocked_reason, created_at, updated_at)`.
- `recipe_steps(instance_id, step_id, activation, primitive, state, kanban_task_id, input_revision_hash, output_revision, blocked_reason, created_at, updated_at)`.
- `advance_events(key, source, payload_json, state, created_at, applied_at)`.
- `budget_charges(key, board, utc_day, instance_id, step_id, activation, tokens)`.
- `outbox(key, target, message, state, attempts, next_attempt_at, delivered_at)`.
- Triage selection rows containing the source task, ranked candidates, reasons, chosen version, parameters, skips, and reroute outcome.

`recipe_steps` MUST be unique on `(instance_id, step_id, activation)` and on non-null `kanban_task_id`.

#### 17.6 State machines

Instance states SHALL be:

`running | waiting_gate | waiting_event | blocked | cancelling | cancelled | done | failed`

Step-activation states SHALL be:

`pending | ready | running | waiting | blocked | done | skipped | cancelled | failed`

Only the advancer MAY write recipe states. Kanban MAY independently change task state; the advancer SHALL observe and reconcile it.

Allowed step transitions are:

- `pending -> ready` when effective dependencies are done or skipped.
- `ready -> running` for `agent_task` and `review_gate`.
- `ready -> waiting` for `approval_gate`, `notify`, and `wait_for_event`.
- `running -> done | blocked`.
- `waiting -> done | blocked`.
- `pending | ready | waiting | blocked -> cancelled`.
- Any nonterminal state MAY become `failed` only for an invariant or unrecoverable execution error.
- Reactivation SHALL insert a new activation row; prior done rows SHALL remain immutable.

Instance status is a summary over its latest step activations. `cancelling`, `cancelled`, `failed`, and `done` take precedence; then `blocked`; then active runnable work; then `waiting_gate`; then `waiting_event`.

`blocked` is recoverable by a typed event or audited operator command. `failed` is terminal. Recovery from failed work requires a new or rerouted instance.

#### 17.7 Advancer contract

Hooks, CLI commands, webhooks, timers, and reconciliation SHALL enqueue events. They SHALL NOT perform flow mutations directly.

Every transition SHALL use:

`sha256(instance_id | recipe_hash | step_id | activation | transition | source_id)`

as its unique advance key. `source_id` MUST be a kanban event/run ID, decision ID, external event ID, due timestamp, or operator command UUID.

Every task activation SHALL use kanban idempotency key:

`recipe/<instance_id>/<recipe_hash>/<step_id>/<activation>`

The advancer SHALL claim one unapplied event, verify the instance is not cancelling or cancelled, compare the expected activation/state, persist the intended action, execute the idempotent kanban/outbox mutation, and mark the event applied. Duplicate or stale events SHALL be no-ops.

Only one leased advancer action MAY execute at a time for an instance. CLI and webhook processes SHALL enqueue; the Factory daemon SHALL execute.

Completion hooks are latency hints only. Every daemon tick SHALL reconcile nonterminal recipe steps against kanban tasks, runs, decisions, outbox rows, and due waits. A swallowed hook or daemon restart MUST therefore reproduce the same advance key and finish the missing transition without duplicate tasks.

Each instance SHALL have an inert, unassigned completion collector. Terminal recipe steps SHALL be kanban parents of that collector. The advancer SHALL complete the collector deterministically when the instance reaches done. Collectors MUST never be assigned to a model seat.

#### 17.8 Budget fuse v1

An activation is the launch of `agent_task` or `review_gate`. Kanban retries within one activation and SHALL retain its existing retry semantics.

Before creating an activation task, one Factory transaction SHALL:

- Refuse if instance activation count would exceed `max_activations`.
- Refuse if that logical step would exceed `max_step_activations`.
- Read the execution profile’s `token_allowance`.
- Refuse if instance charged tokens plus allowance would exceed `max_tokens`.
- Refuse if the UTC board-day charged tokens plus allowance would exceed `board_day_token_ceiling`.
- Otherwise insert the unique budget charge and increment the counters.

Refusal SHALL set the step and instance to blocked with `activation_fuse`, `instance_budget`, or `board_day_budget`. It SHALL NOT repeatedly probe.

Admission charges are non-refundable in v1. Actual token usage SHALL be recorded separately and SHALL NOT release charges. Unknown actual usage SHALL remain unknown, not zero.

An operator MAY raise a ceiling or reset a blocked fuse only with an audited command containing a reason. Lowering a ceiling below already charged usage SHALL be rejected.

#### 17.9 Events, gates, and notifications

`factory event <instance> <step> <payload-json>` SHALL require payload fields `id` and `type`. `(instance, step, id)` MUST be unique. Authenticated Hermes webhook mappings SHALL invoke the same event service.

Human gates and event waits SHALL always appear in kanban as unassigned `blocked(kind=needs_input)` tasks with no live run, claim, or PID.

Factory SHALL expose `recipe waiting` and `recipe show`. Waiting views SHALL not depend on successful chat delivery.

Gate activation and bounded reminders SHALL use the persistent outbox. Outbox delivery failures SHALL be retried without advancing the gate and without an LLM call.

#### 17.10 Triage selection

The Factory daemon SHALL poll triage tasks only on its configured recipe board and lease each source task before calling the selector.

The selector SHALL receive the active recipe manifest library and validated seat roster. It SHALL return a graph of nodes. Every node SHALL contain a title, body, sibling `needs`, ranked candidates with scores and reasons, one chosen `id@version` or null, parameters, and `skip_steps`.

The validator SHALL reject unknown/deprecated versions, invalid node references, cycles, unknown parameters, required-step skips, missing seats/profiles, unsafe workspace parallelism, and budget/profile violations. It SHALL never delete or sanitize an invalid dependency.

A null or invalid choice SHALL park the source task as `blocked(kind=needs_input)` with `no_recipe_match` and the ranked mismatch reasons. V1 SHALL NOT draft recipes.

Each selected node SHALL instantiate one sibling recipe instance with an inert completion collector. Sibling dependencies SHALL link dependent entry steps below prerequisite completion collectors. The original triage parent SHALL be an unassigned collector linked below all sibling collectors. It SHALL carry no recipe.

`recipe reroute` SHALL replace an unactivated instance in place. After any activation, reroute SHALL cancel the old instance, retain its artifacts and audit history, and instantiate a new version.

#### 17.11 Cancellation

`recipe cancel --dry-run` SHALL list active workers, nonterminal steps, completed external actions that cannot be undone, and all downstream tasks or collectors that will remain suppressed.

Cancellation SHALL first set the instance to `cancelling`. This fence MUST make every advancer event and reconciliation pass refuse new activation.

The cancel reconciler SHALL terminate active process groups and confirm exit. If any worker survives, the instance SHALL remain cancelling and no task dependency SHALL be released.

After workers stop, one kanban transaction SHALL clear assignees and claims and archive all nonterminal internal step tasks. Completed and skipped history SHALL remain unchanged. Recipe step rows SHALL become cancelled and the instance SHALL become cancelled.

The instance completion collector SHALL be parked unassigned in `blocked(kind=needs_input)` with `recipe_cancelled`; it SHALL NOT be archived or completed. Therefore cancellation MUST NOT satisfy an outer kanban dependency. Reroute MAY attach a replacement instance and release that collector only after the replacement finishes.

Cancellation SHALL suppress future actions but SHALL NOT claim to reverse completed payments, messages, bookings, commits, or other external effects.

#### 17.12 Build 1 scope

Build 1 SHALL ship:

- The two prerequisite bug fixes in §17.13 as separate commits.
- Durable Factory run records and daemon reconstruction before the first dispatch tick.
- Run accounting for all executors; unknown native usage remains unknown.
- Dispatcher concurrency-cap wiring.
- Recipe board configuration and exclusive triage-ownership validation.
- Immutable recipe loader, validator, and `recipe_versions`.
- Instance, step-activation, advance-event, budget-charge, outbox, and triage-selection persistence.
- Flat task-per-step instantiation and inert completion collectors.
- The idempotent advancer and full daemon reconciliation.
- All five primitives.
- Review targeting and revision-vector invalidation.
- Human-gate parking, waiting queries, notification outbox, events, webhooks, and due-time handling.
- Activation and token admission fuses.
- `recipe show`, `waiting`, `approve`, `reject`, `event`, `cancel --dry-run`, `cancel`, and `reroute`.
- `bare_task_recipe` adoption on recipe-enabled boards.
- `dev-pipeline@1`.
- `aheli-po-intake@1` using simulated external events and no real payment or supplier side effects.
- Restart, duplicate-event, gate-revision, budget, three-day wait, selector-race, reroute, and cancellation regression tests.

Build 1 SHALL NOT ship:

- Recipe drafting or draft approval.
- Nested recipe includes.
- Reservation release or actual-cost reconciliation.
- Arbitrary seat/model overrides.
- GitHub synchronization of recipe instances.
- New dashboard pages.
- Model polling.
- Fingerprints beyond the prescribed advance and task idempotency keys.
- Live Aheli purchasing, payment, booking, shipment, or delivery actions.

#### 17.13 Mandatory pre-recipe fixes

Recipe code MUST NOT merge before both fixes are independently committed and tested.

1. Monitor recurrence fix:

   Monitors SHALL add `interval_seconds`. Each recovery attempt SHALL advance exactly one rung. After its action, one transaction SHALL increment the attempt and either set `next_check_at = now + interval_seconds` or delete the row. Reaching `timeout_at`, the top rung, a terminal task, or `max_attempts` SHALL close the monitor. Terminal escalation MUST occur at most once.

2. Double-governance fix:

   Legacy `policy.on_complete` SHALL return without mutation when no explicit task policy exists. It SHALL never manufacture `_default_policy`. Recipe-bound tasks SHALL bypass legacy policy flow. After recipe cutover, `_default_policy`, `_reopen`, and legacy flow authority SHALL be removed; citation, hierarchy, participant, and decision validation SHALL remain reusable gate-library code.

#### 17.14 Fresh-pass amendments (terra round-3, 2026-07-13 — operator-adjudicated)

Round-3 fresh-eyes (different model, zero prior involvement) found 2 BLOCKER
+ 4 MAJOR items both prior rounds missed; budget-fuse arithmetic and Aheli
expressibility were verified GO with worked examples. Amendments:

1. ROOT-COLLECTOR COMPLETION (was BLOCKER): the triage parent collector is
   completed EXPLICITLY by the advancer when all sibling instance collectors
   are done — same mechanism as instance collectors, selection-scoped
   idempotency key (sha256(selection_id|transition|source)). A selection row
   already persists (17.5); it gains root_collector_task_id. No unassigned
   task is ever left to rot in ready.
2. CANCELLATION ATOMICITY (was BLOCKER): plugin-side ordering PLUS one
   additive kanban API. (a) Archive strictly LEAF-FIRST (reverse topological)
   so no archive ever satisfies a live dependent. (b) NAMED EXCEPTION to the
   zero-core rule (operator-ratified): TWO additive kanban APIs land as
   normal fork commits with tests, usable beyond factory and upstream-
   candidates: `create_blocked_task(..., block_kind, reason)` (atomic
   insert-as-sticky-blocked; kills the create-ready-then-block dispatch
   race) and `cancel_subtree(task_ids, keep_blocked=[collector])` (verify
   worker exit, archive without per-task recompute, single recompute at
   end). Factory startup MUST verify both APIs exist (capability probe) and
   refuse recipes on older kanban.
3. running -> cancelled IS a legal step transition, permitted only after
   confirmed process-group exit (17.6 amended).
4. NOTIFY STATE: a pending/retrying notify step summarizes as instance
   `running` — no new instance state. Outbox backoff is invisible to the
   state machine.
5. HUMAN-GATE CREATION uses create_blocked_task (amendment 2b) — the
   blocked(kind=needs_input) SHALLs in 17.4/17.9 bind to it.
6. WORKSPACE readonly DELETED from v1 schema (was unenforceable:
   VALID_WORKSPACE_KINDS has no readonly; codex executor hardcodes
   workspace-write). v1 rule: parallel steps REQUIRE separate worktrees;
   `shared` workspace steps MUST be totally ordered via needs. The 17.3
   example's `workspace: worktree` is corrected to `worktree`. Read-only
   execution returns in v2 as an executor access-mode contract.
7. BUILD-1 SCOPE grows by: the two kanban APIs (2b) + their tests +
   capability probe; root-collector completion; leaf-first cancel ordering.

#### 17.15 Artifact Discipline (GSD/Spec-Kit harvest)

Factory adopts the donor systems' artifact content while retaining §17's
deterministic runtime and sole-state rules.

**Adopt:** substantive step summaries with dependency frontmatter; bounded
executor deviation authority; adversarial goal-backward verification;
four-level artifact checks (exists, substantive, wired, real data flow);
pre-execution plan scoring; requirements-as-unit-tests checklists; bounded
clarification markers; and ephemeral continue-here handoffs.

**Adapt:** donor phase, plan, Claude, and filesystem vocabulary becomes recipe
instances, step activations, seats, kanban tasks, and kanban comments. Human
gate handoffs are comments consumed by a `RESUMED` marker. Selector output
records informed-default assumptions and at most three prioritized
clarifications. GSD's revision-loop lesson becomes one persisted finding count
per review activation and deterministic stall escalation.

**Ignore:** donor disk state machines, constitution mechanisms, nested model
orchestrators, scheduler/retry machinery, package auto-install behavior, and
additional gate or primitive types. `factory.db` remains sole recipe state,
kanban remains the scheduler, §17 remains the constitution, and the five
primitives remain exhaustive.

The harvest authorizes exactly two engine deltas: (1) review-stall detection
and audited operator release, and (2) continue-here comments on human-gate
park/resume. Future artifact-discipline work SHALL land as recipe content,
templates, selector contract content, tests, or documentation unless a new
operator-ratified §17 amendment explicitly changes this cap.

Files under `recipes/templates/` are canonical. Recipe instructions SHALL
reference the canonical path and inline only a five-to-eight-line executable
summary so a seat can act without guessing. Content changes begin in the
template and then propagate to every referencing recipe; inline summaries do
not silently redefine the canon.

Every future §17 amendment commit body SHALL include a `Sync-Impact` section
listing each affected template and recipe. Each entry SHALL carry `✅` when
propagated in that commit or `⚠` with the explicit pending reason. Omitting an
affected artifact from that list is a spec violation.
