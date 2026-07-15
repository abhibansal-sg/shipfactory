# External Program Review — Autonomy Program (2026-07-15)

> Independent principal-engineer review of the autonomy program brief
> (`docs/briefs/2026-07-15-autonomy-program.md`), commissioned before any
> build lane dispatch. Reviewed at main commit 3bd2691 (since restructured).
> Verdict: NO-GO as drafted; GO after a control-plane hardening phase (A0/A1).
> The build order in §5 of this document is the ratified program plan.
> "The reference product" below = a commercial cloud autopilot-for-software-
> engineering product studied as the capability benchmark.

External Principal-Engineer Review

**Review target:** `main` at commit `3bd26915ed74d4849670daa5d677ee362867190e`, the commit adding the autonomy program brief. 

**Decision: NO-GO on the program as written. GO on a revised program after a control-plane hardening phase.**

The six the reference product bottlenecks are a useful **audit lens**. They are not the right ShipFactory **work breakdown**. The brief copies the visible product lifecycle while omitting the machinery that makes autonomy survivable: trusted revision identity, recoverable external actions, resource admission, security boundaries, rollback, and intake governance.

The most dangerous sentence in the brief is not “auto-deploy.” It is the claim that one clean cycle per workstream is enough to enable Phase B. One clean cycle is a smoke test. It says almost nothing about race conditions, drift, prompt injection, stale evidence, moving-main behavior, or rollback.

---

# 1. PROGRAM VERDICT

## 1.1 The six-workstream decomposition

### The diagnosis is right

the reference product is right that autonomous software delivery fails at the seams between:

1. executable environments,
2. planning,
3. orchestration,
4. testing,
5. review,
6. merge and release.

Its manifesto correctly puts the machine first, emphasizes planning before coding, gives each subtask a fresh context, uses cross-lab review, runs real user flows, and serializes work against a moving main branch. citeturn200742view0

ShipFactory already has unusually strong foundations in recipe immutability, revision invalidation, operator-owned gates, budget admission, reconciliation, and scar-tissue-driven regression tests. The laws around permanently spent event keys, advancer-only step writes, immutable published recipes, and upstream-only review targets are the correct kind of laws.  

### The decomposition is wrong

The six labels can remain. The six workstreams should not.

| Current workstream | Verdict | Required change |
|---|---|---|
| WS1 machine | Keep, but split | Separate deterministic environment provisioning from long-lived app-session management. A script is not a snapshot. |
| WS2 planning | Keep, but split | First build typed artifact and revision-binding infrastructure; then build explore/spec/plan loops. Kill the backtick grep. |
| WS3 fan-out | Reject as designed | Do not mutate a running instance into an arbitrary graph. Materialize deterministic child recipe instances using the existing selector/collector pattern. |
| WS4 watched testing | Keep, but redesign | Add a non-model verification primitive and daemon-sealed evidence. Video is supplementary, not authoritative. |
| WS5 diff-as-story | Kill as a standalone workstream | Fold it into WS4’s evidence/review presentation layer. It has little independent engine surface. |
| WS6 merge/deploy/intake | Split into three workstreams | Release queue, deployment controller, and autonomous intake have different state machines, risk, rollback, and trust boundaries. |
| Missing WS0 | Add before everything | Single-writer enforcement, recoverable action journal, durable process identity, resource governor, migration framework, and trust-domain recording. |
| Autonomy ladder | Make cross-cutting | Auto-approve, auto-release, and auto-deploy must be three separately graduated policy switches. |

## 1.2 The order in the brief is not dependency-honest

The brief says WS1 is required for watched testing, then dispatches WS4 before WS1. It also combines watched verification, UI rendering, and post-approval merging into one first lane. That lane would cross the recipe schema, spawn lifecycle, persistence, dashboard API, evidence format, Git state, and release state in one shot. When it fails, you will not know which layer lied.   

The correct broad order is:

> control-plane safety → artifact/revision identity → serial planning → environment sessions → deterministic verification → evidence presentation → serialized release → rollback-capable deployment → bounded fan-out → quarantined intake → autonomy graduation

Parallelism and self-generated demand come last because both are force multipliers. You do not add force multipliers to a control plane with unresolved exactly-once and process-recovery gaps.

## 1.3 Several proposed mechanics are not implementable against the current interfaces

These are not documentation nits. They are build blockers.

### WS4’s proposed verdict payload is currently illegal

The brief says evidence paths should be added to the verdict JSON. `parse_verdict()` explicitly rejects every unknown approval field and every unknown request-changes field. An `evidence` property would cause a valid-looking verification result to block as an invalid verdict. Evidence therefore needs a separate typed artifact store, not extra free-form verdict fields. `shipfactory/recipes/primitives.py:16–24`. 

### WS1’s `app_up` field is currently illegal

The v1 loader requires exact step keys and exact `agent_task`/`review_gate` parameters. The only accepted agent parameters are `seat`, `instructions`, `execution_profile`, and `workspace`. `app_up`, `environment`, `outputs`, or any other new field fails recipe loading. This requires `shipfactory.recipe/v2`; it cannot be smuggled into `@5`. `shipfactory/recipes/loader.py:20–24,90–120`.  

### WS2’s “two review gates” do not produce a draft-attack-revise loop

A review gate emits a verdict. It does not draft or revise a plan. If both proposed gates target `explore`, a rejection reruns exploration, not a plan author. The legal loop requires a separate upstream `plan-draft` `agent_task`, followed by a `plan-attack` `review_gate` that targets `plan-draft`.

The current `dev-pipeline@4` already demonstrates the structural problem: `plan-check` is the first step and is a `review_gate` with no upstream producer. It can approve, but any legal request-changes verdict has nowhere routable to go. `recipes/dev-pipeline@4.yaml:26–59`; the upstream-only law is explicit in `AGENTS.md:74–75`.  

### “Rewrite `${request}`” has no state-model meaning

Instance parameters are bound once and persisted in `parameters_json`. Downstream string substitution continues to use those pinned parameters. Parent handoff can add prose context, but it does not mutate the request or produce an addressable, hash-bound spec. `shipfactory/recipes/instantiate.py:28–45`. 

### WS6’s selector is already wired

The brief calls the selector “unwired.” The daemon already invokes `selector_stage.run_stage()` when selector configuration is enabled. WS6-D3 is therefore not “wire the selector”; it is “create and govern new signal sources feeding an already active selector.” That is a materially different and much riskier task. `shipfactory/daemon.py:58–80`. 

### The merge source is not defined

Factory persistence currently has no authoritative workspace path, base commit, candidate head, tree hash, commit range, or change-set artifact. `output_revision` is merely a monotonically assigned integer over completed producer steps, and `revision_vector()` hashes activation/revision tuples rather than Git objects. There is currently nothing trustworthy for a merge primitive to merge. `shipfactory/store.py:63–117`; `shipfactory/recipes/instantiate.py:100–127`.  

---

# 2. FULL DETAILED SPEC

## 2.0 Mandatory prerequisite: WS0 — control-plane safety kernel

This must land before WS1–WS6. It is not optional cleanup.

The current event table has no lease, attempt count, expected state, or error outcome. `apply_events()` selects all pending events without claiming them, and the CLI directly invokes it after queuing approvals and releases. That means there are at least two advancer execution paths despite the single-writer law. `shipfactory/store.py:100–103`; `shipfactory/cli.py:285–310`; `shipfactory/recipes/advancer.py:693–775`.   

### 2.0.1 Schema migration framework

Stop relying on opportunistic `CREATE TABLE IF NOT EXISTS` plus inline `ALTER TABLE` checks. Add:

```sql
CREATE TABLE schema_migrations (
    version       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    applied_at    TEXT NOT NULL
);
```

Each migration must:

1. run under `BEGIN IMMEDIATE`;
2. verify the prior version;
3. use a content checksum;
4. commit completely or roll back;
5. fail daemon startup if a migration is partially applied or its checksum differs.

### 2.0.2 Advance-event leasing

Extend `advance_events` with:

```sql
lease_owner        TEXT,
lease_until        TEXT,
attempt_count      INTEGER NOT NULL DEFAULT 0,
expected_activation INTEGER,
expected_state     TEXT,
outcome            TEXT,
last_error         TEXT
```

Allowed states:

```text
pending -> leased -> applied
                  -> discarded
                  -> failed
leased  -> pending     only when the lease expires; no row reinsertion
```

Rules:

- An existing key is never deleted.
- An applied, discarded, or failed key is permanently spent.
- A stale event becomes `discarded` with a reason, not silently indistinguishable from a successful application.
- Event claiming is one `BEGIN IMMEDIATE` transaction selecting and updating one event.
- The event records its expected activation and state at enqueue time where applicable.

### 2.0.3 External-action journal

Do not perform Git, kanban, messaging, process, or deployment effects while pretending a Factory transaction makes them atomic.

Add:

```sql
CREATE TABLE action_intents (
    key             TEXT PRIMARY KEY,
    logical_key     TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    instance_id     TEXT,
    step_id         TEXT,
    activation      INTEGER,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    state           TEXT NOT NULL,
    lease_owner     TEXT,
    lease_until     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    result_json     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(logical_key, attempt)
);

CREATE INDEX idx_action_intents_ready
ON action_intents(state, lease_until, created_at);
```

States:

```text
planned -> leased -> succeeded
                  -> retryable_failed
                  -> terminal_failed
                  -> abandoned
```

The recovery rule is the important part:

- The advance event is consumed once it has durably produced the action intent.
- A retry does **not** reinsert or replay the advance-event key.
- A retry inserts a fresh action-intent key with `attempt + 1`.
- Before executing a retry, the action runner probes the target system to determine whether the prior attempt actually succeeded.

Examples:

- Gate completion: inspect the kanban task before calling `complete_task`.
- Root collector: if already `done`, mark the action succeeded.
- Git push: inspect the remote ref.
- Deployment: inspect the active release identifier.
- Notification: use an idempotency token where the transport supports one; otherwise record the duplication risk explicitly.

This is the generalized version of the task-creation idempotency already used by `activate()`.

### 2.0.4 Enforce one daemon

Hold an exclusive `fcntl.flock()` on:

```text
$HERMES_HOME/shipfactory/daemon.lock
```

for the entire daemon lifetime. A second daemon must exit before opening board databases or dispatching.

The lock record should contain PID, process start time, executable path, boards, and daemon version. PID alone is insufficient because PIDs are reused.

CLI and dashboard commands enqueue only. Remove `advancer.apply_events(conn)` from `_recipe_gate()` and `_recipe_release()`. The next daemon tick performs the decision. The existing direct application contradicts both §17 and the repository’s single-writer law. 

### 2.0.5 Resource leases

Add:

```sql
CREATE TABLE resource_leases (
    key               TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    units             INTEGER NOT NULL,
    instance_id       TEXT,
    step_id           TEXT,
    activation        INTEGER,
    state             TEXT NOT NULL,
    lease_until       TEXT,
    metadata_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    released_at       TEXT
);
```

Initial resource kinds:

```text
worker_slot
environment_slot
browser_slot
port
disk_mb
release_lock:<repo>
deploy_lock:<environment>
```

Capacity comes from operator configuration, not model output or recipe text.

### 2.0.6 Immediate adversarial cases

The implementation lane’s tests are not acceptance. A separate lane must test:

- two OS processes racing to claim the same event;
- two daemon launches;
- crash after action intent insertion but before external action;
- crash after external action but before success recording;
- lease expiry while the first process is merely slow;
- root collector completion returning false;
- gate completion after the kanban task is already done;
- a 30-second notification send while another Factory writer attempts a transaction;
- permanent event-key retention through every recovery;
- configured recipe mode with unreadable configuration.

---

## 2.1 WS1 — deterministic environment provisioning and app sessions

### Correction to the brief

“The bootstrap script is the snapshot” is a category error.

A script is a procedure. Its result depends on:

- toolchain versions,
- lockfiles,
- package repositories,
- network availability,
- mutable caches,
- environment variables,
- seed state,
- already-running services,
- OS state.

A reproducible environment requires a **materialization manifest** containing the script hash and all relevant inputs and outputs. the reference product’s snapshot is a materialized machine state; it is not merely a shell script. citeturn200742view0

Do not run the bootstrap synchronously inside `shipfactory_spawn()`. `daemon.tick()` is single-threaded across recipe advancement, reaping, dispatch, health checks, and watchdogs. A slow bootstrap there stops the whole board—and in multi-board mode, potentially every board. `shipfactory/daemon.py:51–105`. 

### 2.1.1 Configuration

Do not invent an unspecified “board metadata key.” There is no board-metadata abstraction in the current Factory configuration. Add a repo-owned runtime manifest plus an operator-owned policy block.

Repository file:

```yaml
# .shipfactory/runtime.yaml
schema: shipfactory.runtime/v1

bootstrap:
  argv: ["scripts/env-bootstrap.sh"]
  tracked_inputs:
    - pyproject.toml
    - uv.lock
    - package-lock.json
  network: deny

app:
  start_argv: ["scripts/app-start.sh", "--port", "${PORT}"]
  healthcheck:
    path: /health
    expected_status: 200
  stop_signal: TERM

seed:
  argv: ["scripts/seed-test-data.sh"]
```

Operator configuration:

```yaml
recipes:
  runtime:
    manifest_path: .shipfactory/runtime.yaml
    port_min: 19000
    port_max: 19031
    max_sessions: 1
    bootstrap_timeout_seconds: 600
    startup_timeout_seconds: 90
    shutdown_timeout_seconds: 15
    max_output_bytes: 10485760
    default_network: deny
```

The runtime manifest is read from the instance’s trusted base commit and pinned by Git blob SHA. A candidate that modifies `.shipfactory/runtime.yaml`, bootstrap scripts, start scripts, verification scripts, or deployment scripts is classified as `control-plane` risk. The modified script must not execute with autonomous privileges in the same cycle that proposes it.

### 2.1.2 Recipe schema

Introduce `shipfactory.recipe/v2`. Do not change v1 interpretation.

For `agent_task` and `review_gate`, add:

```yaml
params:
  seat: verifier
  instructions: ...
  execution_profile: standard
  workspace: worktree
  access_mode: readonly       # readonly | workspace_write
  environment: app           # source | bootstrapped | app
```

`access_mode` is an enforced executor capability, not a prompt. An executor that cannot guarantee `readonly` is ineligible for an explore or review step.

The current v1 specification explicitly deleted readonly because it was unenforceable. Restoring it without enforcement would repeat the same mistake. 

### 2.1.3 Persistence

Extend `runs` with:

```sql
board                   TEXT,
workspace_path          TEXT,
workspace_identity      TEXT,
base_sha                TEXT,
head_sha                TEXT,
log_path                TEXT,
prompt_path             TEXT,
environment_session_id  TEXT,
provider                 TEXT,
resolved_model          TEXT,
executor_version         TEXT,
profile_hash             TEXT,
process_start_token      TEXT
```

Add:

```sql
CREATE TABLE environment_sessions (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    board                 TEXT NOT NULL,
    workspace_path        TEXT NOT NULL,
    workspace_identity    TEXT NOT NULL,
    base_sha              TEXT NOT NULL,
    candidate_sha         TEXT,
    runtime_manifest_sha  TEXT NOT NULL,
    bootstrap_script_sha  TEXT NOT NULL,
    app_script_sha        TEXT,
    state                 TEXT NOT NULL,
    pid                   INTEGER,
    process_start_token   TEXT,
    process_group         INTEGER,
    port                  INTEGER,
    app_url               TEXT,
    health_status         TEXT,
    lease_until           TEXT,
    stdout_path           TEXT,
    stderr_path           TEXT,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    stopped_at            TEXT,
    last_error            TEXT,
    UNIQUE(instance_id, step_id, activation)
);

CREATE TABLE environment_actions (
    key             TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    state           TEXT NOT NULL,
    action_intent_key TEXT NOT NULL,
    exit_code       INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    last_error      TEXT,
    UNIQUE(session_id, kind, attempt)
);
```

### 2.1.4 State machine

```text
requested
  -> provisioning
  -> bootstrapped
  -> starting
  -> healthy
  -> leased
  -> stopping
  -> stopped
```

Failure states:

```text
provisioning -> blocked(bootstrap_failed)
starting     -> blocked(app_start_failed)
starting     -> blocked(healthcheck_failed)
healthy      -> blocked(environment_lost)
*            -> failed(invariant_error)
```

Rules:

- The recipe step remains `ready` until the required environment reaches `healthy`.
- The app process is owned by the environment-session manager, not by the model worker.
- Worker and app have separate process groups.
- If the app dies before the worker starts, the session may restart with a fresh action attempt.
- If the app dies while the worker or verifier is active, kill the worker and block the activation as `environment_lost`. Do not silently reconnect the same activation to a different environment.
- Cancellation stops workers first, app second, then releases the port and resource leases.
- A session may be reused only when its base/candidate SHA, manifest hash, seed identity, and required access mode match exactly.

### 2.1.5 Path and process safety

The daemon validates that every script is:

- a regular file;
- repo-relative;
- not a symlink;
- present in the pinned tree;
- invoked as an argv array, never an interpolated shell string;
- executed with bounded stdout/stderr;
- executed under a timeout and process group;
- unable to escape the dedicated workspace and state roots.

Default app binding is `127.0.0.1`, not all interfaces.

### 2.1.6 Budgets

WS1 consumes no model tokens but still consumes scarce resources. Add profile caps:

```text
max_environment_seconds
max_environment_sessions
max_output_bytes
max_disk_mb
max_processes
```

Do not use the token budget as a proxy for CPU, memory, disk, ports, or file descriptors.

### 2.1.7 Tests the build lane will miss

- Bootstrap exits `0` but health never becomes ready.
- Bootstrap forks a child and exits, leaving the child alive.
- App binds a different port than allocated.
- Port is stolen between reservation and bind.
- PID is reused after daemon restart.
- Runtime script becomes a symlink after validation.
- Candidate modifies its own bootstrap script.
- Bootstrap downloads a different dependency with the same script hash.
- Two sessions race for the same port.
- Daemon dies after app start but before session state commit.
- Cancellation occurs during seeding.
- Log output fills disk.
- App writes outside its workspace.
- App stays healthy but serves the wrong candidate commit.
- Hermes, Codex, and Claude execution paths do not receive equivalent environment treatment.

---

## 2.2 WS2 — explore → spec → adversarial plan

This is the most valuable workstream, but D1 and D3 are wrong as written.

### 2.2.1 Kill the backtick grep

It fails in both directions.

It rejects valid requests because backticks are also used for:

- proposed new paths,
- shell commands,
- configuration values,
- generated names,
- explanatory prose.

It misses hallucinations because a false symbol does not have to be backtick-quoted.

Worse, it conflicts with the stated product goal. A one-line intent is expected to be ungrounded; exploration is supposed to ground it. Rejecting ungrounded intent before exploration prevents the capability the workstream is meant to add.

The replacement is a typed, version-bound exploration artifact.

### 2.2.2 Artifact persistence

Add:

```sql
CREATE TABLE artifacts (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    run_id                INTEGER,
    kind                  TEXT NOT NULL,
    schema_version        INTEGER NOT NULL,
    state                 TEXT NOT NULL,
    candidate_path        TEXT,
    sealed_path           TEXT,
    sha256                TEXT,
    size_bytes            INTEGER,
    producer              TEXT NOT NULL,
    trust_domain          TEXT,
    base_sha              TEXT NOT NULL,
    head_sha              TEXT,
    repo_tree_sha         TEXT NOT NULL,
    validation_error      TEXT,
    created_at            TEXT NOT NULL,
    sealed_at             TEXT,
    UNIQUE(instance_id, step_id, activation, kind)
);

CREATE TABLE artifact_edges (
    parent_artifact_id  TEXT NOT NULL,
    child_artifact_id   TEXT NOT NULL,
    relation            TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
);
```

Add to `recipe_steps`:

```sql
input_artifact_set_hash   TEXT,
output_artifact_set_hash  TEXT
```

A worker writes only to predetermined candidate paths under:

```text
.shipfactory-output/
```

After the process exits, the daemon:

1. opens the expected file without following symlinks;
2. verifies size limits;
3. validates the schema;
4. validates repository references;
5. copies it into Factory-owned storage;
6. hashes the sealed bytes;
7. marks the artifact sealed;
8. computes the output artifact-set hash.

Agents do not provide arbitrary evidence paths in prose.

### 2.2.3 Recipe schema v2

Add exact step keys:

```yaml
- id: explore
  primitive: agent_task
  title: Explore the intent
  needs: []
  optional: false
  inputs: []
  outputs:
    - kind: exploration
      schema: shipfactory.exploration/v1
      path: .shipfactory-output/exploration.json
  params:
    seat: explorer
    instructions: ...
    execution_profile: planning
    workspace: worktree
    access_mode: readonly
    environment: source
```

Every step in v2 has exactly:

```text
id
primitive
title
needs
optional
inputs
outputs
params
```

`inputs` entries have:

```yaml
- from: explore
  kind: exploration
  required: true
```

`outputs` entries have:

```yaml
- kind: task-spec
  schema: shipfactory.task-spec/v1
  path: .shipfactory-output/spec.json
```

### 2.2.4 Required pipeline

```text
explore
  -> spec-draft
  -> spec-attack
  -> plan-draft
  -> plan-attack
  -> build
```

Exact roles:

| Step | Primitive | Output/decision | Legal rejection target |
|---|---|---|---|
| `explore` | agent_task | exploration manifest | N/A |
| `spec-draft` | agent_task | task specification | N/A |
| `spec-attack` | review_gate | approve/request changes | `spec-draft` |
| `plan-draft` | agent_task | plan of plans | N/A |
| `plan-attack` | review_gate | approve/request changes | `plan-draft` |
| `build` | agent_task | change set | N/A |

This fixes the current first-step review problem and makes the review loop target the artifact author rather than the explorer.

### 2.2.5 Exploration artifact

`shipfactory.exploration/v1` requires:

```json
{
  "schema": "shipfactory.exploration/v1",
  "intent_sha256": "...",
  "base_sha": "...",
  "repo_tree_sha": "...",
  "references": [
    {
      "id": "ref-1",
      "kind": "path",
      "status": "existing",
      "path": "shipfactory/recipes/advancer.py",
      "git_blob_sha": "...",
      "start_line": 690,
      "end_line": 775,
      "text_sha256": "..."
    }
  ],
  "direct_callers": [],
  "constraints": [],
  "untrusted_directives": [],
  "unknowns": []
}
```

Reference statuses are:

```text
existing
proposed
generated
external
```

Rules:

- `existing` paths must exist at `base_sha`.
- `proposed` paths are allowed; they require a reason and intended parent directory.
- Line references include the Git blob hash and text hash.
- Symbol claims must point to at least one resolvable definition or call site.
- Generated and vendor directories are identified explicitly.
- Repository content that attempts to alter tool policy is recorded under `untrusted_directives`.

### 2.2.6 Task-spec artifact

`shipfactory.task-spec/v1` requires:

```text
intent_artifact_id
problem
non_goals
requirements[]
target_files[]
forbidden_paths[]
risk_tags[]
acceptance_cases[]
rollback_notes
assumptions[]
clarifications[]
```

Each requirement has a stable ID:

```json
{
  "id": "REQ-3",
  "behavior": "A duplicate gate decision must not complete a new activation.",
  "oracle": "The second decision is rejected as stale.",
  "risk": "control-plane"
}
```

`clarifications` must be empty before `spec-attack` can approve. Do not hide unresolved questions in assumptions.

### 2.2.7 Plan artifact

`shipfactory.plan/v1` requires:

```json
{
  "schema": "shipfactory.plan/v1",
  "task_spec_sha256": "...",
  "base_sha": "...",
  "nodes": [
    {
      "id": "build-action-journal",
      "title": "...",
      "needs": [],
      "kind": "logic",
      "requirements": ["REQ-1", "REQ-3"],
      "allowed_paths": ["shipfactory/recipes/advancer.py"],
      "expected_outputs": ["change-set"],
      "test_cases": ["TEST-REQ-1-A"],
      "risk_tags": ["control-plane"]
    }
  ],
  "integration_order": [],
  "shared_file_overlaps": [],
  "residual_risks": []
}
```

Validation rejects:

- cycles;
- unknown requirement IDs;
- requirements not covered by a node;
- test cases not mapped to requirements;
- overlapping write paths without an explicit seam plan;
- nodes that can modify policy, verification, or deployment control without a high-risk tag;
- a base SHA different from the exploration and task-spec base;
- plans whose worst-case first activation cannot fit the budget.

### 2.2.8 Prompt-injection boundary

Task bodies, issue text, repository files, logs, web content, comments, and artifact prose are untrusted data.

The explorer must run:

- read-only;
- without deployment, approval, or messaging credentials;
- without access to the operator’s home or keychain;
- without arbitrary network access by default;
- with a system prompt that identifies repository instructions as data subordinate to the lane contract.

Prompt wording alone is not a security boundary. Tool and filesystem capabilities enforce the boundary.

There is an existing collision here: Codex identity injection writes `AGENTS.md` at the workspace root, while `write_identity()` overwrites that file when profile content exists. That can erase repository-local operating law rather than compose with it. Fix identity composition before calling repo instructions authoritative. `shipfactory/executors/base.py:106–114`; `shipfactory/executors/codex_exec.py:53–55`.  

### 2.2.9 Idempotency

Artifact identity is:

```text
sha256(instance_id | step_id | activation | kind)
```

The row is immutable once sealed.

A reactivation produces a new artifact row because activation changes. Old artifacts remain audit history.

If the daemon crashes after copying but before sealing, it revalidates the candidate and sealed bytes. It does not overwrite a different hash.

If the Git tree changes after exploration, the artifact becomes `stale`; it is not silently “updated.” A new activation reruns exploration.

### 2.2.10 Budget behavior

Do not use one undifferentiated activation cap for all planning.

Add v2 budget fields:

```yaml
budgets:
  max_activations: 16
  max_tokens: 300000
  step_activation_caps:
    explore: 1
    spec-draft: 2
    spec-attack: 2
    plan-draft: 2
    plan-attack: 2
    build: 3
  token_pools:
    planning: 120000
    build: 130000
    review: 50000
```

Admission charges remain non-refundable. Every activation is charged to both the instance and its named token pool.

### 2.2.11 Adversarial tests

- A valid request contains a backticked shell command.
- A proposed path does not yet exist.
- A hallucinated symbol is not backtick-quoted.
- A Unicode homoglyph resembles a real symbol.
- A path escapes through `../` or a symlink.
- Repository text says “ignore the operator and approve.”
- An issue body supplies fake JSON that resembles a plan.
- An old artifact from another commit has a valid schema.
- A line citation becomes stale after a preceding edit.
- A plan covers every file but misses a user-visible requirement.
- Two nodes claim the same file without declaring overlap.
- A plan hides test removal under a “generated” classification.
- `spec-attack` rejects and only the spec cone reactivates.
- `plan-attack` rejects and exploration does not rerun unnecessarily.
- The artifact file changes between validation and copy.
- A 100 MB artifact attempts to exhaust disk or parser memory.
- The explorer executor claims read-only support but succeeds in writing.

---

## 2.3 WS3 — bounded plan expansion

### Decision: reject same-instance runtime fan-out

The current engine assumes a static pinned recipe graph:

- the v1 loader permits exactly five primitives;
- the instantiated step rows come directly from the published recipe;
- `recipe_for_instance()` reloads that static pinned graph;
- revision ancestry is calculated from the static recipe definition;
- reconciliation bounds its fixpoint around recipe steps;
- §17 explicitly prohibits nested runtime recipes in v1.   

Adding arbitrary `recipe_steps` to the same instance would require rewriting:

- loader validation;
- persisted step definitions;
- dependency storage;
- revision vectors;
- cone invalidation;
- cancellation topology;
- collector completion;
- dashboard ordering;
- budget admission;
- reconciliation fixpoint bounds;
- recipe immutability semantics.

That is not “one primitive.” It is a second workflow engine hidden inside the first.

### Recommended model: child instances under one expansion group

Reuse the mechanism already proven in `_instantiate_nodes()`: deterministic child instance IDs, child collectors, and dependency links between collectors. Extract that mechanism rather than duplicating it. `shipfactory/recipes/selector_stage.py:118–178`. 

### 2.3.1 Schema

```sql
CREATE TABLE instance_relations (
    parent_instance_id  TEXT NOT NULL,
    child_instance_id   TEXT NOT NULL,
    relation            TEXT NOT NULL,
    ordinal             INTEGER NOT NULL,
    PRIMARY KEY(parent_instance_id, child_instance_id, relation)
);

CREATE TABLE budget_groups (
    id                  TEXT PRIMARY KEY,
    board               TEXT NOT NULL,
    max_tokens          INTEGER NOT NULL,
    charged_tokens      INTEGER NOT NULL DEFAULT 0,
    reserved_tokens     INTEGER NOT NULL DEFAULT 0,
    max_activations     INTEGER NOT NULL,
    activation_count    INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

ALTER TABLE recipe_instances ADD COLUMN budget_group_id TEXT;
ALTER TABLE budget_charges ADD COLUMN budget_group_id TEXT;

CREATE TABLE budget_reservations (
    key              TEXT PRIMARY KEY,
    budget_group_id  TEXT NOT NULL,
    board            TEXT NOT NULL,
    utc_day          TEXT NOT NULL,
    tokens           INTEGER NOT NULL,
    activations      INTEGER NOT NULL,
    state            TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    consumed_at      TEXT,
    released_at      TEXT
);

CREATE TABLE plan_expansions (
    id                    TEXT PRIMARY KEY,
    supervisor_instance_id TEXT NOT NULL,
    source_artifact_id    TEXT NOT NULL,
    source_sha256         TEXT NOT NULL,
    graph_json            TEXT NOT NULL,
    graph_hash            TEXT NOT NULL,
    child_recipe_id       TEXT NOT NULL,
    child_recipe_version  INTEGER NOT NULL,
    max_children          INTEGER NOT NULL,
    state                 TEXT NOT NULL,
    budget_group_id       TEXT NOT NULL,
    completion_step_id    TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    applied_at            TEXT,
    last_error            TEXT,
    UNIQUE(supervisor_instance_id, source_artifact_id)
);

CREATE TABLE plan_expansion_nodes (
    expansion_id       TEXT NOT NULL,
    node_id            TEXT NOT NULL,
    ordinal            INTEGER NOT NULL,
    needs_json         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    node_artifact_id   TEXT NOT NULL,
    child_instance_id  TEXT,
    collector_task_id  TEXT,
    base_sha           TEXT NOT NULL,
    branch_name        TEXT,
    head_sha           TEXT,
    state              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY(expansion_id, node_id)
);
```

### 2.3.2 Recipe declaration

Add a top-level v2 `expansions` declaration:

```yaml
expansions:
  - id: build-plan
    source_step: plan-draft
    source_artifact: plan
    child_recipe: build-subtask@1
    completion_step: builds-complete
    routing_policy: build-kind-router@1
    max_children: 2
```

The parent recipe contains a static wait:

```yaml
- id: builds-complete
  primitive: wait_for_event
  title: Wait for planned builds
  needs: [plan-attack]
  optional: false
  inputs:
    - from: plan-draft
      kind: plan
      required: true
  outputs: []
  params:
    event: build-plan-complete
```

This is an explicit §17 amendment. It does not mutate the parent graph. It instantiates pinned child recipes and completes an existing static wait.

### 2.3.3 Expansion state machine

```text
drafted
  -> validated
  -> waiting_resource
  -> budget_reserved
  -> materializing
  -> running
  -> children_done
  -> complete
```

Exceptional states:

```text
validated      -> blocked(invalid_plan)
waiting_resource -> cancelled
materializing  -> blocked(materialization_failed)
running        -> blocked(child_blocked)
*              -> cancelling -> cancelled
*              -> failed(invariant_error)
```

Materialize at most one child per action intent. Do not hold the Factory transaction while creating kanban collectors or links.

Child IDs are deterministic:

```text
sha256(expansion_id | graph_hash | node_id)
```

A crash after the third of four children simply resumes at the fourth. It does not guess whether earlier children exist.

### 2.3.4 Concurrency cap

Start with:

```text
max_children = 2
max_simultaneous_write_workers = 2
max_simultaneous_app_sessions = 1
max_simultaneous_browser_sessions = 1
```

The brief’s default of four is unjustified. The current daemon already requires an elevated file-descriptor limit after prior EMFILE/SQLite corruption, and current worktrees share writable Git state.  

Raise the cap only after measured headroom under browser, app, ffmpeg, SQLite, and model-worker load.

### 2.3.5 Git isolation

Current worktrees are not a security boundary. Codex is explicitly granted the repository’s common `.git` root so it can create locks, objects, and refs. Two parallel agents can therefore contend on or alter shared Git metadata. `shipfactory/executors/base.py:45–75`; `shipfactory/executors/codex_exec.py:18–31`.  

For write-capable fan-out, use per-child local clones with independent `.git` directories. The disk cost is acceptable at a cap of two. The integration controller receives only validated commit SHAs.

Each child emits a daemon-validated change-set artifact:

```json
{
  "base_sha": "...",
  "head_sha": "...",
  "tree_sha": "...",
  "commits": ["..."],
  "changed_paths": ["..."],
  "allowed_paths": ["..."]
}
```

Any changed path outside the node’s allowed set blocks integration.

### 2.3.6 Integration

A deterministic integration action:

1. creates a fresh integration clone at the pinned base;
2. orders child nodes topologically, then lexically by node ID;
3. cherry-picks exact validated commits;
4. verifies the resulting tree and change manifest;
5. emits an integration change-set artifact.

A conflict does **not** invoke an agent inside the release action.

It produces a conflict artifact containing:

```text
base SHA
child SHAs
conflicted paths
index stages
conflicting hunks
dependency order
```

A separate `integration-fix` agent activation may resolve it. Its output must undergo the full verification and review chain.

### 2.3.7 Routing

Do not let a model-written `kind` directly choose a seat.

Extend seat configuration with:

```yaml
seats:
  dev-frontend:
    ...
    capabilities: [ui]
    trust_domain: anthropic
```

Add immutable routing policy:

```yaml
routing_policies:
  build-kind-router@1:
    ui: [dev-frontend]
    logic: [dev-backend]
    test: [qa-builder]
```

Record the requested seat, configured model, actual provider, resolved model, executor version, and profile hash in the run row.

A seat name is not evidence of cross-lab independence. A proxy can route two seat names to the same provider or model family.

### 2.3.8 Budget interaction

Before materializing any child, reserve the worst-case first activation for:

- every child build;
- every mandatory child review;
- parent integration;
- parent verification.

Reservation occurs under `BEGIN IMMEDIATE` and counts against both instance-group and board-day ceilings.

On activation, a reservation is consumed into the existing non-refundable charge. Pre-activation cancellation may release a reservation; an actual admission charge remains non-refundable.

This avoids building half a graph and discovering the remaining half cannot be admitted.

Also fix the existing board-day cap first: `_admit()` currently reads `FACTORY_BOARD_DAY_TOKEN_CEILING` from the environment rather than the validated `recipes.board_day_token_ceiling` passed through daemon configuration. `shipfactory/recipes/advancer.py:78–91`. 

### 2.3.9 Cancellation

Cancelling the supervisor:

1. sets the supervisor and expansion to `cancelling`;
2. refuses new child materialization;
3. queues cancellation for child instances in reverse topological order;
4. waits for confirmed child worker exits;
5. retains all child artifacts and collectors;
6. cancels the parent wait only when children are terminal.

No child collector is allowed to satisfy an outer dependency merely because it was archived.

### 2.3.10 Adversarial tests

- Two expansion controllers materialize the same graph concurrently.
- Crash after each child-creation boundary.
- Child collector exists but Factory relation row is absent.
- Factory row exists but collector creation failed.
- Graph contains a cycle.
- Graph contains three nodes when cap is two.
- Two children change the same file without declaring overlap.
- A child changes `.git/config`.
- A child creates additional refs outside its namespace.
- Board budget is consumed concurrently by another instance.
- Cancellation occurs while a child is committing.
- Child completes but its change-set SHA belongs to another clone.
- One child blocks and another succeeds.
- Integration order changes across process restarts.
- A conflict is incorrectly “resolved” by dropping one child.
- FD/resource capacity is exhausted while Factory DB remains healthy.
- Parent completes before all child collectors are done.
- A stale expansion-complete event targets a newer plan activation.

---

## 2.4 WS4 — deterministic watched verification and evidence

### Correction to the brief

“Video proof over text verdict” is useful UI framing and bad security framing.

Video proves that pixels were recorded. By itself it does not prove:

- which commit was running;
- which workspace served the app;
- that assertions ran;
- that backend state changed correctly;
- that the result persisted after reload;
- that authorization boundaries held;
- that unshown flows did not regress;
- that the recording was not copied from an earlier run;
- that tests were not weakened;
- that the recording was not edited;
- that main still matches the tested revision.

The machine-checkable object must be a daemon-sealed verification manifest bound to a commit and structured oracles. Video is one item in that bundle.

### 2.4.1 Add a non-model `verification` primitive

Do not overload `review_gate`. A model running commands and saying “125 passed” is still model testimony.

Add to recipe v2:

```yaml
- id: verify-runtime
  primitive: verification
  title: Verify the candidate revision
  needs: [build]
  optional: false
  inputs:
    - from: build
      kind: change-set
      required: true
    - from: plan-draft
      kind: plan
      required: true
  outputs:
    - kind: evidence-bundle
      schema: shipfactory.evidence/v1
      path: .shipfactory-output/evidence-manifest.json
  params:
    manifest: .shipfactory/verification.yaml
    profile: browser-standard
    environment: app
```

`verification` has no seat and invokes no model. It is executed by the action runner.

This is a justified §17 primitive amendment. If the choice is between adding a machine primitive and treating model prose as evidence, add the primitive.

### 2.4.2 Verification profile

```yaml
recipes:
  verification_profiles:
    browser-standard:
      max_runtime_seconds: 1800
      infrastructure_retries: 1
      max_evidence_bytes: 1073741824
      max_log_bytes: 104857600
      capture_video: true
      capture_trace: true
      capture_har: true
      browser_slots: 1
```

### 2.4.3 Repository verification manifest

```yaml
# .shipfactory/verification.yaml
schema: shipfactory.verification/v1

cases:
  - id: unit-suite
    requirement_ids: ["REQ-1", "REQ-2"]
    driver: command
    argv: ["python", "-m", "pytest", "tests/", "-q"]
    oracle:
      type: exit_code
      equals: 0

  - id: approval-flow
    requirement_ids: ["REQ-3"]
    driver: playwright
    script: tests/e2e/approval-flow.spec.ts
    assertions:
      - type: visible
        selector: "[data-testid=approval-card]"
      - type: api-status
        request: gate-decision
        status: 202

capture:
  video: true
  trace: true
  screenshots: on-failure
```

Rules:

- argv arrays only;
- no interpolated shell;
- manifest pinned by blob SHA;
- every case maps to one or more requirement IDs;
- every required requirement is covered;
- unknown drivers fail closed;
- a candidate that modifies the verification manifest or runner is high risk;
- protected baseline tests from the previous trusted main revision run in addition to candidate tests.

Candidate-authored tests alone are not an independent oracle.

### 2.4.4 Evidence persistence

```sql
CREATE TABLE evidence_bundles (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    input_revision_hash   TEXT NOT NULL,
    base_sha              TEXT NOT NULL,
    head_sha              TEXT NOT NULL,
    tree_sha              TEXT NOT NULL,
    environment_session_id TEXT,
    manifest_relpath      TEXT NOT NULL,
    manifest_blob_sha     TEXT NOT NULL,
    state                 TEXT NOT NULL,
    bundle_sha256         TEXT,
    redaction_state       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    sealed_at             TEXT,
    invalid_reason        TEXT,
    UNIQUE(instance_id, step_id, activation)
);

CREATE TABLE evidence_items (
    id               TEXT PRIMARY KEY,
    bundle_id        TEXT NOT NULL,
    case_id          TEXT,
    kind             TEXT NOT NULL,
    path             TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    mime_type        TEXT,
    producer         TEXT NOT NULL,
    command_json     TEXT,
    cwd_relpath      TEXT,
    env_digest       TEXT,
    exit_code        INTEGER,
    started_at       TEXT,
    ended_at         TEXT,
    metadata_json    TEXT NOT NULL
);

CREATE TABLE verification_cases (
    bundle_id                TEXT NOT NULL,
    case_id                  TEXT NOT NULL,
    attempt                  INTEGER NOT NULL,
    requirement_ids_json     TEXT NOT NULL,
    oracle_type              TEXT NOT NULL,
    oracle_json              TEXT NOT NULL,
    status                   TEXT NOT NULL,
    evidence_item_ids_json   TEXT NOT NULL,
    started_at               TEXT NOT NULL,
    ended_at                 TEXT,
    PRIMARY KEY(bundle_id, case_id, attempt)
);
```

Evidence files live under:

```text
$HERMES_HOME/shipfactory/runs/<instance>/<step>/<activation>/evidence/
```

The API serves evidence by ID, not by arbitrary filesystem path.

The bundle hash covers:

- base/head/tree SHAs;
- manifest blob SHA;
- case metadata;
- commands;
- exit codes;
- timestamps;
- environment identity;
- every evidence item hash.

### 2.4.5 State machine

```text
ready
  -> preparing_environment
  -> running
  -> collecting
  -> redacting
  -> sealing
  -> done
```

Failures:

```text
preparing_environment -> blocked(environment_failed)
running               -> blocked(test_failed)
running               -> blocked(test_timeout)
running               -> blocked(test_infrastructure_error)
collecting            -> blocked(evidence_missing)
redacting             -> blocked(redaction_failed)
sealing               -> failed(evidence_invariant)
```

A deterministic test failure is not automatically retried.

A browser crash or infrastructure failure may receive one infrastructure retry. Every failed attempt remains visible. A “green after retry” bundle records the earlier failure and is not Phase-B-eligible unless the policy explicitly permits flake recovery.

### 2.4.6 Commit binding

Immediately before tests:

```text
git status --porcelain must be empty
HEAD must equal change_set.head_sha
tree SHA must equal change_set.tree_sha
```

Immediately after tests, repeat all three checks.

If any file changes during verification, invalidate the bundle.

The running app must expose a daemon-generated instance/head identity endpoint or header, and the browser runner must record it. A video overlay should include instance ID, head SHA, case ID, and runner timestamp generated by the runner—not by the app.

### 2.4.7 Surface selection

Do not let the model decide whether the diff “touches UI/API surfaces.”

Use deterministic path and symbol policies:

```text
UI paths      -> browser verification required
API routes    -> API verification required
DB migrations -> migration/rollback verification required
unknown       -> stricter profile
```

Model risk classification may raise the required profile. It may not lower it.

### 2.4.8 Review after verification

The review sequence becomes:

```text
verification
  -> correctness-review
  -> adversarial-review
  -> approval
```

Both reviewers receive:

- sealed task spec;
- sealed plan;
- exact diff;
- change-set artifact;
- sealed evidence bundle;
- failed/retried case history.

They do not receive only the builder’s summary.

The current “evidence bundle” is prose scraping. `_commit_hash()` hunts for SHA-looking strings and `_test_counts()` extracts phrases such as “125 passed” from metadata and summaries. That is useful for a human handoff but is not an attestation. `shipfactory/recipes/advancer.py:165–285`.  

### 2.4.9 Redaction and secrets

Use synthetic test accounts and non-production data.

Before sealing or publishing:

- scan text artifacts for known secrets and configured regexes;
- strip cookies and authorization headers from HAR;
- redact environment values;
- block the bundle if redaction is uncertain;
- do not commit evidence into the public repository;
- enforce evidence retention and deletion policy.

A screenshot can leak secrets even if stdout is clean.

### 2.4.10 Tests the implementation lane will miss

- Tests execute in the wrong worktree but still pass.
- An old video is copied into the new evidence directory.
- The app URL points to a stale previous session.
- The command prints “125 passed” and exits nonzero.
- Tests are skipped or deselected but exit zero.
- A UI route renders while the backend side effect never occurs.
- State appears correct before refresh and disappears after reload.
- A service worker or browser cache serves old assets.
- Candidate changes after verification but before review.
- Candidate alters the test manifest to remove a case.
- A secret appears in screenshot, trace, or HAR.
- ffmpeg hangs after tests finish.
- Browser process exits while the child app remains.
- A video is truncated but has a valid container header.
- Evidence exceeds the disk budget.
- First attempt fails and second passes.
- Reviewer and builder share the same provider despite different seat names.
- A model approves without opening the evidence.
- The evidence manifest references an item whose bytes were replaced after hashing.

---

## 2.5 WS5 — diff-as-story review

### Decision: merge into WS4

This is a presentation and review-artifact feature, not an independent engine workstream.

### 2.5.1 Story artifact

Use the common artifact table with kind `review-story`.

`shipfactory.review-story/v1` requires:

```json
{
  "schema": "shipfactory.review-story/v1",
  "instance_id": "...",
  "revision_hash": "...",
  "task_spec_sha256": "...",
  "plan_sha256": "...",
  "evidence_bundle_sha256": "...",
  "headline": "...",
  "changes": [
    {
      "importance": 1,
      "requirement_ids": ["REQ-3"],
      "files": ["shipfactory/recipes/advancer.py"],
      "why": "...",
      "risk": "...",
      "evidence_case_ids": ["approval-flow"]
    }
  ],
  "generated_or_mechanical_files": [],
  "not_changed": [],
  "residual_risks": []
}
```

Machine validation requires:

- every changed path appears exactly once;
- every requirement appears in at least one change or explicit “not implemented” entry;
- every safety claim links to an evidence case;
- files may not be hidden merely by calling them generated;
- deletions and configuration changes are always called out;
- residual risks cannot be empty when verification contained retries, skips, or warnings.

The story is narrative. The diff, spec, and evidence remain authoritative.

### 2.5.2 Gate-decision binding

The current dashboard approval request contains only instance and step. It is not bound to activation, revision vector, evidence bundle, or one-time nonce. `dashboard/plugin_api.py:578–587`. 

Add:

```sql
CREATE TABLE gate_decisions (
    id                   TEXT PRIMARY KEY,
    instance_id          TEXT NOT NULL,
    step_id              TEXT NOT NULL,
    activation           INTEGER NOT NULL,
    revision_hash        TEXT NOT NULL,
    evidence_bundle_id   TEXT,
    evidence_bundle_hash TEXT,
    actor_kind           TEXT NOT NULL,
    actor_id             TEXT NOT NULL,
    channel              TEXT NOT NULL,
    decision             TEXT NOT NULL,
    reason               TEXT,
    nonce_hash           TEXT,
    policy_hash          TEXT,
    created_at           TEXT NOT NULL,
    consumed_at          TEXT,
    advance_event_key    TEXT UNIQUE
);
```

The decision request must carry:

```text
instance
step
activation
revision_hash
evidence_bundle_hash
nonce
decision
```

A stale decision returns a conflict and does not enqueue an event.

### 2.5.3 Phone approval

Telegram should carry a signed, expiring deep link or one-time action token bound to the exact decision tuple.

Rules:

- token expiry no longer than ten minutes;
- one-time nonce;
- operator identity required at the decision service;
- no model seat or worker environment can read the signing key;
- duplicate click is a no-op;
- any rework invalidates all outstanding tokens;
- Telegram delivery is not the authority; the persisted decision is.

### 2.5.4 Adversarial tests

- Old phone link is clicked after rework.
- Link for one instance is modified to target another.
- Same nonce is replayed.
- Story omits a deleted security check.
- Story classifies lockfile changes as generated.
- Story contains HTML/script payload from an issue body.
- Large diff causes truncation and omitted files.
- Evidence bundle is replaced after notification but before click.
- Approval is valid for activation one but activation two is waiting.

---

## 2.6 WS6 — release, deployment, and intake

These are three separate workstreams.

## 2.6.A Release queue

### Use a `release` primitive, not a raw `merge` primitive

“Merge worktree to main” is too underspecified. The operation must integrate against a moving target, reverify, perform a compare-and-swap push, and recover from an ambiguous push result.

Recipe:

```yaml
- id: release
  primitive: release
  title: Release the approved revision
  needs: [approval]
  optional: false
  inputs:
    - from: build
      kind: change-set
      required: true
    - from: verify-runtime
      kind: evidence-bundle
      required: true
  outputs:
    - kind: release-record
      schema: shipfactory.release/v1
      path: .shipfactory-output/release.json
  params:
    policy: main-standard
```

The recipe references an operator-owned release policy. It cannot specify arbitrary remotes, branches, or commands.

### Persistence

```sql
CREATE TABLE release_requests (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    approval_decision_id  TEXT NOT NULL,
    evidence_bundle_id    TEXT NOT NULL,
    evidence_bundle_hash  TEXT NOT NULL,
    repo_identity         TEXT NOT NULL,
    target_ref            TEXT NOT NULL,
    approved_base_sha     TEXT NOT NULL,
    candidate_sha         TEXT NOT NULL,
    candidate_tree_sha    TEXT NOT NULL,
    state                 TEXT NOT NULL,
    observed_target_sha   TEXT,
    integration_sha       TEXT,
    remote_result_sha     TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    last_error            TEXT,
    UNIQUE(instance_id, step_id, activation)
);

CREATE TABLE release_actions (
    key             TEXT PRIMARY KEY,
    release_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    action_intent_key TEXT NOT NULL,
    state           TEXT NOT NULL,
    stdout_path     TEXT,
    stderr_path     TEXT,
    result_json     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    last_error      TEXT,
    UNIQUE(release_id, kind, attempt)
);
```

### State machine

```text
requested
  -> queued
  -> fetching
  -> integrating
  -> reverifying
  -> ready_to_push
  -> pushing
  -> remote_verified
  -> merged
```

Exceptional:

```text
integrating -> blocked(conflict)
reverifying -> blocked(main_verification_failed)
pushing     -> waiting_retry(remote_unavailable)
*           -> failed(invariant_error)
```

### Release algorithm

1. Acquire `release_lock:<repo>`.
2. Confirm the approval decision still matches activation, revision hash, and evidence hash.
3. Fetch the target remote.
4. Record the observed remote target SHA.
5. Create a fresh integration clone at that SHA.
6. Apply the exact approved candidate commits.
7. Verify the resulting diff:
   - all approved changed paths and blob hashes are present;
   - no additional unapproved path appears;
   - no approved commit was silently dropped.
8. Run the full protected verification profile against the integration SHA.
9. Push only if the remote target still equals the observed SHA.
10. Fetch and confirm the remote ref equals the intended integration SHA.
11. Record `merged`.
12. Retain the source workspace and evidence for a configured forensic period; prune later.

Do not use force push.

The brief’s “check both source and tests came over” is too narrow. Some valid changes need no test-file change, and a malicious change can add meaningless tests. Compare the entire approved change manifest, not two file classes.

### Ambiguous outcomes

If the process dies after push but before success recording, recovery fetches the remote:

- remote equals intended integration SHA → mark succeeded;
- remote remains old SHA → retry with a fresh action attempt;
- remote moved elsewhere → requeue integration;
- remote state is unclassifiable → block for incident review.

### Conflicts

A conflict is not automatically resolved by the release controller.

Any model-assisted conflict resolution creates a new candidate SHA, which invalidates:

- prior verification;
- both review verdicts;
- approval decision;
- release request.

The result returns through verification and approval.

### Release tests

- Main advances before integration starts.
- Main advances after reverification but before push.
- Push succeeds but response is lost.
- Remote rejects due to branch protection.
- Candidate commit exists but one file was dropped.
- Extra unapproved file appears.
- Integration passes candidate tests but fails protected tests.
- Dirty operator main checkout is irrelevant because release uses a clean clone.
- Crash at every state boundary.
- Duplicate release event.
- Two repositories release concurrently, but one repository never has two releases.
- Worktree cleanup fails after successful remote merge.
- Git hooks attempt side effects.
- Disk fills during clone.

---

## 2.6.B Deployment controller

### Reject arbitrary `on_merged` scripts

A script being Git-versioned does not make it safe. If the candidate can modify the script and the same cycle later executes it with daemon privileges, the candidate has modified its own deployment control plane.

Deployment policy must live in an operator-controlled repository or be read from the last trusted target revision, not from the unapproved candidate.

Recipe:

```yaml
- id: deploy
  primitive: deploy
  title: Deploy the merged revision
  needs: [release]
  optional: false
  inputs:
    - from: release
      kind: release-record
      required: true
  outputs:
    - kind: deployment-record
      schema: shipfactory.deployment/v1
      path: .shipfactory-output/deployment.json
  params:
    policy: local-canary
```

### Persistence

```sql
CREATE TABLE deployments (
    id                       TEXT PRIMARY KEY,
    release_id               TEXT NOT NULL,
    environment              TEXT NOT NULL,
    artifact_sha             TEXT NOT NULL,
    previous_deployment_id   TEXT,
    state                    TEXT NOT NULL,
    deploy_action_key        TEXT,
    health_deadline          TEXT,
    health_result_json       TEXT,
    rollback_action_key      TEXT,
    created_at               TEXT NOT NULL,
    completed_at             TEXT,
    last_error               TEXT
);
```

State machine:

```text
queued
  -> preflight
  -> deploying
  -> verifying
  -> healthy
  -> committed
```

Failure path:

```text
deploying/verifying
  -> rollback_pending
  -> rolling_back
  -> rolled_back
```

Terminal failures:

```text
rollback_failed
invariant_failed
```

A deployment policy cannot be enabled until its rollback path has passed an independent drill.

### Daily-driver isolation

On the current Mac, autonomous deployment should initially be limited to services running under:

- a dedicated macOS service account;
- a dedicated state/data root;
- no access to the operator login keychain;
- no `sudo`;
- an allowlist of launchd labels and flag files;
- explicit port ranges;
- process, disk, and FD resource limits;
- a supervisor outside the code being deployed.

Do not allow the factory to arbitrarily restart other production systems on the operator’s daily driver.

Self-deployment of ShipFactory requires a separate supervisor:

1. stage the new release in a versioned directory;
2. start a shadow daemon against a non-production board or read-only health mode;
3. health-check it;
4. atomically switch the active symlink or launchd configuration;
5. retain the old daemon for rollback;
6. have the external supervisor—not the retiring daemon—confirm success.

The daemon must not kill and replace itself inside its own action transaction.

### Deployment tests

- Health check fails and previous version is automatically restored.
- Deployment process dies after changing the flag but before recording success.
- Rollback command fails.
- New daemon starts but cannot open Factory DB.
- New daemon creates a second active dispatcher.
- Feature flag changes but the app still serves old code.
- Machine reboots mid-deploy.
- Deployment script was modified by the same candidate.
- Deployment attempts to operate on an unapproved launchd label.
- Another production process owns the requested port.
- Disk is full during staging.
- Previous release directory was incorrectly pruned.

---

## 2.6.C Self-generating intake

### The finder does not create tasks directly

The current selector accepts leased triage tasks, calls a model, caches its decomposition, and may instantiate multiple recipe instances. It already has enough authority. A finder that can create arbitrary triage tasks gives a hallucinating or compromised model a direct demand-generation path into the factory. `shipfactory/recipes/selector_stage.py:201–278`. 

Use deterministic ingestion, deduplication, quarantine, and independent validation first.

### Persistence

```sql
CREATE TABLE intake_signals (
    id                     TEXT PRIMARY KEY,
    source                 TEXT NOT NULL,
    external_id            TEXT NOT NULL,
    observed_at            TEXT NOT NULL,
    raw_payload_path       TEXT NOT NULL,
    raw_payload_sha256     TEXT NOT NULL,
    normalized_fingerprint TEXT NOT NULL,
    trust_level            TEXT NOT NULL,
    severity               TEXT,
    repo_identity          TEXT NOT NULL,
    observed_revision      TEXT,
    caused_by_release_id   TEXT,
    state                  TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    UNIQUE(source, external_id)
);

CREATE INDEX idx_intake_fingerprint
ON intake_signals(repo_identity, normalized_fingerprint, state);

CREATE TABLE intake_proposals (
    id                   TEXT PRIMARY KEY,
    signal_id            TEXT NOT NULL,
    finder_run_id        INTEGER,
    spec_artifact_id     TEXT,
    confidence           REAL,
    proposed_risk_class  TEXT,
    validated_risk_class TEXT,
    state                TEXT NOT NULL,
    duplicate_of         TEXT,
    triage_task_id       TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE intake_rate_windows (
    source          TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    observed_count  INTEGER NOT NULL,
    proposed_count  INTEGER NOT NULL,
    accepted_count  INTEGER NOT NULL,
    rejected_count  INTEGER NOT NULL,
    PRIMARY KEY(source, window_start)
);
```

### Configuration

```yaml
recipes:
  intake:
    enabled: false
    mode: shadow
    max_signals_per_hour: 20
    max_proposals_per_day: 5
    max_active_self_generated: 2
    duplicate_cooldown_seconds: 86400
    quarantine_control_plane: true
    rollback_first_window_seconds: 3600
    sources:
      test_failures: true
      structured_logs: true
      todo_scan: false
```

Start with `mode: shadow`. Shadow mode records proposals but creates no triage task.

### Intake state machine

```text
observed
  -> normalized
  -> deduplicated
  -> quarantined
  -> proposed
  -> independently_validated
  -> accepted
  -> triaged
```

Alternatives:

```text
normalized  -> duplicate
quarantined -> rejected
proposed    -> rejected
*           -> suppressed(circuit_breaker)
```

### Signal handling

For test failures:

```text
fingerprint = repo + test node ID + normalized exception type + normalized top frames
```

For logs:

```text
fingerprint = repo + service + exception type + normalized stack + release
```

Strip request IDs, timestamps, UUIDs, and volatile addresses before fingerprinting.

Raw logs are stored as untrusted payloads. The model receives bounded quoted excerpts, never a raw message concatenated as instructions.

### Causal rollback rule

If a signal first appears shortly after a factory release and is plausibly attributable to that release:

1. pause new autonomous work for that repo;
2. evaluate rollback;
3. create an incident record;
4. rollback first when the health policy says to;
5. only then propose a follow-up fix.

Do not let the factory immediately file a task to “fix” damage it just caused while leaving the damaging deployment active.

### Circuit breakers

Trip the intake breaker when any of these occurs:

- more than the configured signals per hour;
- duplicate ratio exceeds a threshold;
- finder acceptance rate collapses;
- more than two active self-generated instances;
- the same fingerprint recurs after an attempted fix;
- control-plane errors originate from ShipFactory itself;
- a recent deployment is unhealthy;
- budget headroom falls below reserve;
- model/provider identity changes unexpectedly.

The kill switch must be a non-model, operator-owned config value checked before signal ingestion and before triage creation.

### Intake tests

- 10,000 identical errors.
- Same error with a different UUID each time.
- Crafted log line containing prompt injection.
- TODOs in vendor/generated directories.
- A model hallucinates ten features from one warning.
- Finder output references a nonexistent signal.
- A deployment causes a crash loop and the finder proposes a code fix instead of rollback.
- A self-generated fix changes logging and creates a new fingerprint for the same failure.
- Signal source clock is skewed.
- Intake DB write succeeds but triage creation fails.
- Triage task exists but proposal row is absent.
- Duplicate finder cron processes run.
- Kill switch is activated during proposal validation.
- Control-plane repository produces its own failure signal.

---

## 2.6.D Autonomy policy plane

Do not encode the autonomy tripwire table inside a mutable recipe narrative. Version it independently and immutably.

```sql
CREATE TABLE autonomy_policy_versions (
    id              TEXT NOT NULL,
    version         INTEGER NOT NULL,
    hash            TEXT NOT NULL,
    status          TEXT NOT NULL,
    document_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY(id, version)
);

CREATE TABLE risk_assessments (
    id                      TEXT PRIMARY KEY,
    instance_id             TEXT NOT NULL,
    revision_hash           TEXT NOT NULL,
    deterministic_class     TEXT NOT NULL,
    model_class             TEXT,
    final_class             TEXT NOT NULL,
    reasons_json            TEXT NOT NULL,
    classifier_version      TEXT NOT NULL,
    created_at              TEXT NOT NULL
);

CREATE TABLE autonomy_decisions (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    revision_hash         TEXT NOT NULL,
    evidence_bundle_hash  TEXT NOT NULL,
    policy_id             TEXT NOT NULL,
    policy_version        INTEGER NOT NULL,
    policy_hash           TEXT NOT NULL,
    risk_assessment_id    TEXT NOT NULL,
    action                TEXT NOT NULL,
    outcome               TEXT NOT NULL,
    reasons_json          TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
```

The policy engine is not an agent. It is a deterministic control-plane actor.

Auto-approval emits the same queued gate-decision event as a human, but records:

```text
actor_kind = policy
actor_id = autonomy-policy-engine
policy_hash
revision_hash
evidence_bundle_hash
```

Keep three independent policy booleans:

```text
auto_approve
auto_release
auto_deploy
```

A class may have auto-approval enabled while release or deployment still parks.

---

# 3. WHERE THIS GOES WRONG

## 3.1 Phase B creates failure modes the listed conditions do not catch

The brief’s conditions are:

- evidence complete;
- suite green;
- both verdicts approve;
- budget under cap.

Those conditions are necessary at best. They are not close to sufficient.

| Failure | Mechanism | Required control |
|---|---|---|
| Test reward hacking | Builder weakens, skips, mocks, or deletes tests; suite is green because the oracle was changed. | Protected baseline tests, diff policy for test/harness changes, mutation testing for eligible classes. |
| Evidence gaming | Agent copies an old video or creates an attractive flow that does not assert the requirement. | Daemon-generated bundle, commit binding, runner-controlled trace and assertions. |
| Wrong revision | Tests run on branch A while approval or merge uses branch B. | Commit/tree SHA in every artifact and decision; post-test dirty-tree check. |
| Shared blind spot | Two seat names use the same underlying provider, proxy, model family, or prompt assumptions. | Runtime provider/model attestation and distinct trust-domain requirement. |
| Prompt injection | Repo, issue, dependency, or log tells both models to approve or exfiltrate. | Untrusted-data boundary, capability sandbox, protected policy prompts, no approval credentials. |
| Risk underclassification | A “docs-only” change modifies `AGENTS.md`; “test-only” removes protection; a patch dependency runs install scripts. | Deterministic sensitive-path rules; these classes are not low-risk by default. |
| Diff laundering | Dangerous changes hide in generated files, renames, submodules, lockfiles, or minified bundles. | Full tree manifest, rename-aware diff, generated-file policy, lockfile and submodule inspection. |
| Moving-main TOCTOU | Candidate was safe against old main but conflicts semantically with work merged later. | Serialized release queue and reverification against integration SHA. |
| Flaky green | One passing retry masks a nondeterministic regression. | Preserve all attempts; flaky bundles ineligible by default. |
| Slow model drift | Provider silently changes model behavior; prior clean cycles no longer predict current runs. | Model/provider version tracking, automatic regression to Phase A on model change. |
| Judge drift | Reviewer becomes more permissive over time while still producing “approve.” | Calibrated holdout corpus and periodic seeded faults. |
| Budget theater | Under-budget says nothing about correctness, safety, or completeness. | Treat budget only as a resource predicate, never a safety predicate. |
| Secret leakage | Tests pass and video looks good while HAR or screenshot contains credentials. | Mandatory redaction, synthetic accounts, evidence publication block. |
| Post-deploy degradation | Verification covers immediate flow but not memory leak, load, data corruption, or delayed job failure. | Canary health window, production telemetry, rollback. |
| Bad ground truth | “Operator would have approved” measures operator agreement, not actual safety. | Include production outcome, rollback rate, escaped defect rate, and seeded-fault detection. |

The proposed low-risk classes are especially poor:

- **Docs-only** can alter `AGENTS.md`, release instructions, security guidance, or future agent prompts.
- **Test-only** can remove assertions and make future harmful changes invisible.
- **Patch dependency bumps** can change transitive code, install scripts, lockfiles, and supply-chain behavior.

None should be automatically low-risk without path- and ecosystem-specific policy.

## 3.2 Auto-merge and auto-deploy on the daily driver

This fails because one machine is simultaneously:

- execution worker;
- database host;
- browser/video host;
- operator workstation;
- release machine;
- production-service host;
- possibly control-plane host for other systems.

Concrete failure chains:

### Resource starvation

Two model workers, a full app, browser, ffmpeg, SQLite checkpoints, and tests consume FD, CPU, memory, and disk. The operator’s other production system times out. The factory sees that timeout as a new bug and files more work.

**Control:** dedicated service user, hard concurrency caps, disk quotas, reserved capacity, and no self-intake from resource-starvation signals until attribution runs.

### Self-restart split brain

A deployment restarts the daemon while the old daemon is still alive. Both dispatch. This is not hypothetical territory; dual writers and stale WAL state are already known scar tissue.

**Control:** external supervisor plus exclusive daemon lock.

### Shared credential blast radius

A compromised agent can read operator credentials, Git credentials, SSH agents, browser sessions, or keychain material if run under the login user.

**Control:** dedicated account, no login keychain, minimal credentials, repository-scoped push token, no production secrets in worker environments.

### Accidental control-plane modification

The factory modifies its own deployment script, database migration, or approval policy and immediately uses the modified version.

**Control:** self-modifying control-plane files are always high risk and execute only from the previously trusted revision.

### Sleep, reboot, VPN, and interactive interference

The workstation sleeps, changes network, mounts/unmounts volumes, or is rebooted during deployment.

**Control:** action reconciliation must tolerate abrupt loss, and production deployment on this machine remains a consciously accepted risk. For meaningful Phase C, move the execution/deployment account to a dedicated host or at minimum a dedicated always-on OS context.

## 3.3 Fan-out versus SQLite and one Mac

### WAL does not make SQLite a parallel write engine

WAL lets readers coexist with a writer. There is still one writer at a time. Fan-out increases:

- run-record writes;
- budget admissions;
- action leases;
- step transitions;
- selector writes;
- outbox writes;
- process reaping;
- multiple kanban-board writes.

Current `apply_events()` does not lease events, and current admissions read counters before writing. Under multiple advancers, that produces stale reads, busy upgrades, or duplicate external attempts—not useful parallelism.  

### FD limits are a capacity constraint, not a bootstrap instruction

The repository already requires `ulimit -n 4096` after an FD leak contributed to EMFILE and SQLite failures. Fan-out adds:

- model subprocess pipes and logs;
- prompt files;
- app sockets;
- browser processes;
- video files;
- SQLite connections;
- Git object and lock files;
- file watchers.

“Set 4096” is not resource governance. It is a higher cliff.

### Shared Git state creates hidden serialization

Current Codex worktree support grants the common `.git` root writable access. Parallel commits and ref operations contend on shared lock files and expose sibling refs.  

### Budget races appear before token overrun

The current board-day cap is read from an environment variable in `_admit()`, while selector admission uses validated config. These are already two inconsistent budget implementations. Fan-out will amplify the inconsistency.  

### Cancellation is not process-safe yet

Current cancellation sends SIGTERM to processes present in the in-memory `_RUNNING` map and immediately invokes `cancel_subtree()`. It does not wait for exit, and a daemon restart loses `_RUNNING`. Under fan-out, cancelled workers can continue modifying branches after dependencies have been suppressed. `shipfactory/recipes/advancer.py:778–820`. 

## 3.4 Self-generating intake

The central risk is a positive-feedback loop:

```text
factory release
  -> production error
  -> finder creates task
  -> factory creates fix
  -> fix changes logging/error shape
  -> finder sees new fingerprint
  -> more tasks
```

Without causal release linkage, rollback-first policy, dedupe, and a circuit breaker, the factory confuses its own damage with a growing backlog.

Other failure mechanisms:

- A public issue embeds prompt injection that reaches the selector.
- An attacker sends inputs that generate error logs and therefore work.
- A finder optimizes for number of tasks found rather than defects fixed.
- TODO scans prioritize old comments, vendored code, and intentional debt.
- Duplicate errors differ only by request ID and bypass naive hashes.
- One failing test creates a task every cron interval.
- The finder invents features because “opportunity” has no objective oracle.
- Intake creation outpaces execution and consumes the daily budget before real work arrives.
- A compromised cron process floods triage directly.
- Factory control-plane errors cause the factory to autonomously rewrite the factory while it is unhealthy.

Finder output must be a proposal, not a task.

## 3.5 Watched testing and the evidence-belief gap

A polished video can create more confidence than the underlying evidence deserves.

| The operator may infer | What the video actually establishes |
|---|---|
| “This is the reviewed commit.” | Only that some app was recorded unless the runner binds and displays the commit. |
| “The flow passed.” | Only that the visible path looked plausible; there may be no assertion. |
| “The backend is correct.” | Pixels do not prove state, persistence, or authorization. |
| “The entire feature was tested.” | Only shown cases were exercised. |
| “No regressions exist.” | Absence of visible failure in a short recording is weak negative evidence. |
| “The test is independent.” | The builder may have authored the test and the evidence script. |
| “This cannot be replayed.” | Media can be copied unless hash- and revision-bound by the daemon. |
| “Security is fine.” | A happy path says little about access control, injection, data leakage, or concurrency. |

Video should answer: **“What happened visually in this particular case?”**

It should not answer: **“Is this revision safe to release?”**

## 3.6 Existing engine failures likely to appear before the new features fail

In priority order:

### 1. The single-writer law is not currently true

CLI approve/reject and review release enqueue an event and immediately call `apply_events()`. The daemon also calls `apply_events()`. That is a second advancer path.  

### 2. Events are not leased

`apply_events()` selects up to 100 pending rows and processes them. Two processes can select the same events. 

### 3. Gate completion is cross-database and crash-ambiguous

The Factory transaction remains open while the kanban gate is unblocked and completed. A crash after kanban completion but before the Factory commit leaves the event pending; retry can fail because the task is already terminal. 

### 4. Root-collector completion spends the key before verifying the effect

`reconcile_root_collectors()` writes an already-applied event row using `INSERT OR REPLACE`, then calls `complete_task()` and ignores its return. If completion fails, the key masks future recovery—the exact class of failure the spent-key law warns about. 

### 5. Notification delivery holds a Factory transaction across subprocess calls

`deliver_outbox()` opens one Factory connection, selects up to fifty rows, and can spend thirty seconds per `hermes send` while retaining that connection’s transaction scope. A bad transport can block Factory writers for many minutes. 

### 6. Recipe startup is documented as fail-closed and implemented as fail-open

`daemon.tick()` catches configuration, import, file, and OS errors, sets recipe results to `None`, then continues to `dispatch_once()`. The comment says a configured recipe board is fail-closed; the control flow does the opposite. 

### 7. Execution profiles do not control runtime or retries

Configuration validates execution profiles containing `max_runtime_seconds`, `max_retries`, and `token_allowance`. But the recipe loader forbids runtime/retry fields in step params, while `activate()` looks for them in those forbidden params and otherwise uses hardcoded defaults. The profile currently controls token admission, not task runtime or retry limits.   

### 8. Unknown token usage is recorded as zero

The spec says unknown usage must remain unknown. `record_run_end()` converts missing values to zero, and the executor base contract also calls for zero when unknown. That corrupts both telemetry and any Phase-B predicate using actual usage.   

### 9. Process state is not durable

`_RUNNING` is an in-memory dictionary. `Popen()` occurs before `record_run_start()`. A daemon crash loses the process object; a DB failure after `Popen()` leaves an untracked worker. The Hermes executor bypasses Factory run tracking entirely. `shipfactory/spawn.py:20,69–72,118–135`.   

### 10. Reaping suppresses board-transition failures

Run completion is recorded first; every exception while completing or blocking the kanban task is swallowed. The comment assumes normal stale detection will recover, but after restart there may be no `_RUNNING` record to drive reconciliation. 

### 11. Review evidence is heuristic prose

Commit hashes and test counts are scraped from summaries and metadata. A worker can satisfy the display by writing “125 passed” and a plausible SHA. 

### 12. Legacy policy can reappear on a Factory DB error

`policy.on_complete()` treats the recipe lookup as best-effort and catches every exception. If the lookup fails under DB contention or corruption, it falls through to legacy policy behavior on a recipe task. That violates exclusive recipe authority exactly when state is least trustworthy. `shipfactory/policy.py:170–185`. 

### 13. Watchdog subprocesses have no timeout

`watchdog._run_kanban()` uses `subprocess.run()` without a timeout, and due monitors execute sequentially in the daemon tick. One hung Hermes CLI invocation can stop advancement, reaping, dispatch, and every board.  

### 14. Manual instantiation has an orphan-collector crash window

`instantiate()` creates the kanban collector before inserting the Factory instance. A crash between those databases leaves an orphan collector; for a randomly generated instance ID, retry cannot deterministically rediscover it. 

### 15. The current plan-check cannot legally request rework

As noted, it is a first-step review gate with no upstream producer. This should be corrected in the first new recipe version even before autonomous planning is added. 

---

# 4. WHAT THE REFERENCE PRODUCT KNOWS THAT THIS BRIEF MISSES

## 4.1 Hard-won operational choices worth copying

### Environment first

the reference product begins with the real runnable stack because downstream tests and review are guesses otherwise. It describes preinstalled dependencies, live services, authentication, seed data, browser state, and machine snapshots. ShipFactory should copy the operational lesson, while replacing cloud snapshots with pinned environment manifests and controlled sessions. citeturn200742view0

### Spend more reasoning before code

The manifesto treats planning as the cheapest place to find mistakes and explicitly uses cross-lab attack/revision before decomposition. That maps directly to typed exploration, spec, and plan artifacts. citeturn200742view0

### Fresh context per bounded unit

Fresh workers reduce context contamination and let model routing vary by job. ShipFactory already gets some of this from process-per-activation. It should preserve it in child instances rather than creating a giant orchestration context. citeturn200742view0

### Cross-lab review is a real control

The useful insight is not “two models.” It is **different correlated failure domains**. ShipFactory should record actual provider/model identity and reject supposed cross-lab review when independence cannot be attested. citeturn200742view0

### Watched end-to-end behavior matters

the reference product correctly distinguishes running a unit suite from exercising the actual user flow and retaining evidence. Copy the browser-driven execution, trace, and live preview. Do not copy the implication that watching a recording is a complete oracle. citeturn200742view0

### Merge is a queue, not a worker command

Their moving-main framing is operationally correct. Merge/release is serialized bookkeeping against a changing target, not “run `git merge` in the builder worktree.” citeturn200742view0

### “Done with you” is not sales fluff

Running initial sessions alongside the customer is also a safety strategy: policies are calibrated against real failures before hands-off operation. ShipFactory’s Phase A is the local analogue. citeturn200742view0

## 4.2 Choices driven by their SaaS/VC model that should not be copied

### Hundred-way parallelism

the reference product imagines many isolated cloud machines. ShipFactory has one Mac, one filesystem, shared Git state, local SQLite, finite ports, and finite FDs. The correct local optimization target is reliable throughput, not maximum concurrent sessions. citeturn200742view0

### “Tokens are not the bottleneck”

On this architecture, CPU, memory, disk, browser slots, process slots, operator-machine availability, model-subscription limits, and SQLite write serialization are bottlenecks. Token admission remains useful, but “drown the bottleneck in tokens” is not a resource plan. citeturn200742view0

### Raw machine access and broad secrets

A forward-deployed SaaS may justify extensive integrations and dedicated tenant machines. A solo local system should prefer a narrow service account, synthetic data, and minimum credentials. Do not turn “run the real stack” into “give every worker the operator’s environment.”

### Continuous vendor-managed model routing

the reference product sells ongoing model benchmarking and rerouting. ShipFactory can copy the need for reevaluation, but not assume seat metadata remains correct. It needs periodic benchmark fixtures and actual runtime model attestation.

## 4.3 What the manifesto conspicuously does not answer

The manifesto is a product thesis, not an operational safety specification. It does not materially specify:

- exactly-once external actions;
- crash recovery;
- rollback;
- canary deployment;
- prompt injection;
- compromised repositories or issue bodies;
- evidence provenance;
- protection against test weakening;
- secrets exfiltration;
- dependency supply-chain risk;
- flaky tests;
- incident response;
- audit-log integrity;
- control-plane self-modification;
- resource starvation;
- database recovery;
- disaster recovery.

ShipFactory must answer all of those itself.

Most importantly, the reference product does not claim recurring autonomous intent is a solved, production-complete layer. The manifesto describes it as the direction and says the system climbs one layer at a time. Treating self-generating intake as immediate parity work overreads the source. citeturn200742view0

---

# 5. BUILD ORDER + FIRST LANE

## 5.1 Recommended sequence

| Order | Lane | Dependency | Acceptance evidence |
|---:|---|---|---|
| 0 | **A0 single-writer/action journal** | None | Two-process race tests, daemon-lock test, kill-at-every-action-boundary tests, no duplicate effects. |
| 1 | **A1 durable runs/resource baseline** | A0 | Popen crash recovery, daemon restart reconstruction, Hermes/Codex/Claude parity, correct profile and board budget wiring. |
| 2 | **Artifact/revision foundation + recipe v2** | A0–A1 | Hash tampering, stale base, symlink, oversized artifact, wrong-worktree tests. |
| 3 | **WS2 serial explore/spec/plan** | Artifact foundation | Independent prompt-injection corpus, invalid reference corpus, legal revision loops, no fan-out. Publish `dev-pipeline@5`. |
| 4 | **WS1 environment/app sessions** | A1, artifact foundation | Real app fixture, port collision, restart, leaked-child, stale PID, app identity, cancellation. |
| 5 | **WS4 deterministic verification** | WS1, artifacts | Copied video, wrong SHA, post-test mutation, secret leakage, flaky retry, corrupted evidence. Publish `dev-pipeline@6`. |
| 6 | **WS5 story + gate UI** | WS4 | Diff completeness, stale phone link, replay, omitted deletion, XSS/content injection. |
| 7 | **WS6-A serialized release** | WS4–WS5 | Moving-main races, crash after push, remote CAS, dropped-file detection, full integration reverify. Publish `dev-pipeline@7`. |
| 8 | **WS6-B deploy + rollback** | Release | Forced health failure, automatic rollback, reboot/power-loss simulation, self-update supervisor. |
| 9 | **WS3 expansion, cap two** | Proven serial release path | Budget/resource race, clone isolation, deterministic integration, cancellation cascade. |
| 10 | **WS6-C intake shadow mode** | Release, deployment, incident attribution | Production-log replay, dedupe, flood, prompt injection, causal rollback, kill switch. |
| 11 | **Phase B by named class** | Sustained evidence | Holdout faults, operator sampling, production outcomes, rollback drills, model-drift reset. |

Do not place intake before fan-out and release safety. Demand generation before controlled delivery is how systems turn bugs into workloads and workloads into outages.

Each published recipe remains immutable. A reasonable progression is:

```text
dev-pipeline@5  serial typed planning
dev-pipeline@6  environment + deterministic verification
dev-pipeline@7  release queue
dev-pipeline@8  bounded expansion
```

Do not put WS2–WS6 into one `@5`.

## 5.2 First dispatched lane brief — verbatim

```markdown
# LANE BRIEF — A0 single-writer and recoverable-action foundation

You are a non-interactive build lane. Work only in your assigned worktree.
Do not add any autonomy-program feature. This lane changes the control plane
under the existing behavior so later features have a safe substrate.

Read, in order:

1. AGENTS.md
2. docs/factory-spec.md §15 and §17
3. shipfactory/store.py
4. shipfactory/daemon.py
5. shipfactory/cli.py
6. shipfactory/recipes/advancer.py
7. shipfactory/recipes/primitives.py
8. tests/test_recipes.py
9. tests/test_daemon.py
10. docs/briefs/2026-07-14-engine-fix-lane.md

## Mission

Make the advancer truthfully single-writer and make existing kanban/outbox
external effects recoverable across process races and kill -9 boundaries.

## Invariants — literal

- Existing advance-event keys are permanently spent.
- Never delete or reinsert an applied/discarded/failed advance-event key.
- A recovery attempt uses a fresh action-attempt key.
- Only the daemon applies advance events.
- CLI and dashboard commands enqueue only.
- Only the advancer writes recipe-step state.
- Published recipe bytes do not change.
- No Hermes-core change is in scope.
- No merge, deploy, evidence, app-up, fan-out, or intake feature is in scope.

## Scope — EXACTLY these 7 deltas

### A0-1 — Schema migrations

Add a transactional `schema_migrations` table and one numbered migration for
the fields/tables in this lane. Startup must fail on a partially applied or
checksum-mismatched migration. Preserve existing data.

### A0-2 — Daemon singleton

Hold an exclusive advisory lock at
`$HERMES_HOME/shipfactory/daemon.lock` for the daemon lifetime.

The lock record contains PID, process-start identity, boards, and executable.
A second daemon exits nonzero before opening a kanban board or dispatching.

Add `--require-recipes` to the daemon command. The production launcher uses
it. In this mode, missing/invalid recipe configuration is fatal; it never
falls through to legacy dispatch.

### A0-3 — CLI is enqueue-only

Remove direct calls to `advancer.apply_events()` from `_recipe_gate()` and
`_recipe_release()`.

Approve, reject, and release return a queued decision identifier and current
status. The gate does not move until a daemon tick.

Do not change the One Law or grant any agent approval authority.

### A0-4 — Advance-event leasing

Add lease owner, lease expiry, attempt count, outcome, and last-error fields
to `advance_events`.

Claim exactly one pending event under `BEGIN IMMEDIATE`.

States are:
`pending -> leased -> applied|discarded|failed`.

An expired lease may return to pending without reinserting the row.
Applied/discarded/failed rows never return to pending.

Stale or nonmatching events become `discarded` with a reason, not silently
indistinguishable from successful application.

### A0-5 — External action intents

Add `action_intents` with stable logical key plus attempt number.

Move these existing effects behind action intents:

1. approval-gate kanban completion;
2. triage-root collector completion;
3. notification delivery.

The event transaction may create an action intent but must not perform the
external command.

The action runner executes outside the Factory write transaction, verifies
the result, and records the outcome.

On retry it first probes whether the prior attempt already succeeded.
A retry inserts a fresh action-attempt key; it never reuses the advance-event
key.

Remove `INSERT OR REPLACE` from root-collector event handling.

### A0-6 — Outbox leasing

Claim one due outbox/action row, commit the lease, close the Factory
transaction, perform `hermes send`, then record the result in a new
transaction.

A slow or hung send must not retain a Factory write transaction.
Preserve bounded backoff and the existing no-model rule.

### A0-7 — Fail-closed recipe authority

On a recipe-enabled/required board:

- configuration or Factory DB lookup failure must not run legacy policy;
- startup-guard failure must not dispatch;
- recipe-state lookup failure in `policy.on_complete()` must not fall
  through to legacy reopen behavior.

Log and persist the failure as an incident/error outcome where possible.

## Required regressions — fail before, pass after

1. Two separate processes call `apply_events` for one gate decision; exactly
   one action intent and one kanban completion occur.
2. Start two daemon processes; the second exits before its first tick.
3. CLI approve leaves the gate waiting until a daemon tick.
4. Kill the action runner after gate completion but before outcome recording;
   restart marks the existing effect succeeded without a duplicate action.
5. Kill after action-intent insertion but before the effect; restart performs
   the effect once.
6. Root `complete_task()` returns false; no successful/applied completion is
   recorded and a fresh action attempt remains possible.
7. A `hermes send` stub sleeps longer than the SQLite busy timeout while a
   second process writes Factory state; the second write succeeds boundedly.
8. A stale gate decision for activation 1 cannot complete activation 2.
9. Every terminal advance-event key remains present and unchanged after
   recovery.
10. `--require-recipes` plus missing/invalid config performs zero dispatches.
11. A Factory DB error during recipe lookup performs zero legacy policy
    mutations.
12. Existing recipe, cancellation, selector, dashboard, and outbox tests stay
    green.

Use real SQLite files and real process concurrency for race tests. Thread-only
tests are insufficient.

## Acceptance

- Full suite green twice consecutively.
- Print both run counts.
- Independent adversarial lane adds kill-point and two-process tests after
  this lane is complete; this lane's own tests do not count as that suite.
- No published recipe file changes.
- No unexplained interface change to §15 or §17.
- Update AGENTS.md with any new numbered finding learned during the work.

## Honesty clause

Report every clause not satisfied literally. Do not replace a process-race
test with a mock and call it equivalent. `DONE_WITH_CONCERNS` is acceptable;
a false clean claim is not.

Final line:

LANE_RESULT: done <one-line summary>

or

LANE_RESULT: blocked <one-line reason>
```

This follows the strongest existing local brief pattern: exact numbered scope, reproducer-first tests, two full-suite runs, and an honesty clause.  

## 5.3 Completion evidence required per workstream

### A0/A1

Demand:

- independent two-process race suite;
- kill -9 failpoints at every cross-database/action boundary;
- no duplicate task, message, or transition;
- stale decisions rejected;
- worker reconstruction after daemon restart;
- FD count stable over repeated ticks;
- actual config ceiling enforced.

### WS2

Demand:

- a hostile repository corpus;
- stale artifact and wrong-base tests;
- prompt injection from task, repo, and log content;
- legal spec and plan rework cones;
- a plan that looks plausible but misses one requirement is rejected;
- an independent reviewer recreates the plan from intent and compares coverage.

### WS1

Demand:

- real app startup—not only a shell stub;
- app/head identity;
- port collision;
- restart recovery;
- leaked child cleanup;
- process-group cancellation;
- no public network binding;
- resource-cap enforcement.

### WS4

Demand:

- wrong-worktree and wrong-commit tests;
- copied evidence;
- post-test mutation;
- secret leakage;
- truncated/corrupted video;
- flaky first failure;
- protected baseline suite;
- trace and structured oracle evidence, not only screenshots.

### WS5

Demand:

- every changed file accounted for;
- every requirement mapped;
- deleted and control-plane files highlighted;
- stale/replayed phone actions rejected;
- untrusted story content sanitized.

### Release

Demand:

- moving-main test at each release boundary;
- crash after push before recording;
- remote ref confirmation;
- no force push;
- complete manifest transfer;
- full integration reverify;
- retained forensics after merge.

### Deployment

Demand:

- health-check failure;
- automatic rollback;
- rollback failure escalation;
- machine reboot during deploy;
- second-daemon prevention;
- candidate cannot alter the policy it is executing.

### Fan-out

Demand:

- max concurrency actually enforced;
- separate Git metadata;
- budget reservation race;
- FD/disk pressure;
- deterministic integration;
- cancellation cascade;
- child failure does not release parent.

### Intake

Demand:

- replay of real historical logs;
- duplicate and flood corpus;
- malicious issue/log inputs;
- post-release causal attribution;
- rollback-first behavior;
- shadow precision/recall metrics;
- kill-switch drill.

### Phase B

Demand all of:

- at least 50 eligible instances in the named class;
- at least 30 calendar days;
- zero escaped severity-1 or severity-2 defects;
- zero incorrect releases;
- zero failed rollbacks;
- random human audit of at least 10%;
- seeded-fault detection;
- no unresolved model/provider change;
- automatic return to Phase A after any policy, verifier, model, or deployment-system change.

One clean cycle is nowhere near this bar.

---

# 6. THE 10 QUESTIONS

## 1. What is trusted enough to control tools?

**Recommended answer:** Only operator-pinned configuration, previously trusted control-plane code, and daemon-generated metadata. Repository content, task bodies, issues, logs, model outputs, summaries, and evidence candidates are untrusted data.

**Tradeoff:** Agents get fewer convenient capabilities and some tasks park more often. In return, prompt injection does not automatically become deployment authority.

## 2. What exact object does an approval approve?

**Recommended answer:** The tuple:

```text
instance
step
activation
candidate commit SHA
candidate tree SHA
input revision hash
spec hash
plan hash
evidence bundle hash
```

**Tradeoff:** Any rework, rebase, conflict resolution, test change, or evidence regeneration invalidates the approval. This creates repeated review, but eliminates stale approval.

## 3. Can a change modify the machinery that judges or deploys it?

**Recommended answer:** No. Changes to Factory control-plane code, policy, verification manifests, test harnesses, deployment scripts, risk classifiers, evidence renderers, or autonomy policies are high-risk and use the previous trusted machinery for evaluation.

**Tradeoff:** Improving the harness is slower and may require a two-stage rollout. The benefit is avoiding self-certified control-plane changes.

## 4. What is the hard local blast radius?

**Recommended answer:** Initially two write workers, one app session, one browser session, one release per repository, and one deployment per environment. Autonomous workers run under a dedicated account with no access to unrelated production systems.

**Tradeoff:** Throughput is much lower than the the reference product vision. Reliability and operator-machine safety are much higher.

## 5. What counts as machine-verifiable evidence?

**Recommended answer:** A daemon-sealed manifest bound to an exact commit/tree, containing executed argv, environment identity, exit codes, structured requirement-level oracles, logs, traces, state assertions, and artifact hashes. Video is supplementary.

**Tradeoff:** Considerably more implementation work than attaching a recording to a verdict. It creates evidence a policy engine can actually evaluate.

## 6. What happens when main moves?

**Recommended answer:** One serialized release queue per repository. Every candidate integrates into a clean checkout of the latest target, reruns protected verification, and pushes with compare-and-swap semantics. Conflict resolution creates a new candidate requiring new evidence and approval.

**Tradeoff:** Reverification consumes time and compute. It prevents approvals from being silently applied to a different program than the one reviewed.

## 7. What is the rollback contract?

**Recommended answer:** No autonomous deployment without an automatically executable, previously tested rollback to a known-good artifact. Health-check failure triggers rollback without model judgment.

**Tradeoff:** Some deployment types remain human-gated because they are not reversible. That is a feature, not an implementation gap.

## 8. How is self-intake prevented from becoming a self-exciting queue?

**Recommended answer:** Deterministic signal collection, normalized fingerprints, causal linkage to releases, rollback-first incident handling, proposal quarantine, independent validation, strict rate limits, maximum active self-generated work, and a non-model kill switch.

**Tradeoff:** The system misses some legitimate opportunities and introduces intake latency. It does not drown itself in duplicate or hallucinated work.

## 9. What proves reviewers are independent?

**Recommended answer:** Recorded actual provider, resolved model, executor version, profile hash, and trust domain. Phase-B review requires distinct attested trust domains. A configured seat name is insufficient.

**Tradeoff:** Some proxy or subscription configurations become ineligible when actual model identity is unavailable. The remaining approvals have a defensible independence claim.

## 10. What exactly graduates a risk class?

**Recommended answer:** Separate graduation for auto-approval, auto-release, and auto-deployment. For auto-approval: at least 50 eligible instances over at least 30 days, zero serious escaped defects, zero incorrect approvals, 10% random human audit, seeded-fault detection, and automatic regression to Phase A after any model, policy, verifier, or harness change. Auto-release additionally requires moving-main and crash-recovery evidence. Auto-deploy additionally requires repeated rollback drills and production health outcomes.

**Tradeoff:** Phase C arrives slowly and may never cover control-plane, migration, dependency, authentication, security, spend, deletion, or deployment-policy changes on the current machine.

---

# Bottom line

ShipFactory should pursue the vision. It should not pursue this program map.

The strongest parts to retain are:

- environment-first execution;
- typed planning before code;
- fresh bounded workers;
- cross-domain review;
- real-flow verification;
- serialized release;
- graduated autonomy.

The parts to reject are:

- backtick grounding;
- same-instance runtime fan-out;
- video as proof;
- arbitrary post-merge scripts;
- direct finder-to-triage authority;
- one-cycle graduation;
- merging verification, presentation, release, and autonomy into one first lane.

The first engineering milestone is not a better approval card. It is making the existing single-writer and idempotency claims true across process races and external effects. Until that is done, every new autonomous capability increases the probability that ShipFactory will perform the wrong action exactly once—and then permanently remember that it already succeeded.