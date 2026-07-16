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
- `assert_commit_binding` proved a workspace's git identity (clean, HEAD,
  tree) but never proved that workspace was the *specific* one shipfactory
  actually spawned for the candidate — two different worktrees checked out
  from the same repo naturally share identical HEAD/tree SHAs, so a wrong-
  worktree bug or swap would pass every existing check. `run_action` now
  cross-checks an opt-in `workspace_owner_task_id` against the workspace
  shipfactory's own `runs` table recorded for that task's real spawn,
  independent of git content (finding #35, verification adversarial lane).
- Evidence items were only ever integrity-checked by whole-file hash against
  their own DB row — sufficient to catch tampering, but not to catch a
  *legitimately re-hashed* copy of stale evidence (an old video/trace
  wholesale-copied into a fresh bundle's directory, its own sha256 freshly
  and honestly computed on those stale bytes). Captured evidence
  (video/trace/screenshot/HAR) now goes through a runner-owned container
  format (`build_capture_container`) that stamps instance/head/bundle/case
  identity *inside* the sealed bytes, stamped by the trusted runner scope
  only — never from driver/case output — and `verify_evidence_bundle` checks
  that stamped identity against the bundle's own row, and the header's
  declared payload hash/length against the actual trailing bytes (catches a
  capture pipeline that crashed mid-write, header committed, body
  truncated). Binary captures (screenshot/video) cannot be text-scanned for
  secrets, so they are unconditionally `redaction_state="uncertain"` and
  force sealing to `blocked(redaction_failed)` rather than seal silently
  (finding #36, verification adversarial lane).
- The engine had no mechanism binding a review approval to the evidence the
  reviewer was supposed to have opened: `parse_verdict`/`citation_ok` only
  required a file:line-shaped citation somewhere in the body, never the
  sealed bundle's own hash, and nothing re-checked commit binding between
  when verification sealed and when the approval was actually applied.
  `_review_approval_blocker` now re-verifies the upstream verification
  step's sealed bundle live (integrity + `state=="done"`), re-runs
  `assert_commit_binding` against the candidate's own worktree at
  decision-time (catching a candidate that mutates the workspace *after*
  verification but *before* review), and requires the verdict body to
  literally cite the bundle's `bundle_sha256` before an approve is honored.
  The same blocker also now refuses an approval when the reviewing seat
  resolves to the identical (executor, profile, model) as the seat that
  produced the change-set it's reviewing, even under a different seat name
  (`config.reviewer_shares_builder_provider`) — a same-provider "review" is
  not independent just because the seat name differs (finding #37,
  verification adversarial lane).
- §2.4.7's deterministic surface policy (UI→browser, API→api, migration→
  migration, unknown→stricter) did not exist anywhere; verification profile
  selection was a bare recipe-declared string with zero relationship to what
  the diff actually touched. `classify_required_surface` now derives a
  floor from the changed-path set, and `run_verification` enforces it
  opt-in via `profile["surface"]` (backward compatible with every profile
  that hasn't declared one yet) — a model's own risk classification may
  combine with the floor via `max()`, never substitute for or lower it
  (finding #38, verification adversarial lane).
- `verification.control_plane_paths()` (distinct from the SF-8
  `environments.control_plane_paths()`) was defined and exported but never
  called anywhere — nothing recorded whether a candidate's diff actually
  touched the verification manifest or its playwright scripts, which the
  SF-9 brief itself calls "control-plane risk." `run_verification` now
  diffs base_sha..head_sha and embeds `control_plane_touched` in the sealed
  bundle payload so this is auditable (finding #38, verification adversarial
  lane).
- Redaction only pattern-scanned "Header: value"-shaped text. HAR represents
  headers as `{"name": ..., "value": ...}` JSON pairs, so a Cookie/
  Authorization value sitting in a HAR capture sailed through untouched
  even though §2.4.9 explicitly requires cookies/auth headers stripped from
  HAR. Added a JSON-aware pattern for that shape. Separately, the existing
  `token=`/`secret=`-style pattern's stop-character class excluded only
  whitespace/comma/semicolon — inside JSON it happily consumed past the
  closing quote into the next key, corrupting the document. Both are fixed
  in `_SECRET_PATTERNS` (finding #39, verification adversarial lane).
- `phase_b_eligible` ("autonomous graduation eligible") was computed purely
  per-bundle: a step whose activation 1 genuinely failed and activation 2
  then ran clean showed activation 2 as eligible with zero trace of
  activation 1's failure. Sealing now looks up prior sealed activations of
  the same instance/step, forces `phase_b_eligible=False` if any were
  non-`done` or themselves ineligible, and records exactly which in
  `prior_activation_failures` on the sealed bundle — history stays visible,
  it does not just quietly reset every activation (finding #40,
  verification adversarial lane).
- `output_contains`/`exit_code` oracles cannot see both "did real tests run"
  and "did the exit code lie" at once: a command that prints a fabricated
  `"125 passed"` and exits nonzero, or one that deselects everything and
  exits zero, can satisfy a naively-authored oracle. Added an explicit
  `pytest_summary` oracle type that requires `exit_code==0`, a real parsed
  passed-count ≥ `min_passed`, zero parsed failures/errors, and rejects
  `"no tests ran"`/all-deselected outright — a structural fail-closed
  alternative for manifest authors, not a fix to the other oracle types
  (their honest text/exit-code semantics are unchanged and still footguns
  if misused) (finding #41, verification adversarial lane).
- The command driver only reaped its process group on timeout
  (`_kill_child`), never on normal completion — a case whose own process
  exited 0 could still leave a detached grandchild (a backgrounded "app",
  standing in for what a real browser/child-app pairing would leave
  behind) running past evidence collection. `verified_killpg`'s token
  re-verification is meaningless for a leader that already exited and was
  reaped (there is nothing left to verify against), so the fix is a
  separate best-effort `_reap_process_group` (`os.killpg` directly) called
  right after every successful `communicate()`, not a weakening of
  `verified_killpg` itself. Added `run_supervised_sidecar`/
  `stop_supervised_sidecar` (SIGTERM, then bounded SIGKILL escalation) as
  the general primitive a future real ffmpeg/video-capture wrapper needs so
  a hung sidecar can never block evidence collection (finding #42,
  verification adversarial lane).

## Conventions

- Git author: `Abhinav Bansal <abhibansal-sg@users.noreply.github.com>`.
  No AI co-author trailers. Public repo — no secrets, tokens, or private
  paths in commits; screenshots/evidence must be scrubbed before adding.
- Findings get numbers (#22–#42 so far). When you fix one: commit message
  cites it, and the lesson lands in this file **in the same run**.
- All tests green before claiming done. `python -m pytest tests/ -q`.
