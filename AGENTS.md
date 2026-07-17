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
├── environments.py   # SF-8: runtime manifest + materialization + app sessions
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
- Recipe instances persist their trusted `base_sha`; required v2 inputs from
  an older base block visibly as `artifact_stale` and need a fresh activation.
- Sealed artifact bytes publish through a same-directory fsynced temp file and
  atomic rename. Validation rejection is terminal; interrupted sealing remains
  retryable and must verify or safely replace any torn deterministic target.

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
- Worker ownership is database-first: persist the run and acquire its
  `worker_slot` lease before spawn, then bind PID + OS start token. On restart,
  adopt only an exact token match; a live-looking reused PID is a crashed run.
- Missing executor usage is `NULL`, never zero. Cost rollups expose known and
  unknown run counts separately; admission remains backed by non-refundable
  allowance charges from validated daemon configuration.
- Reap-driven kanban transitions use `action_intents`. A failed complete/block
  must leave a retryable attempt visible; direct best-effort board writes are
  not an acceptable recovery path.
- Environment sessions (`environments.py`, SF-8): the runtime manifest
  (`.shipfactory/runtime.yaml`) and every script it references are read from
  the trusted base commit by git blob SHA and materialized into a
  Factory-owned scratch root before exec — the candidate workspace is never
  consulted for script bytes, so a candidate that edits or symlinks its own
  bootstrap/app-start script after validation has no effect (finding #32).
  Materialization and app-up both spawn as `start_new_session=True` process
  groups reaped across daemon ticks, mirroring the A1 worker-slot pattern:
  DB row inserted (state `materializing`/`starting`, no pid) *before* Popen,
  pid+OS-start-token attached only after spawn succeeds, so a daemon that
  dies mid-spawn leaves an unambiguous pid-less row.
- A daemon restart never optimistically resumes a mid-flight materialization
  (bootstrap or seed) — it kills the child only if the OS start token still
  matches (never a blind PID kill) and marks it `failed`, forcing a fresh
  content-addressed rebuild on next demand. App sessions are adopted instead
  (state carries over, healthcheck contract re-derived from the pinned
  manifest, never trusted from a stale DB snapshot) because losing a healthy
  app on every daemon bounce would be needlessly disruptive; a token
  mismatch still crashes the row and releases its port lease rather than
  guessing.
- Ports are leased through the same A1 `resource_leases` table (`kind='port'`)
  rather than a parallel table: the specific port number lives in
  `metadata_json`, and the scan-for-a-free-port + insert happens inside the
  same `BEGIN IMMEDIATE` writer transaction that already serializes
  concurrent lease acquisition, so two sessions racing for one port range
  never double-bind.
- Testing adoption-after-restart within one pytest process is not the same
  as a real daemon restart: a genuinely restarted daemon's old children get
  reparented to init, which reaps zombies immediately, but a test that just
  clears an in-memory dict is still the process's real OS parent — a killed
  child sits as a zombie that still answers `ps`/`psutil` identity probes
  with its original (still-matching) start token. Tests simulating this
  must opportunistically `os.waitpid(-1, os.WNOHANG)` in their poll loop,
  or they'll see a supposedly-dead process reported as still alive.
- An exploration reference's `generated` classification was unverified —
  it could relabel any real, hand-authored tracked file (e.g. a test about
  to be deleted) with zero corroboration, since only `existing`/`proposed`
  statuses had required-field or hash checks. `generated` now requires an
  honest `git_blob_sha` whenever its declared path resolves at `base_sha`;
  a path genuinely absent at `base_sha` (a not-yet-built output) still needs
  no corroboration (finding #33, SF-7 adversarial lane).
- `access_mode: readonly` on a v2 recipe step (`explore`, `spec-attack`,
  `plan-attack` in dev-pipeline@5) was validated for shape by the loader but
  never enforced anywhere — every executor (codex, claude, hermes) ran with
  full workspace-write regardless. Prompt wording is not a security
  boundary; the OS is. `shipfactory_spawn` now chmods a readonly step's
  workspace non-writable (dirs `0o550`, files `0o440`) before exec, leaving
  only `.shipfactory-output/` writable so the step can still seal its
  result or emit a verdict (finding #34, SF-7 adversarial lane).
- Finding #34's first cut was itself fail-open and incomplete: a DB/JSON
  error resolving `access_mode` silently returned "no enforcement needed"
  instead of blocking the spawn, and the Hermes executor branch never
  called the enforcement function at all. `_step_access_mode` now raises
  `AccessModeResolutionError` on any ambiguous lookup — `shipfactory_spawn`
  lets that abort the spawn rather than run unprotected — and the
  enforcement call moved above the executor branch so codex/claude/hermes
  are covered identically. The chmod mechanism itself is still same-UID
  bypassable (`chmod u+w` before writing restores the worker's own access);
  this is now recorded honestly as `access_enforcement_level="advisory"` on
  the run row (never `"enforced"`) — the SF-8 truthful-labeling pattern
  (see `_apply_network_policy`, finding #7) applied to the filesystem
  boundary (finding #1, cross-lab review of the SF-7 adversarial lane).
- `_read_candidate` read a candidate artifact's bytes with no protection
  against the file changing mid-read — a torn read spanning two writes
  could produce a hybrid document that still parses as valid JSON and
  passes schema validation (nothing hash-binds unrelated fields like
  `unknowns` to `intent_sha256`), sealing as if it were one coherent
  snapshot. It now re-`fstat`s the open fd after the read loop and rejects
  with `"modified while being read"` if mtime/ctime/size differ from the
  pre-read `fstat` — a real, deterministic TOCTOU guard, not a hope that a
  racing writer never lands in the window (finding #2, cross-lab review of
  the SF-7 adversarial lane).
- `_validate_exploration_repository` hash-verified an `existing` reference's
  cited bytes but never checked a `kind: symbol` reference's claimed
  identity against them — a byte-perfect hash of `login`'s real text could
  be dishonestly labeled `revoke_all_sessions` (hallucinated) or `lοgin`
  (Greek omicron homoglyph, byte-distinct from the real name) and would
  seal. §2.2.5 requires a symbol claim to resolve to a definition or call
  site, not merely name SOME real span; a `kind: symbol` reference's `id`
  must now appear, verbatim, as its own token in the cited text (finding
  #3, cross-lab review of the SF-7 adversarial lane).
- Four SF-7 adversarial tests exercised their named attack only in name:
  the backticked-command test never ran a worker (checking the rendered
  task body alone proves nothing about what executes); the repository-
  directive test typed the injection phrase into the test's own document
  instead of a real committed file; the decoy-plan test wrote fake plan
  JSON to a stray file the pipeline never reads instead of the actual
  request/issue-body channel; the hidden-test-removal test asserted only
  an exploration reference, never a plan. All four now construct the real
  attack: a genuine spawned subprocess receiving the backticks only via
  stdin, a real git-committed file cited with a normal hash-verified
  reference, the decoy embedded in the `request` parameter that flows
  into `${request}` substitution, and a real plan node targeting the
  hidden file alongside the exploration deception (finding #4, cross-lab
  review of the SF-7 adversarial lane).
- Git SHA equality cannot identify the worktree that produced a candidate.
  Verification actions now carry the exact producer task, recipe activation,
  durable run id, and workspace; migration 13 stores activation on `runs` and
  the successful `producer_run_id` on `recipe_steps`. Missing, older, foreign,
  or path-mismatched producer identities fail closed. App reuse is separately
  bound to environment base/candidate SHA and workspace, the live PID start
  token, and a no-cache `/.shipfactory/identity` probe for the current
  instance/head before a healthy row is trusted (finding #35, verification
  adversarial lane).
- Capture is part of the production case loop, not a post-hoc helper. The
  trusted browser subprocess emits pre-stamped SFEV containers; the parent
  never gives attacker bytes a fresh identity. Kind, attempt, instance, head,
  bundle, case, timestamp, payload hash, and payload length are checked before
  an item is inserted, cumulative evidence budgets include captures, and any
  copied/truncated/mis-redacted or uncertain binary capture blocks sealing.
  Capture subprocesses use the same bounded supervised-sidecar lifecycle as
  real browser evidence collection (finding #36, verification adversarial
  lane).
- V2 review tasks are activated with Factory-opened exact inputs: verified
  sealed spec/plan bytes, the producer task+activation+run and binary diff,
  every transitive sealed evidence-bundle byte sequence, and prior activation
  history. Approval reopens the same inputs, traverses verification through
  intermediate reviews, rechecks the candidate worktree, and verifies the
  task's Factory-generated input digest; model-written hash testimony is not
  an evidence boundary. Independence is enforced at executor/provider-family
  level across profiles and models, and missing or invalid seat configuration
  blocks approval (finding #37, verification adversarial lane).
- Surface selection is mandatory. The advancer combines changed paths with
  sealed spec/plan path claims, applies UI→browser, API→api, migration→rollback,
  unknown→stricter, and passes that floor into the production action. Profiles
  must declare a surface at or above it and the manifest must actually execute
  the corresponding browser/API/rollback/protected behavior; model risk may
  only raise the floor. Verification-control-plane touches are recorded in the
  sealed payload (finding #38, verification adversarial lane).
- HAR and structured trace redaction parses JSON recursively and is independent
  of object key order; authorization/cookie header pairs and nested cookie
  objects are replaced while the output remains valid JSON. The production
  parent independently confirms the runner's redaction claim. Non-JSON/binary
  traces are never replacement-decoded or rewritten: without provider-backed
  structured redaction they remain byte-identical, are marked `uncertain`, and
  block sealing (finding #39, verification adversarial lane).
- Bundle verification compares every sealed security field with its DB row:
  instance/step/activation, input revision, base/head/tree, manifest,
  environment, workspace producer identity, required surface, redaction,
  phase-B eligibility, state/reason, full case attempts, and the complete item
  manifest. Prior failed/ineligible activations remain in
  `prior_activation_failures` and keep later activations ineligible; neither DB
  drift nor a freshly re-hashed stale item can reset history (finding #40,
  verification adversarial lane).
- `pytest_summary` no longer parses candidate stdout. It accepts only an actual
  pytest argv instrumented with the runner-owned structured-evidence plugin and
  nonce, a zero pytest exit status, real collected/passed counts at or above
  `min_passed`, and zero failures/errors; fabricated wrappers, no-tests,
  all-deselected, and mixed pass/fail runs fail closed (finding #41,
  verification adversarial lane).
- A pytest-shaped argv is not enough to trust its interpreter. A candidate could
  commit `./python3` or a `./pytest` shebang chain, inherit the evidence path and
  nonce, and forge the runner-owned JSON. Pytest launchers and their resolved
  targets must both be executable, absolute, and outside the candidate
  workspace; preserve an external virtualenv launcher path rather than resolving
  away its `pyvenv.cfg` context (finding #43, verification rereview).
- Normal, error, and timeout cleanup retain the leader as a waitable process
  until token-fenced group cleanup; there is no raw post-reap `killpg`. A
  process-tree tracker records PID/start-token/PGID identities and also stamps a
  runner-owned supervision nonce so detached/reparented sessions can be found
  on hosts that permit process-scope enumeration. Browser capture sidecars use
  deterministic readiness, token-fenced SIGTERM, bounded SIGKILL escalation,
  and descendant cleanup on every exit path. An incomplete process-scope scan
  fails closed as `infrastructure_error` (including transient macOS psutil
  `proc_environ` failures) rather than allowing a case to pass with uncertain
  cleanup. Browser children retain a private `HOME`; only the browser cache
  resolved through the selected Playwright interpreter is exposed, so real
  browser execution works without restoring operator-home access (finding #42,
  verification adversarial lane).
- Migration coverage cannot be inferred from the word `rollback` in a case id or
  argv. The protected manifest must declare separate `migration_down` and
  `migration_up` command behaviors, both requiring exit code zero, using the
  same non-trivial migration tool, and each direction must be a bare positional
  primary subcommand (`argv[1]`, or `argv[2]` for an exactly named Python
  executable running `<script>`). Option flags such as `--rollback`, scanning
  every argument for `--reason=rollback-please`, and broad `python*` executable
  matching are not behavioral boundaries. Candidate-only declarations never
  satisfy the floor (finding #44, verification rereview).
- Review-input binding has both focused blocker tests and a public-path v2
  `reconcile()` regression. The latter completes a real review task with an
  approve verdict and proves an unbound task blocks the step and instance with
  `review_inputs_not_bound`; testing only `_review_approval_blocker` would miss
  a future call-site bypass (finding #45, verification rereview).
- Build workers do not own Git identity. Codex workspace-write and Claude
  default permissions cannot reliably write a linked worktree's shared Git
  metadata, and expanding model permissions would enlarge the trust boundary.
  The reap/sealing path now validates exact dirty paths against the sealed plan
  and task-spec exclusions, rejects symlinks/conflicts/preexisting model
  commits, creates one hook-disabled commit with the canonical public author,
  and writes the daemon-derived change-set. An exact existing Factory commit is
  the only accepted retry state after a crash between commit and seal (finding
  #46, dev-pipeline@6 integration review).
- An app request's expected recipe instance and candidate head are durable
  session identity, not data to infer from a request-key convention. Migration
  14 stores both values; app startup overwrites the reserved
  `SHIPFACTORY_INSTANCE_ID`/`SHIPFACTORY_HEAD_SHA` environment variables from
  those trusted columns, and reuse plus the live root identity probe recheck the
  persisted tuple before evidence can run (finding #47, dev-pipeline@6
  integration review).
- Claude's ambient global MCP registry is not a valid Factory tool boundary: a
  malformed or oversized global tool definition can crash a review before it
  starts. Every Claude executor command uses `--strict-mcp-config` while keeping
  default permissions; bypass mode is never an availability fix (finding #48,
  dev-pipeline@6 integration review).
- Recording a gate decision's policy hash is insufficient unless queued event
  consumption compares it with `current_binding()` again. Human-gate policy
  identity remains the immutable recipe hash in this phase, and any recorded
  mismatch is consumed as a stale decision while the approval gate remains
  waiting (finding #49, dev-pipeline@6 integration review).
- A review story binds the SHA-256 of its exact declared change-set alongside
  spec, plan, and evidence. Production validation resolves the story step's
  declared inputs and activation revision instead of selecting instance-wide
  "latest" rows, then reopens the exact producer workspace. Canonical story
  bytes remain unescaped when sealed—even paths containing HTML metacharacters;
  escaping is an API/UI projection only (finding #50, dev-pipeline@6 integration
  review).
- Approval data is useful only when it reaches the operator surface. Waiting
  gates and instance detail now carry the exact bound review-story projection,
  and the dashboard renders its changes, paths, evidence, omissions, and
  residual risks exclusively as React text children—never executable HTML
  (finding #51, dev-pipeline@6 integration review).
- A published recipe's live configuration contract includes deterministic
  verification profiles as well as agent seats and token profiles. Loader and
  startup validation now fail closed on an unknown verification profile; @6 is
  covered against its exact seat/profile names, and both sequential production
  reviewers must each be provider-family independent from the builder. Requiring
  the two reviewers to use different provider families from one another would
  reject the ratified verifier/architect configuration without strengthening
  the builder-review trust boundary (finding #52, dev-pipeline@6 integration
  review).
- Real-browser adversarial controls need a runtime budget large enough to
  reach their intended oracle under full-suite process load. A ten-second
  generic unit-driver budget let Chromium cold-start latency turn deterministic
  backend/reload failures into `test_timeout`; the real-browser control now
  keeps its fail-closed assertion while using a bounded 30-second budget so it
  proves `test_failed` for the named attack rather than host scheduling noise
  (finding #53, dev-pipeline@6 cutover).
- Asynchronous verification tests must measure the concurrency invariant, not
  scheduler luck. The action-level poll budget now covers the sequential
  protected-plus-candidate case set without changing either case's production
  timeout, and the non-blocking regression proves that the fast action seals
  while the deliberately slow runner process is still live instead of requiring
  `Popen` bookkeeping to finish within one wall-clock second on a loaded host
  (finding #54, dev-pipeline@6 cutover).
- Public Git author and message metadata are descriptive, never proof that a
  build commit belongs to Factory. Before moving `HEAD`, finalization now stages
  only validated paths, writes the tree and deterministic public commit object
  with hook-disabled plumbing, and durably records the exact commit/tree/base,
  run, instance/step/activation, resolved workspace, message, and timestamps in
  a non-claimable `action_intents` record. The finalizer alone performs the
  compare-and-swap `update-ref`, then marks the journal row terminal; retries
  accept only that persisted SHA and context, so forged public metadata and
  cross-run/worktree reuse fail closed. The final canonical diff also rechecks
  task-spec forbidden paths, including rename sources, on every retry
  (finding #55, dev-pipeline@6 rework).
- Persisted recipe policy bytes are not authoritative merely because their
  stored hashes are unchanged. Every active instance load now canonicalizes
  and hashes `normalized_yaml`, requiring equality with both
  `recipe_versions.hash` and `recipe_instances.recipe_hash`; decision enqueue
  and daemon application use that same transaction-bound loader and discard
  queued decisions when policy bytes drift (finding #56).
- Review provider independence must be derived from the exact successful
  durable builder and reviewer runs, bound to their recipe activation and
  kanban task, rather than mutable seat configuration. A post-spawn seat edit
  cannot launder collusion or create a false provider identity; missing,
  stale, non-successful, ambiguous, or unknown run identity blocks approval
  (finding #57, dev-pipeline@6 review-run identity lane).
- Dirty-tree and staged-tree path comparisons must use the same rename
  representation. Pre-stage validation names both the source and destination;
  staged `--name-only` must therefore use `--no-renames` before set comparison,
  while the final canonical diff remains rename-aware and binds
  `previous_path`. Otherwise legitimate approved renames fail before the
  forbidden rename-source boundary can run (finding #58, dev-pipeline@6 final
  review proof gap).
- Legacy execution-policy reopen is one ownership transition: status, completed
  timestamp, next-stage assignee, and reassignment failure-streak reset update
  in the same kanban write transaction. Assigning through the CLI while the
  task is still `done` and reopening status afterward can return `ready` under
  the old worker, so both the update count and emitted ownership event are
  checked (finding #59, dev-pipeline@6 exact-gate stability pass).
- Recipe immutability must survive loader namespace evolution without rewriting
  published rows. Historical v1 rows use `factory.recipe/v1` and
  `FACTORY_VERDICT`; compatibility may alias only those exact tokens in a
  temporary copy, verify the row's own stored hash, revalidate both policies,
  and require exact current semantic equality. "Standalone" excludes letters,
  digits, and underscore on the token's left edge; hash-consistent but
  non-document rows, non-list steps, and non-dict step entries raise a clean
  `RecipeError`. The original normalized bytes and hash remain authoritative;
  every other difference still fails immutable (finding #60,
  dev-pipeline@6 live publication cutover).
- A v2 worker cannot produce a declared artifact if its task prompt omits the
  exact output path and schema. Factory appends an explicit output contract to
  every v2 agent/review task with outputs, states that chat prose is not an
  artifact, and separately tells build workers that `change-set` is
  Factory-generated and must not be written by the model (finding #61,
  dev-pipeline@6 live shakedown).
- A schema identifier is not a schema contract. Workers cannot infer private
  required fields or nested shapes from `shipfactory.*` names, so each
  worker-authored output now includes a complete placeholder JSON template and
  field rules in the task body. Prompt and validator top-level/nested key sets
  share one source to prevent documentation drift; semantic seal rules are
  stated alongside the shape; and exact template placeholders are derived into
  a recursive validator guard, because prompt wording is not an enforcement
  boundary. Unsupported worker schemas fail activation instead of eliciting
  invalid guesswork (finding #62, dev-pipeline@6 live shakedown).
- A review worker cannot emit a parseable verdict when its task merely names
  the `SHIPFACTORY_VERDICT` sentinel without exposing the JSON contract. The
  v2 review task body now includes parser-valid approve/request examples,
  derives the exact allowed upstream `target_step` values (preferring declared
  agent-task inputs and binding change-set reviews to the builder), states the
  line-citation rule, and resolves the executor protocol ordering: verdict on
  one physical line immediately before the mandatory `SHIPFACTORY_RESULT`
  line. A review with no valid rework target fails activation instead of
  eliciting unparseable prose (finding #63, dev-pipeline@6 live shakedown).

## Conventions

- Git author: `Abhinav Bansal <abhibansal-sg@users.noreply.github.com>`.
  No AI co-author trailers. Public repo — no secrets, tokens, or private
  paths in commits; screenshots/evidence must be scrubbed before adding.
- Findings get numbers (#22–#63 so far). When you fix one: commit message
  cites it, and the lesson lands in this file **in the same run**.
- All tests green before claiming done. `python -m pytest tests/ -q`.
