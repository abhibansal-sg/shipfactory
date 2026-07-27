# Projects & Visual Recipes Frozen Specification

Status: frozen planning contract, 2026-07-27. This document is the source of
truth for the implementation sprint. It describes additive API and persistence
work only; it does not authorize a commit, deployment, approval, daemon
mutation, worker-side Linear mutation, or published-recipe edit. The
post-implementation orchestrator closeout reconciliation described below is
the sole planned Linear status/comment action.

## Product and success definition

The daily product is one continuous flow:

`Projects → choose project → choose attached recipe → fill parameters → Start → watch the same recipe graph run.`

In plain English, success means an operator can open the Projects tab, select a
Hermes project, attach/detach allowed immutable recipe versions and set exactly
one default, fill the selected recipe's immutable parameter form, press a real
rendered Start button once, and then see the created flight and its live
execution on the graph derived from the exact pinned recipe bytes. Policy
changes survive reload and restart. A repeated request cannot create a second
flight. Existing board-first instances remain inspectable, but an unbound or
ambiguous board is shown under `Unclassified` and has no launch control.

## User journeys

### Bound-project launch

1. The dashboard loads `GET /projects` and renders project identity, recipe
   policy, and bounded flight rollups. Board names are not user-facing.
2. The operator selects a project. The UI requests
   `GET /projects/{project_id}/recipes` and renders the returned immutable
   parameter schemas, defaults, optional steps, budget, and graph link.
3. The operator fills parameters and provides a client-generated
   `idempotency_key`; an optional `linear_issue_id` identifies the one issue
   represented by this flight.
4. The UI posts `POST /projects/{project_id}/flights`. The server resolves the
   project and explicit Hermes board binding, validates attachment and
   parameters, and calls the existing Factory instantiation seam. The request
   never supplies a board.
5. The UI navigates to the returned `instance_id`, loads the exact graph and
   overlay, and polls read-only resources. It never advances a step.

### Attach and configure recipes

The Projects detail renders a real operator control for every configured
recipe version: attach, detach, and set-default actions all use
`PUT /projects/{project_id}/recipe-policy`. The control displays the current
allowed set and one default, rejects an attempt to default a detached recipe,
and refreshes from the server after a successful write. A reload or daemon/UI
restart reads the persisted ShipFactory policy and shows the same result.
The route stays on the existing local dashboard operator trust boundary used
by seat/create/update and instance write routes. The sprint does not defer this
control and does not add a new authentication system.
This is mandatory first-sprint acceptance, not an optional follow-up.

### Repeat and conflict

A retry with the same project, issue, recipe, version, parameters, skipped
steps, and `idempotency_key` returns the original flight identity. Reusing
either key with different launch facts returns `409 idempotency_conflict`; it
never creates a second collector or instance. A Linear issue is globally one
Factory flight; an issue reused with different launch facts also conflicts.

### Legacy and unclassified work

The Projects response includes a visible `unclassified` bucket for instances
whose board has no active Hermes project binding, has a null binding, or is
ambiguous. The bucket can show status, history, and receipts. It has no enabled
Start button. Rebinding is an operator action in Hermes; the Factory reflects
the next read and does not write a mapping.

### Human approval and recovery

An approval node is visibly operator-only. The dashboard may display the
existing queued approve/reject controls, but an agent never invokes them and
the Projects/graph APIs never provide an auto-approval path. The residual
instance `selection-7fa06c4d775ccaf15bc8f129`, step `work`, activation `1`,
reason `worker_blocked` is handled only through the existing audited CLI/event,
cancel, or eligible release path after live inspection. This specification does
not authorize direct SQL, a generic force-unblock, or approval.

## Authority boundaries and non-goals

Hermes `projects.db` is authoritative for project id, slug, name, archive state,
folders, and explicit `board_slug`. ShipFactory reads it live through the
existing Hermes project APIs and never stores project-to-board data. Attaching
recipes is persisted in ShipFactory policy, while project-to-board remains a
live Hermes binding: changing a Hermes board binding affects future launches
only. It never mutates the immutable project/board identity captured on an
existing instance; the instance already owns its execution board, so migration
16 does not add a duplicate board column.

ShipFactory is authoritative only for Factory recipe attachment policy and
Factory flight identity. The additive `project_recipe_policies` table is keyed
by Hermes `project_id`; it is not a board mapping and contains no board name.
Policy keys are immutable `recipe_id@version` values validated against the
configured recipe library.

The immutable recipe library and pinned `recipe_versions.normalized_yaml` are
the only workflow definitions. The graph is a disposable read-only projection;
there is no workflow registry, graph database, edited published YAML, or
client-owned state machine. A graph request validates the exact document before
projection and fails closed on malformed or ambiguous policy.

One Linear issue is one Factory flight. The engine does not decompose a work
ledger, create child flights, or infer a request from Linear metadata. The first
sprint accepts and persists `linear_issue_id`; the in-product backlink writer is
explicitly deferred/unavailable because this checkout has no Linear client or
backlink writer. After implementation, the orchestrator performs a separate
authorized Linear reconciliation by updating issue status/comments with commit,
focused-test, and runtime evidence.

The first sprint does not modify Hermes core, add a graph dependency, introduce
a new executor, weaken supervision, repair runtime state, or change a
published recipe. A Hermes modification becomes justified only if source
inspection proves the plugin cannot read the native project registry; current
source inspection proves it can.

## Backend API contract

All routes below are mounted below the existing plugin prefix. JSON field names
are exact. Unknown request fields are rejected. Timestamps are UTC ISO-8601
strings. Recipe keys are the literal `id@version` form.

### `GET /projects`

Response:

```json
{
  "projects": [{
    "id": "p_123", "slug": "factory", "name": "Factory",
    "binding": "bound",
    "recipes": {"allowed": ["dev-pipeline@14"], "default": "dev-pipeline@14"},
    "rollup": {"active": 1, "waiting": 1, "recent": [{
      "instance_id": "inst-1", "recipe": "dev-pipeline@14",
      "status": "waiting_gate", "updated_at": "2026-07-27T00:00:00+00:00",
      "linear_issue_id": "SF-123"
    }]}
  }],
  "unclassified": {
    "id": "unclassified", "label": "Unclassified", "binding": "unclassified",
    "rollup": {"active": 0, "waiting": 0, "recent": []}
  }
}
```

`binding` is `bound`, `unbound`, or `ambiguous` for a project and is
`unclassified` for the bucket. The response deliberately omits board names.
`recent` is bounded and read-only; it is not a ledger.

### `GET /projects/{project_id}/recipes`

Response:

```json
{
  "project_id": "p_123",
  "recipes": [{
    "key": "dev-pipeline@14", "id": "dev-pipeline", "version": 14,
    "status": "active", "recipe_hash": "sha256", "description": "...",
    "parameters": {"request": {"type": "string", "required": true, "default": null}},
    "budgets": {"max_activations": 27, "step_activation_caps": {}},
    "steps": [], "optional_steps": [], "default": true
  }],
  "default_recipe": "dev-pipeline@14"
}
```

`steps` retains recipe order and contains `id`, `title`, `primitive`, `needs`,
`optional`, `seat`, `execution_profile`, `access_mode`, `environment`,
`instructions`, `inputs`, `outputs`, and `activation_cap`. The server checks
the policy again at launch. Unknown project, unbound/ambiguous project,
unknown or disabled attachment, missing immutable version, and invalid policy
are explicit `400`/`404` errors; no fallback to the global recipe list occurs.

### `PUT /projects/{project_id}/recipe-policy`

This operator/configuration seam changes only Factory attachment policy; it
does not bind a board, instantiate a flight, or complete a gate.

Request: `{"allowed_recipe_keys":["dev-pipeline@14","creative-video@1"],"default_recipe_key":"dev-pipeline@14"}`

Response: `{"project_id":"p_123","allowed_recipe_keys":["creative-video@1","dev-pipeline@14"],"default_recipe_key":"dev-pipeline@14","updated_at":"2026-07-27T00:00:00+00:00"}`

The implementation uses the existing local dashboard operator trust boundary
already used by seat/create/update and instance write routes. It is a first-
sprint route and is not deferred for lack of a new auth system; no new auth
system is added. The API tests exercise the operator-bound write, and the
Projects DOM tests exercise attach, detach, and set-default buttons.
For a non-empty allowed set, exactly one default is required and must be a
member of that set; `null` is permitted only when the allowed set is empty.

### `POST /projects/{project_id}/flights`

Request:

```json
{
  "recipe": "dev-pipeline", "version": 14,
  "parameters": {"request": "Ship the change"}, "skip_steps": [],
  "linear_issue_id": "SF-123", "idempotency_key": "operator-launch-uuid"
}
```

Response `201` for a new flight and `200` for an idempotent replay:

```json
{
  "instance_id": "inst-1", "project_id": "p_123", "recipe": "dev-pipeline@14",
  "recipe_hash": "sha256", "parameters": {"request": "Ship the change"},
  "skip_steps": [], "linear_issue_id": "SF-123",
  "idempotency_key": "operator-launch-uuid",
  "linear_backlink": {"status": "unavailable", "issue_id": "SF-123", "reason": "in-product backlink writer deferred"},
  "status": "running", "created_at": "2026-07-27T00:00:00+00:00"
}
```

The route resolves and verifies the explicit Hermes board binding internally,
then passes that hidden board to the existing `instantiate()` seam. It never
accepts `board`, `board_slug`, or a client-selected collector. An unbound or
ambiguous project cannot launch.

### Graph, overlay, and error routes

`GET /recipes/{recipe_id}/versions/{version}/graph` returns the frozen graph
projection for an active immutable recipe. `GET /instances/{instance_id}/graph`
returns the same graph source plus the current overlay. Both are read-only.
The instance route uses the instance's pinned recipe version/hash, never the
library's latest version.

Existing `GET /instances/{instance_id}/receipts`, `/runs/{run_id}/log`, and
`/runs/{run_id}/prompt` remain the receipt/read paths. The graph inspector links
to these exact resources and does not expose filesystem paths.

Error response shape for all new routes:

```json
{"error": "idempotency_conflict", "message": "...", "field": "idempotency_key"}
```

`error` is stable machine-readable text; `message` is human-readable and
untrusted; `field` is optional. Use `404` for unknown project/instance/version,
`409` for idempotency or binding conflicts, and `400` for invalid policy,
parameters, or unsupported graph projection.

The nested response fields are exact, even when the examples above abbreviate
an array:

```text
FlightSummary = {
  instance_id: string,
  recipe: string,
  status: string,
  updated_at: string,
  linear_issue_id: string | null
}
ProjectRollup = {active: int, waiting: int, recent: FlightSummary[]}
ProjectSummary = {
  id: string, slug: string, name: string,
  binding: "bound" | "unbound" | "ambiguous",
  recipes: {allowed: string[], default: string | null},
  rollup: ProjectRollup
}
RecipeStepSummary = {
  id: string, title: string, primitive: string, needs: string[],
  optional: bool, seat: string | null, execution_profile: string | null,
  access_mode: "readonly" | "workspace_write" | null,
  environment: string | null, instructions: string | null,
  inputs: object[], outputs: object[], activation_cap: int | null
}
RecipeSummary = {
  key: string, id: string, version: int, status: string,
  recipe_hash: string, description: string, parameters: object,
  budgets: {max_activations: int | null, step_activation_caps: object},
  steps: RecipeStepSummary[], optional_steps: {id: string, title: string}[],
  default: bool
}
ProjectRecipesResponse = {
  project_id: string, recipes: RecipeSummary[], default_recipe: string | null
}
ProjectFlightResponse = {
  instance_id: string, project_id: string, recipe: string, recipe_hash: string,
  parameters: object, skip_steps: string[], linear_issue_id: string | null,
  idempotency_key: string, linear_backlink: {
    status: "pending" | "written" | "unavailable",
    issue_id: string | null, reason: string | null
  }, status: string, created_at: string
}
```

`GET /projects` returns `ProjectsResponse={projects: ProjectSummary[],
unclassified: {id: "unclassified", label: "Unclassified",
binding: "unclassified", rollup: ProjectRollup}}`. `PUT` returns
`ProjectRecipePolicy={project_id: string, allowed_recipe_keys: string[],
default_recipe_key: string | null, updated_at: string}`. `inputs` and `outputs`
retain the exact validated recipe objects; the API does not invent or strip
their declared keys.

## Persistence and migration contract

Add migration 16 after the current migration 15 in `shipfactory/store.py`.
Migration checksums remain immutable after publication. The migration is
additive and has no board mapping.

```sql
CREATE TABLE project_recipe_policies (
  project_id TEXT PRIMARY KEY,
  allowed_recipe_keys_json TEXT NOT NULL,
  default_recipe_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
ALTER TABLE recipe_instances ADD COLUMN project_id TEXT;
ALTER TABLE recipe_instances ADD COLUMN linear_issue_id TEXT;
ALTER TABLE recipe_instances ADD COLUMN launch_idempotency_key TEXT;
CREATE INDEX idx_project_recipe_policies_updated ON project_recipe_policies(updated_at);
CREATE INDEX idx_recipe_instances_project_updated ON recipe_instances(project_id, updated_at DESC);
CREATE UNIQUE INDEX uq_recipe_instances_linear_issue ON recipe_instances(linear_issue_id) WHERE linear_issue_id IS NOT NULL;
CREATE UNIQUE INDEX uq_recipe_instances_launch_key ON recipe_instances(project_id, launch_idempotency_key) WHERE project_id IS NOT NULL AND launch_idempotency_key IS NOT NULL;
```

`allowed_recipe_keys_json` is canonical JSON: a sorted, duplicate-free array of
strings. `default_recipe_key` is null or a member of that array. Legacy rows
have null `project_id` and remain visible through board classification; no
backfill may guess a project from a board, cwd, title, or issue.

The launch transaction laws are:

1. Resolve Hermes project and binding, validate policy/library/hash, and bind
   parameters before any external collector effect.
2. Compute a canonical request fingerprint from project id, recipe key/hash,
   canonical bound parameters, sorted skip steps, Linear issue id, and
   idempotency key. A mismatch against an existing unique key is `409`.
3. Derive a deterministic instance id from project id and idempotency key and
   pass it to the existing `instantiate(..., instance_id=...)` seam. The
   existing collector key remains deterministic on retry.
4. A successful instance row contains exact project and issue identity; policy
   edits never alter it. Unique indexes are the final race fence.
5. If a crash occurs after collector creation but before Factory insertion, a
   retry probes the deterministic collector/instance identity before creating
   another external task. No direct external effect runs inside a Factory
   state transaction.
6. A repeated Linear issue with the same canonical flight facts returns the
   existing instance; different facts conflict. A null issue is permitted, but
   its launch key remains mandatory.

## Frozen graph projection: `shipfactory.graph/v1`

The graph is generated from validated recipe bytes and has this exact shape:

```json
{
  "schema_version": "shipfactory.graph/v1",
  "source": {"recipe_id": "dev-pipeline", "version": 14, "recipe_key": "dev-pipeline@14", "recipe_hash": "sha256", "pinned": true},
  "nodes": [{
    "id": "build", "title": "Build", "primitive": "agent_task", "shape": "rectangle",
    "projection_only": false, "optional": false, "needs": ["plan-draft"],
    "inputs": [{"from": "plan-draft", "kind": "plan", "required": true}],
    "outputs": [{"kind": "change-set", "schema": "shipfactory.change-set/v1", "path": ".shipfactory-output/change-set.json"}],
    "params": {"seat": "builder", "instructions": "..."}, "seat": "builder",
    "execution_profile": "build", "access_mode": "workspace_write", "environment": "source",
    "activation_cap": 3, "legal_rework_targets": []
  }],
  "edges": [{"id": "plan-draft->build:needs", "from": "plan-draft", "to": "build", "kind": "needs", "kinds": ["needs"], "label": "needs", "projection_only": false}],
  "layout": {"direction": "TB", "rank_gap": 56, "lane_gap": 28}
}
```

Node order is recipe list order. `primitive` is the exact declared primitive.
`shape` is `rectangle` for work (`agent_task`, `review_gate`, `verification`,
`notify`) and `diamond` for deterministic routing/wait/human gates. A reviewer
LLM's judgment is inside the reviewer rectangle. A synthetic verdict router is
an additional diamond with id `{review_step_id}:verdict`,
`primitive: "review_verdict_router"`, and `projection_only: true`; it only
routes structured `approve` or `request_changes` output. `approval_gate` is a
diamond with explicit operator-only metadata and no seat.

Edges are coalesced by `(from,to)`. `kind` is `needs`, `input`, or
`review_rework`; `kinds` contains all coalesced kinds in stable order. A
`review_rework` edge is overlay metadata, not a dependency and never changes
the frozen DAG. Its label contains the legal target. Illegal, missing, or
ambiguous rework targets produce a visible `unsupported` node/edge with a
reason; they never become a guessed valid route.

`params` is declared policy metadata. Resolved instance parameters are in the
overlay and may not replace the source recipe. No graph field authorizes a
state transition.

## Layout and accessibility semantics

Use local deterministic rank/lane layout and native SVG created by the existing
React IIFE. Do not add Dagre, React Flow, canvas, or another graph framework.
Rank is `0` for a node with no `needs`, otherwise one plus the maximum parent
rank. Within a rank, retain recipe order, then id. Siblings share a rank; a
multi-parent consumer receives converging edges and may have a decorative,
non-executable join marker. Review rework edges are backward dashed arrows.

Skipped nodes remain in place with reduced opacity and a text label. Unknown or
unsupported values remain visible and are non-actionable. Every graph has
`role="group"`, a visible heading, and a text summary of source recipe/hash.
Each node is a focusable `g` with `tabindex="0"`, `data-graph-node="{id}"`,
`data-step-id="{step_id}"`, and an `aria-label` containing title, primitive,
state, and actor/blocker summary. Each edge has `data-graph-edge="{edge_id}"`,
`data-edge-from`, `data-edge-to`, `data-edge-kind`, and an accessible label.
Native SVG `<title>`/`<desc>` or equivalent text nodes describe the same
semantics. Keyboard Enter/Space opens the inspector; Escape closes it; focus is
visibly ringed. Color is never the only state signal. Human approval uses a
distinct label/icon plus `operator-only`; reduced-motion disables transitions.
Long graphs scroll without changing node order or edge endpoints.

Do not use `dangerouslySetInnerHTML`, `.innerHTML`, or HTML interpolation for
recipe instructions, review stories, logs, evidence, paths, or errors. Render
untrusted values as React text children.

## Live overlay, actor/blocker, history, receipts, and evidence

`GET /instances/{instance_id}/graph` adds:

```json
{
  "instance": {"instance_id": "inst-1", "project_id": "p_123", "status": "running", "linear_issue_id": "SF-123"},
  "next_actor": {"kind": "seat", "id": "builder", "step_id": "build", "activation": 1, "label": "builder / build profile"},
  "blocker": null,
  "nodes": [{"step_id": "build", "current_activation": 1, "state": "running", "attempts": 1, "task_id": "task-1", "actor": {"kind": "seat", "id": "builder"}, "blocker": null}],
  "history": [{"step_id": "build", "activation": 1, "state": "running", "rejected_by_step_id": null, "rejected_by_activation": null, "verdict": null, "finding_count": null}],
  "rework_edges": [],
  "receipts": {"available": true, "endpoint": "/instances/inst-1/receipts"},
  "evidence": {"status": "bound|missing|stale|unavailable", "items": []}
}
```

`next_actor` is server-authoritative and null when no next action exists. Its
`kind` is `seat`, `operator`, or `machine`; a missing durable run says
`assigned seat/profile; run not yet recorded` and never guesses a model.
`blocker` is null or `{kind, reason, step_id, activation}`. Prefer persisted
`blocked_reason`, binding error, artifact stale/missing reason, review stall,
or durable action outcome. Unmet dependencies are `waiting_on_dependencies`,
not blocked. Unknown states remain `unknown`.

The overlay uses latest step rows for the collapsed graph and every activation
from `recipe_steps` for history. Rework history stays folded within the same
logical node and shows causal source/target, verdict JSON, finding count,
decision binding, and evidence/review-story identity. It never creates a second
recipe node. Receipts expose exact run id, seat, executor, provider, resolved
model, timings, exit/result, token counts, access-enforcement label, and
`has_log`/`has_prompt`; raw content is fetched only after expansion and reports
truncation or missing-file errors. Evidence links are bound to the exact
instance/activation and sealed hash, never to an instance-wide latest artifact.

## Security and safety invariants

- Approval gates remain operator-only. Dashboard/API agents never approve,
  reject, release, or complete a gate; state-changing calls remain queued and
  the daemon is the single writer.
- Project lookup uses explicit Hermes `board_slug`; no cwd, title, current
  daemon board, or Linear inference. No Factory project-board table exists.
- Launch validates active attachment, immutable recipe hash, parameters,
  notification targets, and hidden board binding before instantiation.
- Unique launch and issue indexes plus canonical request comparison are the
  idempotency fence. A retry probes existing external identity before effects.
- Graph and overlay routes are read-only. The browser cannot mutate
  `recipe_steps`, `advance_events`, gates, or boards.
- Pinned graph bytes are hash-bound. A malformed or drifted document blocks
  projection rather than selecting a plausible fallback.
- T1 supervision is not weakened: isolate/mock-fence only the unrelated
  ambient scanner for the fake-pytest regression; retain a separate known,
  unreadable nonce-matching child test that must classify as infrastructure
  error.
- Stale daemon reconciliation, if implemented, runs only after the singleton
  lock is held. It uses PID plus OS start token; it never adopts an old writer
  and never closes a provably live matching identity.
- No runtime database repair, direct SQL heal, process kill without token
  fencing, `hermes update`, or worker-side/in-product Linear write is part of
  the implementation lane. The post-implementation orchestrator reconciliation
  is the explicit exception described in the authority boundary.
- Screenshot, log, prompt, review-story, and graph text is untrusted and must
  be escaped by normal React text rendering. No secrets belong in fixtures or
  committed evidence.

## Compatibility, rollback, and deferred items

All changes are additive. Existing `/recipes`, `/instances`, `/waiting`, and
decision routes remain backward-compatible. Existing instances with null
project identity remain visible through current board projections and can be
classified only by current Hermes binding. Migration 16 is forward-only in the
application: rollback means disable new routes, stop rendering the new
Projects/graph controls, and leave the additive table/nullable columns intact
for audit; no destructive downgrade or data rewrite is permitted. A dashboard
bundle rollback must restore the prior bundle and CSS as one pair.

Deferred: in-product Linear API client/backlink write; automatic migration of
legacy boards;
multi-issue flights; work-ledger decomposition; graph editing; graph registry;
client-side workflow semantics; new graph dependencies; browser screenshot
automation selection; live daemon DB repair; automatic residual-instance
cleanup; auto-approval; Hermes core changes; and full-suite execution in this
lane.

## Acceptance and proof

Focused tests may be red locally during TDD, but every integrated branch and
orchestrator checkpoint is green. This lane does not run the full suite.
Required proof includes:

1. Project adapter tests read Hermes identity and explicit binding and prove no
   Factory board mapping is written. Policy tests prove canonical
   allowed/default validation and migration 16.
2. API tests cover the operator-bound attach/detach/default policy write,
   reload/restart persistence, bound launch, no-board request, hidden board resolution,
   unbound/ambiguous rejection, policy filtering, parameter validation,
   idempotent replay, issue uniqueness, exact hash binding, and Unclassified
   rollups. Responses contain the exact fields above.
3. Graph tests cover `dev-pipeline@14`, `creative-video@1`, a synthetic
   in-memory parallel/join recipe, typed fan-in, review router diamonds,
   operator-only approval, unsupported routes, stable order, and pinned hash.
4. Overlay tests cover next actor, exact blocker, folded activations, rework
   edges, receipts, missing/stale evidence, and no browser write.
5. T1 tests prove fake-pytest `test_failed` despite an unrelated ambient
   unreadable process and preserve `test_infrastructure_error` for a known
   unreadable supervised child. Daemon tests cover stale closure, token
   mismatch, live matching identity, lock ordering, and clean stop.
6. Rendered DOM proof uses `dashboard/conformance-harness.html` through the
   documented Vite harness. It finds the bound project, chooses a recipe,
   attaches and detaches allowed versions, sets one default, reloads to prove
   persistence, and covers the real policy buttons and API call. It then fills
   parameters, finds `data-project-launch="{project_id}"`, clicks it,
   and asserts the success toast plus returned project/recipe/instance
   identity. It asserts no enabled launch control in Unclassified and no unsafe
   HTML APIs.
7. Screenshot proof shows: long `dev-pipeline@14`; creative review and human
   gate; synthetic parallel/join; expanded rework history; and narrow waiting
   approval. Check overlap, arrow direction, labels, focus ring, contrast,
   operator-only distinction, skipped/unsupported treatment, and reduced
   motion. The repository identifies the Vite server command but not a
   screenshot automation command; the orchestrator must name the approved
   browser capture tool before claiming screenshot proof.

## FROZEN INTERFACE CONTRACT

The following names, fields, paths, and ownership rules are exact. Workers may
not reinterpret them.

### Python helpers

```python
def resolve_hermes_project(project_id: str) -> HermesProjectProjection:
    ...

def load_project_recipe_policy(db: Any, project_id: str) -> ProjectRecipePolicy | None:
    ...

def save_project_recipe_policy(
    db: Any, project_id: str, allowed_recipe_keys: list[str],
    default_recipe_key: str | None,
) -> ProjectRecipePolicy:
    ...

def project_rollup(db: Any, project_id: str | None) -> ProjectRollup:
    ...

def launch_project_flight(
    project_id: str, request: ProjectFlightRequest,
) -> ProjectFlightResponse:
    ...

def project_graph(recipe: Mapping[str, Any], *, recipe_hash: str, pinned: bool) -> GraphProjection:
    ...

def instance_graph_overlay(db: Any, instance_id: str) -> GraphOverlay:
    ...

def reconcile_stale_daemon_runs(
    *, identity_probe: Callable[[int], str | None] | None = None,
) -> int:
    ...
```

The last helper is called only inside `daemon_lock()` after lock acquisition;
its default probe compares PID and persisted OS start token and never adopts a
row.

### Pydantic models and routes

```python
class ProjectFlightRequest(BaseModel):
    recipe: str = Field(min_length=1)
    version: int = Field(ge=1)
    parameters: dict[str, object] = Field(default_factory=dict)
    skip_steps: list[str] = Field(default_factory=list)
    linear_issue_id: str | None = None
    idempotency_key: str = Field(min_length=1)

class ProjectRecipePolicyWrite(BaseModel):
    allowed_recipe_keys: list[str]
    default_recipe_key: str | None = None

@router.get("/projects")
def list_projects() -> ProjectsResponse: ...

@router.get("/projects/{project_id}/recipes")
def project_recipes(project_id: str) -> ProjectRecipesResponse: ...

@router.put("/projects/{project_id}/recipe-policy")
def update_project_recipe_policy(
    project_id: str, request: ProjectRecipePolicyWrite,
) -> ProjectRecipePolicy: ...

@router.post("/projects/{project_id}/flights", status_code=201)
def create_project_flight(
    project_id: str, request: ProjectFlightRequest,
) -> ProjectFlightResponse: ...

@router.get("/recipes/{recipe_id}/versions/{version}/graph")
def recipe_graph(recipe_id: str, version: int) -> GraphProjection: ...

@router.get("/instances/{instance_id}/graph")
def instance_graph(instance_id: str) -> InstanceGraphResponse: ...
```

`ProjectsResponse`, `ProjectRecipesResponse`, `ProjectFlightResponse`,
`GraphProjection`, `GraphOverlay`, `InstanceGraphResponse`, `NextActor`,
`Blocker`, `ProjectRecipePolicy`, `ProjectRollup`, and `FlightSummary` use
exactly the JSON fields specified above. No board field appears in a public
Projects or flight request/response.

### DB columns and indexes

Migration 16 owns `project_recipe_policies(project_id,
allowed_recipe_keys_json, default_recipe_key, created_at, updated_at)`;
`recipe_instances` owns nullable legacy-compatible `project_id`,
`linear_issue_id`, and `launch_idempotency_key`. The exact indexes are
`idx_project_recipe_policies_updated`, `idx_recipe_instances_project_updated`,
`uq_recipe_instances_linear_issue`, and `uq_recipe_instances_launch_key` as
shown in the persistence section. No project-to-board column/table/index may
be added.

### Stable DOM attributes

Projects: `data-project-id`, `data-project-launch`, `data-recipe-key`,
`data-recipe-attach`, `data-recipe-detach`, `data-recipe-default`,
`data-flight-instance-id`, `data-unclassified`. Graph: `data-graph-node`,
`data-step-id`, `data-activation`, `data-graph-edge`, `data-edge-from`,
`data-edge-to`, `data-edge-kind`. All actionable controls have an accessible
name; Start is the only flight-launch control and is absent/disabled in
Unclassified.

### Lane-owned file lists

No worker may edit outside its list:

- SF-21 determinism: `tests/test_verification_adversarial.py` and, only if
  needed, `tests/test_verification.py`; production `shipfactory/verification.py`
  is not edited by this lane unless source inspection proves a pure injectable
  test seam impossible, and production classification remains unchanged.
- SF-21 daemon: `shipfactory/store.py`, `shipfactory/daemon.py`,
  `tests/test_daemon_run_record.py`.
- Project policy/store: `shipfactory/store.py`,
  `tests/test_project_recipe_policy.py` (new).
- Project API/rollups/launch: `dashboard/plugin_api.py`,
  `tests/test_projects_api.py` (new).
- Graph projection: new `shipfactory/recipe_graph.py`,
  `tests/test_graph_projection.py` (new) in Wave 1; graph routes later belong
  to the project API owner in `dashboard/plugin_api.py`.
- SVG renderer, Projects UI, attach controls, and live inspector:
  `dashboard/dist/index.js` only, one exclusive owner for the entire sprint;
  no concurrent renderer/inspector/UI edits.
- CSS: `dashboard/dist/style.css` only.
- Harness/conformance: `dashboard/conformance-harness.js`,
  `tests/test_dashboard_graph_contract.py` (new), and explicitly approved
  `dashboard/conformance-evidence/` artifacts only.
- Orchestrator-owned integration/docs: the two files named by this lane only;
  workers do not commit, push, merge, edit recipes, runtime state, or Git
  metadata.
