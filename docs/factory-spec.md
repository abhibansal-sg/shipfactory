# Hermes Factory Plugin — Complete Specification v1.0

Date: 2026-07-12. Ratified by: Abhi. Author: Hermes (operator).
Repo: ~/Developer/products/hermes-factory (standalone plugin repo, installed
into ~/.hermes/plugins/factory/).

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
                                                          |  dispatch_once(spawn_fn=factory_spawn)
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
  spawn.py              # factory_spawn(task, workspace, board) -> pid
  daemon.py             # dispatch loop: dispatch_once(spawn_fn=...) + watchdog tick
  policy.py             # execution-policy engine (review/approval stages)
  telemetry.py          # token/cost parsing + JSONL + rollups
  watchdog.py           # monitors + subtree watchdogs + recovery ladder
  hierarchy.py          # reportsTo chain, roles, permission checks
  github_sync.py        # two-way GitHub Issues <-> kanban sync
  cli.py                # `hermes factory <verb>` argparse tree
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

`$HERMES_HOME/factory/seats.yaml`:
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

`factory_spawn(task, workspace, board) -> Optional[int]`:
1. Look up task.assignee in seat registry. Unknown seat -> return None
   (dispatcher skips, same semantics as skipped_nonspawnable).
2. executor=hermes -> delegate to kanban_db._default_spawn unchanged.
3. executor=codex|claude -> Executor.build_cmd():
   - Inject identity: copy/point profile's AGENTS.md + context into workspace
     (codex: AGENTS.md at workspace root; claude: CLAUDE.md or --append-system-prompt).
   - Build task prompt from kanban build_worker_context(conn, task_id) EXACT
     same context the hermes worker gets (import from hermes_cli.kanban_db).
   - Spawn subprocess.Popen (fire-and-forget, new session), redirect output
     to $HERMES_HOME/factory/runs/<task_id>-<ts>.log, return pid.
4. Record run row in factory.db (seat, task, executor, model, pid, started_at).
5. On child exit (daemon reaps via pid polling — same pattern as kanban's
   _record_worker_exit): parse usage from log (telemetry.py), finalize run row,
   and call kanban complete/block on the task IF the harness did not (codex/
   claude workers cannot call kanban tools — the DAEMON transitions the task
   from the harness's exit code + a DONE/BLOCKED sentinel line the prompt
   instructs the harness to print as its last output:
   `FACTORY_RESULT: done|blocked <one-line summary>`).
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
  b. Stage verdicts are recorded via `hermes factory verdict <task> \
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
- JSONL mirror at $HERMES_HOME/factory/telemetry.jsonl (append-only).
- Rollups (pure SQL): by seat/day, by executor, by task; `hermes factory
  costs [--by seat|executor|task] [--since 7d]` prints a table.
- This answers "is PC burning more than Hermes-native": same work, measured
  per run from day one.

## 8. GitHub sync (github_sync.py)

- One-way-each-direction explicit sync (NOT a hidden daemon thread):
  `hermes factory sync [--board X] [--repo owner/name]` +
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
- org chart: `hermes factory org` prints the tree ASCII.

## 10. Dashboard (dashboard/)

Minimal, stdlib-only (http.server, single process, localhost bind,
token query param reusing kanban dashboard's pattern):
- GET / -> board columns (todo/ready/in_progress/review/done) per company.
- GET /seats -> seat table: executor, model, running (live pids), today's
  runs, today's tokens, ledger of last 5 outcomes.
- GET /runs/<id> -> run log tail + usage + task link.
- GET /costs -> telemetry rollup (by seat/day).
- POST /pause?seat=X -> sets seat.paused in factory.db (spawn skips paused).
Launch: `hermes factory dashboard [--port 18820]`.

## 11. CLI (cli.py) — registered via ctx.register_cli_command

`hermes factory <verb>`: init (write seats.yaml skeleton + db), seats,
org, daemon (runs dispatch+watchdog+optional sync loop; --once for one
tick), verdict, policy (show/set per task), monitor (add/list), watchdog
(add/list), costs, sync, dashboard, runs, pause/resume <seat>.

## 12. Plugin wiring (__init__.py)

register(ctx):
- ctx.register_cli_command("factory", ...) full argparse tree.
- Hooks: kanban_task_completed -> policy.on_complete;
  kanban_task_blocked -> watchdog.on_block (monitor bump);
  kanban_task_claimed -> telemetry.on_claim (run row precreate).
- Tools (service-gated on factory.db existing): factory_verdict,
  factory_costs, factory_monitor_add — JSON-string returns per Hermes tool
  contract.

## 13. Testing contract (every lane)

- pytest, tmp_path-isolated HERMES_HOME (monkeypatch.setenv), no network,
  no real kanban DB writes outside tmp.
- Each module: happy path + the specific failure the design guards
  (sentinel missing, cycle in reports_to, verdict without citation,
  fingerprint unchanged skip, conflict both-updated, usage parse of real
  captured codex/claude output samples — include fixture strings).
- A tests/test_e2e_smoke.py: init -> create task on a tmp board (real
  kanban_db API against tmp db) -> factory_spawn with executor=hermes
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
  factory_spawn(task, workspace: str, *, board=None) -> int|None   # signature matches kanban spawn_fn
  reap_finished() -> list[dict]  # poll recorded pids, finalize runs, transition tasks via kanban CLI

Sentinel (executor prompts MUST instruct, daemon MUST parse):
  last line of harness output: `FACTORY_RESULT: done|blocked <summary>`

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
  ~/Developer/products/hermes-mobile); real factory.store init_db; real
  seats.yaml via factory.config; add a task; run kanban dispatch_once with
  the REAL factory_spawn but executor command swapped to a /bin/sh stub
  harness that prints tokens + FACTORY_RESULT sentinel (spawn a REAL
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
  d. Malformed FACTORY_RESULT lines (garbage, multiple, mid-log) — parser
     must be last-line-wins, never crash.
  e. store.py under concurrent writers (2 threads x 50 ops) — no
     sqlite 'database is locked' unhandled (WAL or busy_timeout = fix).
  f. citation_ok fuzz: empty, unicode, 10MB body, regex-metachar payloads.
- Every finding = case-law style entry in EXECUTION-CONTEXT.md: pattern,
  file:line, fix or explicit accepted-risk.

Gate: BOTH lanes' suites + the original 37 ALL green together via
~/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q.

## 17. STAGE INSTRUCTIONS (ratified direction 2026-07-13, pre-validation)

Policy stages gain an optional `instructions` field (3-6 lines, a named
bar not a playbook). Instructions live on the stage in a NAMED POLICY
TEMPLATE (git-versioned), never per-task blobs. Empty = model judgment +
seat skill carry the stage. Injected into the reopen-comment / worker
context when the stage activates. Rationale: unguided smart-model review
is the proven failure mode (Nous sweeper adoption; luna F1 'accepted
risk'); the instruction names which bar applies, the seat skill holds
procedure.

## 18. RECIPE SYSTEM (ratified direction 2026-07-13, pre-validation)

A recipe = pure data (YAML in git, versioned; running instances pin their
version): ordered/parallel steps, each {id, executor, model, instructions,
gate: review|approval|mechanical, needs[], concurrency, optional: skip
conditions}. UNIFIES policy stages and org flow: both become steps.
Runtime = the existing kanban dispatcher — each step instantiates as a
kanban task (parent/child links = graph, blockedByIssueIds = ordering,
claims/TTL/circuit-breaker/notify inherited). Recipe engine = template
instantiator + step-advancer hook ONLY. HARD LINE: no custom scheduler,
no DAG engine, no retry semantics beyond kanban's. Domain-general:
proof = dev-pipeline recipe + one Aheli workflow recipe. Kanban's own
workflow_template_id / current_step_key / step_key columns (documented
'v2 workflow routing', currently unused) are the designed-for seams.

## 19. TRIAGE RECIPE-ROUTING + DRAFT VALVE (ratified direction 2026-07-13)

Triage (kanban_decompose pattern: aux-LLM proposes, deterministic code
validates/instantiates) gains recipe SELECTION: per child node, choose
from the recipe LIBRARY + parameterize (skip optional steps, seat/model
overrides). Parent may carry a ship-bearing recipe while a sub-parent
carries build-verify-only — recipes attach per node, compose via existing
parent-gating. CREATION is fenced: when nothing in the library fits,
triage COMPOSES A DRAFT from existing tested step primitives only, files
it as draft attached to a needs_recipe blocked task; a draft NEVER runs
without one approval (operator now; strong-lane architect later once
earned). Approved drafts enter the library versioned. Doctrine: models
choose and judge; tested code executes.

## 20. SHAKEDOWN BACKLOG (gaps ratified for fix, 2026-07-13)

1. F2 run durability: persist _RUNNING pid map in store, rescan on daemon
   start (MUST precede first real load).
2. Budgets/quota-windows ported from PC onto runs data (ceilings, not
   just recording).
3. Labels sidecar (task_labels) — Nous-aligned P0-P4 on factory tasks.
4. Approval-stage chat notification: ping operator when an approval stage
   becomes READY (not only at terminal states).
5. Origin fingerprints — lands WITH recipes (dup-suppression for
   recipe-churned tasks), not after.
