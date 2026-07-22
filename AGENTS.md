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
`hermes_cli.kanban_db`) and file descriptors ≥ 4096. **The Hermes checkout on
`PYTHONPATH` must carry the recipe-kanban APIs** (`create_blocked_task` /
`cancel_subtree`, the `feat-kanban-recipe-apis` line) — the shared Hermes tree
is often switched to another branch for unrelated work, which drops those
functions and hard-fails the startup guard. Use a dedicated git worktree so the
shared tree is never disturbed:

```bash
cd /Volumes/MainData/Developer/products/shipfactory
ulimit -n 4096
# One-time: a worktree pinned to the recipe-API line (shares the repo .git).
#   git -C <hermes-repo> worktree add <WT> feat-kanban-recipe-apis
WT=/Volumes/MainData/Developer/worktrees/hermes-shipfactory-recipe-apis
export PYTHONPATH="$WT"                 # daemon: SHIPFACTORY_HERMES_PATH
export HERMES_MOBILE_PATH="$WT"         # tests: conftest reads this
PY=/Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python

$PY -m shipfactory.cli daemon --board <board>     # the daemon
$PY -m pytest tests/ -q                           # the tests (all must pass)
```

State lives in `$HERMES_HOME/shipfactory/` (`shipfactory.db`, `seats.yaml`,
`runs/`, `telemetry.jsonl`). The kanban boards live in
`$HERMES_HOME/kanban/boards/<board>/kanban.db`.

**Hermes CLI provenance flap (ABH-370):** if `hermes send`/`hermes kanban …`
starts failing with `Hermes Git runtime failed provenance check; run: hermes
update`, do NOT run `hermes update` (it restarts the managed 9119 gateway and
advances the runtime). A dashboard-startup npm step re-dirties the managed
runtime's `package-lock.json`; the clean, non-destructive fix is:
`git -C "$HOME/Library/Application Support/StraitsLab/HermesGit/hermes-agent"
checkout -- package-lock.json`. It is a generated lockfile line, never real code.

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
- Token pools must close mathematically against the activation caps they claim
  to permit. Admission charges the configured 50k allowance per agent/review
  activation, not eventual usage: dev-pipeline@6's happy path required 450k
  while `max_tokens` was 420k, and its planning happy path required 150k from a
  120k pool, so it could never reach approval even with no rework. Immutable
  dev-pipeline@7 makes activation caps the single budget rule: planning 250k,
  build 150k, review 600k, and global 1,000k exactly cover every declared cap;
  tests derive these totals from the recipe rather than duplicating a happy-path
  estimate. Startup re-derives closure for each latest active v2 recipe from the
  live execution profiles' `token_allowance`, so a later config edit fails
  closed instead of silently making a published pipeline impossible (finding
  #64, dev-pipeline@6 live shakedown).
- Never assume execution profiles share one allowance. The first v7 closure
  used 50k for every profile, but live `build` was 75k; the new startup guard
  caught `build: 150k < 3 × 75k` before v7 was published to the live Factory.
  V7 remains immutable in Git. Dev-pipeline@8 supersedes it with build 225k and
  global 1,075k while planning/review remain 250k/600k, and tests use the
  ratified per-profile allowance map plus prove v7 fails under it. Read live
  allowance values before cutting the immutable successor; do not extrapolate
  from charges observed in other pools (finding #65, v7 publication preflight).
- A review's rework destination is Factory policy, not model judgment, when the
  immutable recipe exposes exactly one legal agent-task producer. A reviewer
  may omit `target_step` even after receiving a parser-valid example; Factory
  now derives only that sole target while keeping outcome, body, citation, and
  unknown-field validation strict. Ambiguous targets still fail closed. The
  audited non-approval release path accepts historical
  `invalid request_changes verdict` blocks only through the same derivation,
  so it cannot redirect the rework cone or synthesize review substance
  (finding #66, v8 live shakedown).
- Operator CLI wrappers do not duplicate recoverability policy from the
  advancer. `recipe release` delegates validation to the same enqueue-only
  function used by every caller; otherwise a new recoverable engine reason can
  pass unit tests yet remain impossible to invoke through the live operator
  surface (finding #67, v8 cutover recovery).
- Clean review approval is semantic but bounded: an explicit `approve` verdict
  plus `APPROVE` may state that no unaddressed findings, issues, ambiguities,
  blockers, or gaps remain. Requiring only the literal phrases `clean pass` or
  `no findings` turns a substantively clean durable review into an invalid
  protocol result; generic positive prose still does not satisfy the exemption
  (finding #68, v8 live spec review).
- Canonical build finalization stages only the already validated dirty source
  paths, using literal Git pathspecs. Staging the repository root plus an
  exclusion still makes Git reject an ignored `.shipfactory-output` directory,
  blocking an otherwise valid build before the Factory commit is journaled
  (finding #69, v8 live build finalization).

- A rename must chase every registration surface: the Headframe -> ShipFactory
  rename updated manifest.json and the API prefix but left the dashboard
  bundle registering the tab under the old name, so the operator trust
  surface rendered NO_REGISTER for three days while 17 PRs shipped. Names
  that must agree across files need a drift-guard test, not discipline
  (finding #70, SF-17 lane 1, PR #21).
- Sealed context must be verified at the consumer, not the producer: Hermes
  caps worker-context task bodies at 8KB, silently truncating Factory-inlined
  sealed review inputs while the body-binding check passed against the
  untruncated DB row. Reviewers judged inputs they never saw. Fixed by
  re-delivering the full body in the prompt (codex/claude) and refusing to
  spawn hermes-executor seats on over-cap bodies — the delegated hermes
  worker's capped context is unreachable from the plugin (finding #71,
  SF-17 lane 3, PR #23).

- Contract gates must validate shape, not curate content: the v2 finding-
  location gate's extension allowlist rejected a reviewer's legitimate
  citation of message.txt:1 — the exact file the task changed — turning a
  correct hostile review into a malformed-verdict block on the very first
  live dev-pipeline@9 journey. The gate now requires one concrete
  file.ext:line location with any short alphanumeric extension
  (finding #72, first-light 2026-07-18).

- A hostile gate needs sufficiency conditions, not just necessary ones:
  dev-pipeline@9's attack instructions said "approve only when ..." with no
  clause compelling approval, so a maximally diligent reviewer found the
  next-deeper (always technically correct) gap every round — four live
  first-light journeys died at the spec gate across three request rewrites.
  dev-pipeline@10 gives all four review gates explicit blocker criteria plus
  a MUST-approve-otherwise clause, routing non-blocking observations into
  the approve verdict's summary (finding #73, first-light 2026-07-18).

- Verification trust probes inherit the daemon's PATH, not the pinned
  verification-profile PATH: with bare `python` absent from a stock macOS
  PATH, the pytest trusted-interpreter probe and the Playwright shared-cache
  probe both failed silently, so every browser case died in the isolated
  runner HOME with 'Executable doesn't exist' — the real cause of the v8-era
  test_infrastructure_error blocks. The launcher now exports the venv onto
  PATH (finding #74, first-light-5 2026-07-18).

- Hot-path trust probes must be deterministic: resolving the Playwright
  browser cache by launching a driver subprocess with a 5s timeout
  intermittently lost the race under verification load, silently returning
  None so PLAYWRIGHT_BROWSERS_PATH was never granted and every browser case
  died in the isolated runner HOME. Well-known cache locations are now
  checked directly; the subprocess probe survives only as a fallback
  (finding #75, first-light-8 2026-07-18).

- Environment grants must be made where the environment is constructed:
  fixing the browser-cache resolver (finding #75) changed nothing because the
  v2 verification runner is spawned with a REBUILT scrubbed env and an
  isolated HOME — the grant made at the inner sidecar site never existed in
  the outer child, and inside that child every fallback was structurally
  blind. _runner_env itself now grants PLAYWRIGHT_BROWSERS_PATH, resolved
  from the real HOME in the daemon and inherited explicitly by nested
  children (finding #76, first-light-9 2026-07-18).

- Loop prevention is count-based, never token-based (operator decision,
  2026-07-18): the token-budget system (max_tokens, token_pools,
  token_allowance, board-day token ceiling, budget_charges accounting) is
  being removed wholesale. Infinite loops are bounded by run counts
  (max_activations per instance, step_activation_caps per step) and the
  per-run wall-clock deadline. One subtlety: the activation counter used to
  increment only as a side effect of the token charge; it is now incremented
  unconditionally on admission so the run caps stand alone (finding #77,
  token-budget removal PR 1).

- grok is a first-class executor / provider family (finding #78): the grok
  CLI runs headless via `grok --prompt-file /dev/stdin --output-format json
  -m grok-4.5`, authenticated by its own session (no API key in Factory
  config; ~/.grok permission_mode=always-approve keeps it non-interactive).
  It emits ONE JSON object whose `text` field carries the agent's final
  message (and the sentinel) and a `usage` record — so extract_text/parse_usage
  parse the whole log as one object, not JSONL. grok is a distinct family from
  codex/claude, so a codex builder + grok reviewer satisfies the cross-provider
  law with zero Anthropic.

- An artifact field that must echo a Factory-provided value needs an
  echo-it-verbatim instruction, not a describe-it one: the review-story worker
  computed its own revision_hash (a change-set-style hash) instead of copying
  the input_artifact_set_hash it was handed, blocking one step from the
  approval card. The output contract now says revision_hash and the four
  artifact hashes must be copied VERBATIM from the Factory-opened review
  inputs — do not compute or derive them (finding #79, first-light-13).
- KNOWN OPEN: on a failing browser case the on-failure screenshot (binary,
  un-text-scannable) seals as redaction_state=uncertain and blocks the whole
  bundle with `redaction_failed`, masking the real test failure and denying
  the normal verify-failed rework path. Only bites on a failing build. Fix
  candidate: trust binary captures from the sandboxed verification runner, or
  surface the test failure ahead of the redaction block (finding #80, deferred).
- A read surface that enriches an object and a write surface that consumes it
  must agree on the full field set. `/waiting` attached every gate binding
  (activation, revision_hash, evidence_bundle_hash) via current_binding, and
  the card even rendered `gate.activation`, but the dashboard `decide()` posted
  only `{instance, step, reason}` — so the operator's first real approval click
  422'd on the seven missing binding fields. The bundle's decide() now echoes
  the binding + a fresh client-minted nonce + operator actor/channel, and
  refuses locally when a gate has no revision binding yet. Round-trip and
  3-field-422 tests plus a bundle-guard lock it (finding #81, first-light-14).
- A human gate decision re-verifies the whole evidence bundle by reading every
  sealed item off disk (input_artifacts -> verify_evidence_bundle). `~/.hermes`
  is a SYMLINK (-> /Volumes/MainData/Runtime/Hermes here), so the stored item
  paths and the computed evidence root land in different symlink forms depending
  on the serving process's HERMES_HOME: the daemon/CLI use `~/.hermes` on both
  sides (pass), but the dashboard host resolves the root to `/Volumes/...` while
  item paths stay `~/.hermes` — a lexical `path.relative_to(root)` then raises
  ValueError -> "evidence item <id> path is invalid" and fail-closes EVERY
  browser approve/reject deterministically (first misread as a transient because
  the CLI path passes). Fix: compare fully-resolved real paths
  (`path.resolve().relative_to(root.resolve())`) — symlink-agnostic, and an
  in-root item symlinked to a target OUTSIDE root now fails closed too (stronger
  than the old lexical check). Secondary hardening: `_read_evidence_item` retries
  a genuinely transient lstat/read OSError (bounded 4 x 50ms); hash/size mismatch
  stays fatal. NB: the fix only reaches a process that reloads shipfactory — the
  running Hermes host serves the OLD check until it restarts; CLI decisions run
  fresh code immediately (finding #82, first-light-14).
- OpenCode is a first-class executor family, not a model alias on another
  harness. It runs headlessly with the prompt on stdin via `opencode run
  --pure --format json --agent build --dir <workspace>`, selects a provider
  model such as `zai-coding-plan/glm-5.2`, and maps the seat reasoning value
  to OpenCode's model variant. Text comes from JSONL `text` events; usage is
  summed across `step_finish.part.tokens` records. `--pure` excludes ambient
  plugins, and Factory deliberately omits `--auto` so an unexpected permission
  request fails closed instead of widening the worker boundary (finding #83,
  self-build GLM seat preparation).
- A ShipFactory seat name is NOT a Hermes profile name. Hermes'
  `dispatch_once` buckets a ready task `nonspawnable` when its assignee is not
  a `~/.hermes/profiles/<name>/` directory (via `profile_exists`), so a
  step-granular seat like `spec-author` never spawned and the first self-build
  stalled. Fix (finding #84): `daemon.tick` runs `rescue_nonspawnable_seats`
  after `dispatch_once` — for a ready task Hermes bucketed whose assignee is a
  ShipFactory non-hermes seat, we claim + `resolve_workspace` +
  `set_workspace_path` + spawn through our own `spawn_fn`. It is race-free
  (Hermes appends to `skipped_nonspawnable` BEFORE it would `claim_task`, so we
  are the sole claimant), respects `max_concurrent`, and carves out
  hermes-executor seats (whose `hermes -p <assignee>` argv genuinely needs the
  profile) and unknown assignees (stay gated). Zero Hermes core modification.
- Seat data model (finding #84, Paperclip-derived): a seat is `executor`
  (adapter) + an adapter-owned `config` blob whose keys each executor validates
  via its `CONFIG_KEYS` (so a knob the harness ignores is a load error, not a
  silent lie) + top-level `model`/`reasoning` (finding #12 invariant, never
  buried) + a forward-compat `skills` tuple (delivery deferred). `profile` is
  required only for a hermes seat. `_normalize_seat` drops unknown YAML keys
  with a warning (rollback-safe). Migration is a no-op: existing seats load
  byte-identical with `config={}`. Deferred: `reports_to`/`validate_acyclic`
  removal (6 live consumers) and the `config`→argv translation (emit a flag
  only when the key is explicitly present, to keep `config={}` byte-identical).
- The nonspawnable rescue must release its claim on spawn failure. A
  transient sqlite `disk I/O error` (stale-WAL class) inside the spawn path
  stranded a claimed task as phantom-running with no run row — the daemon saw
  "running", Hermes saw nothing. Fix (finding #85): wrap
  `resolve_workspace`/spawn in try/except and `reclaim_task(conn, task_id,
  reason=...)` on any failure, so the next tick retries instead of stalling.
- The codex sandbox (`codex exec -s workspace-write`) DENIES binding TCP
  ports (verified: `socket.bind` → `PermissionError`). A builder therefore can
  never pass server-binding tests (`test_environment_sessions` etc.), and a
  step contract demanding "full suite green" from inside the sandbox is
  unsatisfiable — the worker retries forever and blocks. Rule (finding #86):
  builders run focused, non-binding tests only; the full-suite gate belongs to
  deterministic verify-runtime, which runs OUTSIDE the worker sandbox.
- A verification app script must serve `GET /.shipfactory/identity` returning
  `{"instance_id": $SHIPFACTORY_INSTANCE_ID, "head_sha": $SHIPFACTORY_HEAD_SHA}`
  (env injected by `environments.py` app-session start). The commit-binding
  probe (`verification.py` `_probe_live_identity`) hits that path and fails
  closed on 404 — `environment_identity_mismatch` — so a health-only app like
  the first `sf-app.sh` blocks every runtime verification. Boot-testing
  `/healthz` is NOT enough; boot-test the identity route too (finding #87,
  self-build-r7c).
- The verification clean-check must carve out `.shipfactory-output/`. Every
  worker worktree carries that untracked directory BY DESIGN (spawn creates
  it; the artifact contract writes into it), and the sealing layer already
  excludes it from the canonical diff + dirty-path collection — but
  `verification.py _repository_identity` did a raw `git status --porcelain`,
  so every runtime verification of a worker-built candidate failed
  "workspace is not clean". Fix (finding #88): filter exactly that directory
  from the status lines; any other dirt (including prefix cousins like
  `.shipfactory-output-x`) still fails closed, and the tree binding is
  unaffected (`write-tree` reads the index only). NB: #87 masked #88 — the
  identity probe ran BEFORE the clean-check, so r7c never reached it;
  sequential harness bugs unmask one at a time, so after fixing one,
  pre-flight the NEXT gate locally instead of discovering it by flight.

## Conventions

- Git author: `Abhinav Bansal <abhibansal-sg@users.noreply.github.com>`.
  No AI co-author trailers. Public repo — no secrets, tokens, or private
  paths in commits; screenshots/evidence must be scrubbed before adding.
- Findings get numbers (#22–#88 so far). When you fix one: commit message
  cites it, and the lesson lands in this file **in the same run**.
- All tests green before claiming done. `python -m pytest tests/ -q`.
