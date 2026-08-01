# FROZEN CONTRACT — ShipFactory operator-gate hardening (2026-08-01)

Both lanes build against THIS file verbatim. Do not redesign these shapes.
If a shape here is wrong, STOP and report — do not silently diverge.

Repo: /Volumes/MainData/Developer/products/shipfactory
Branch: main (ahead of origin; do NOT commit, do NOT push — the orchestrator commits)

## Four defects being closed

| # | Defect | Owner lane |
|---|---|---|
| 1 | Approval card labels the CRITIC's output as "Proposed deliverable"; plan unreachable | Lane 2 |
| 2 | All output renders in flat `<pre>` — markdown illegible | Lane 2 |
| 3 | No cancel surface for graph Runs (operator had to hand-edit SQL) | Lane 1 (+ Lane 2 button) |
| 4 | Reject carries no feedback — `work` is the fixed string `f"Human decision: {result}"` | Lane 1 (+ Lane 2 textarea) |

## THE ONE LAW (non-negotiable)

Approval gates belong to the human operator. Agents never press Approve.
Cancel is NOT approve — cancel is an audited operator action and is permitted.
Nothing in either lane may auto-approve, auto-decide, or bypass a human gate.

## Lane file ownership — DO NOT CROSS

**Lane 1 (Python core) owns:**
- `shipfactory/store.py`
- `shipfactory/decisions.py`
- `shipfactory/graph_recipe.py`
- `dashboard/plugin_api.py`
- `shipfactory/cli.py`
- NEW `tests/test_graph_cancel_and_feedback.py`
- ONE line in `tests/test_graph_store_v1.py` (see "Migration 19" below)

**Lane 2 (dashboard bundle) owns:**
- `dashboard/dist/index.js`
- `dashboard/dist/style.css`
- NEW `tests/test_dashboard_approval_card.py`

Neither lane touches the other's files. Neither lane runs any `git` command.
Neither lane edits `AGENTS.md` (orchestrator owns it).

---

## SHARED SHAPE 1 — `human_box_decisions_v1.reason` (migration 19)

Lane 1 adds migration 19, named exactly:

```python
(19, "graphrunner_v1_decision_reason_and_cancel", _GRAPH_RUNNER_DECISION_REASON_MIGRATION_TEXT),
```

Column added to `human_box_decisions_v1`:

```sql
ALTER TABLE human_box_decisions_v1 ADD COLUMN reason TEXT
```

- `reason` is NULL-able (historical rows have no reason and must survive).
- Existing rows are NOT rewritten.
- Follow the exact idiom of migrations 17/18 in `store.py`: a `_TEXT` blob, a
  `_STATEMENTS` tuple, entries in BOTH `_MIGRATIONS` and `_MIGRATION_STATEMENTS`,
  and an `elif version == 19:` branch in the artifact-detection loop
  (~`store.py:880-900`) that detects the column via `PRAGMA table_info`.

**Finding #120 applies (this is the whole reason it is in the contract):**
`tests/test_graph_store_v1.py:214` asserts `... == 18`. Advance it to `19` in
the SAME landing. `tests/test_store.py:69` derives the max dynamically and needs
no edit. Grep for any other hardcoded `18` schema assertion before finishing.

## SHARED SHAPE 2 — decision API (Lane 1 implements, Lane 2 calls)

`POST /api/plugins/shipfactory/v1/human-boxes/{attempt_id}/decision`

Request body (Pydantic `GraphHumanDecision`, `extra="forbid"` STAYS):

```json
{
  "result": "approved" | "rejected" | "<any declared arrow label>",
  "nonce": "<fresh client nonce>",
  "actor_kind": "human",
  "actor_id": "local-operator",
  "channel": "dashboard",
  "reason": "<operator feedback text, optional>"
}
```

- `reason` is a NEW optional field: `str | None = Field(default=None, max_length=8000)`.
- **Policy (ratified by the operator): a reason is REQUIRED when the decision
  routes to a rework/negative arrow, optional otherwise.** Concretely: if
  `result == "rejected"`, a missing/blank `reason` fails closed with HTTP 422,
  `code: "invalid_human_decision"`, `field: "reason"`, message
  `"a rejection requires operator feedback"`. Any other `result` accepts a
  missing reason. Do NOT hardcode a list of negative labels beyond `rejected` —
  the check is exactly `result == "rejected"`.
- Blank means `reason.strip() == ""` after coercion.
- 8000-char cap; over-cap is 422 (Pydantic handles it).

Replay/idempotency: `reason` joins the identity tuple. An exact replay
(same attempt, nonce, result AND reason) returns the prior row with
`replayed: true`. Same nonce with a DIFFERENT reason is a conflict — 409.

## SHARED SHAPE 3 — feedback reaches the downstream box

This is the POINT of defect 4. `shipfactory/decisions.py:503` currently builds:

```python
"work": f"Human decision: {result}",
```

It becomes, EXACTLY (no other format — Lane 2 renders against this):

```python
work = f"Human decision: {result}"
if reason:
    work = f"{work}\n\nOperator feedback:\n{reason}"
```

So a rejected box downstream receives, verbatim in its `preceding_outputs`:

```
Human decision: rejected

Operator feedback:
<the operator's text>
```

The reason is ALSO persisted on the `human_box_decisions_v1` row (shape 1).
Both must land in the SAME transaction as today's decision write — the durable
record and the routed work must never disagree.

## SHARED SHAPE 4 — cancel API (Lane 1 implements, Lane 2 calls)

`POST /api/plugins/shipfactory/v1/runs/{run_id}/cancel`

Request body:

```json
{ "reason": "<why the operator is cancelling, optional>" }
```

Success response `200`:

```json
{ "run": { "id": "...", "state": "cancelled", "blocked_reason": "...", "completed_at": "..." } }
```

Semantics, all inside ONE `BEGIN IMMEDIATE`:

1. Run must currently be `running`, `paused`, or `escalated`. A run already
   `completed`/`failed`/`cancelled` returns **409** with
   `code: "run_not_cancellable"`. (Idempotency: cancelling an already-`cancelled`
   run returns 200 with the existing row, NOT 409.)
2. Every non-terminal `box_attempts_v1` row for the run moves to `failed`
   with `finished_at` set and
   `technical_failure = "operator cancelled the run"`.
3. Every `pending` `route_tokens_v1` row for the run moves to `cancelled`.
4. Every `pending`/`leased` `run_events_v1` row for the run moves to
   `discarded`, `outcome = "operator_cancelled"`, lease fields NULLed.
5. The run row moves to `state='cancelled'` with `completed_at` set and
   `blocked_reason` = canonical JSON
   `{"type":"operator_cancelled","reason":<reason or null>,"actor":<actor_id>,"at":<iso>}`
   (sorted keys, `separators=(",",":")`, `ensure_ascii=False`).

**Schema change required:** `recipe_runs_v1.state` CHECK currently allows
`('running','paused','escalated','completed','failed')` and the second CHECK
ties `completed_at` to `completed`/`failed`. Migration 19 must also admit
`'cancelled'` as a terminal state WITH `completed_at NOT NULL`. SQLite cannot
ALTER a CHECK — follow the EXACT table-rebuild idiom already used at
`store.py:562-596` (`_next` table → INSERT…SELECT → DROP → RENAME → recreate
index). Preserve every existing row and the
`idx_recipe_runs_v1_active` index.

Worker processes: the API does NOT kill OS processes. It marks state only.
The daemon's existing reap path observes the failed attempts and cleans up.
(Killing from the request thread would race the daemon — do not do it.)

CLI parity: add `shipfactory run cancel <run_id> [--reason TEXT]` in
`shipfactory/cli.py` that calls the SAME store function. Do not duplicate policy
in the CLI (finding #67 — the operator surface and the engine share one path).

## SHARED SHAPE 5 — which box holds the approvable artifact

`shipfactory/graph_recipe.py` gains an OPTIONAL top-level recipe key:

```yaml
approval_artifact: <box-id>
```

- Optional. Absent = today's behaviour (positional fallback, below).
- If present it MUST name a real box in the same recipe, else `RecipeError`
  at load time. Published recipes without it keep loading byte-identical —
  their hash MUST NOT change. Verify this: load an existing published recipe
  before and after and assert the hash is unchanged.
- Surface it in the graph API payload (`/v1/runs/{id}/graph`) as
  `recipe.approval_artifact` (string or `null`) so Lane 2 can read it.

Lane 2's card selects the plan to show as:
1. If `recipe.approval_artifact` is set → newest COMPLETED attempt of that box.
2. Else → walk the gate's incoming arrows backwards, skipping boxes whose
   `id` or `who` matches `/review|attack|critic|adversar/i`, and take the
   newest completed attempt of the first box that does not match.
3. Else → today's behaviour (last preceding output), so nothing regresses.

## Lane 2 rendering rules (defects 1 + 2)

- **Markdown subset rendered as React children. NEVER `dangerouslySetInnerHTML`.**
  Finding #51 is law: operator-trust surfaces render model text as text nodes.
  Support: ATX headings `#`–`####`, `-`/`*`/`1.` lists, fenced code blocks,
  tables (GFM pipe), `**bold**`, `` `code` ``, blockquotes, horizontal rules,
  paragraphs. Anything unrecognised falls through as a plain text paragraph —
  never dropped, never executed.
- Card order, top to bottom:
  1. Original request
  2. **Final plan / proposed deliverable** (selected per shape 5) — rendered
     markdown, NOT `<pre>`
  3. Critic verdicts — one collapsed `<details>` per critic box, each with its
     own heading + result pill, rendered markdown inside
  4. Raw payloads — collapsed, height-bounded, `<pre>` is fine HERE
  5. Decision controls
- Reject flow: choosing a `rejected` action reveals a required textarea
  (label "Why are you rejecting?"). Submit is disabled until non-blank.
  It posts `reason` per shape 2. Approve does not require it but MAY offer an
  optional note field.
- Cancel control: a "Cancel Run" button on the Run panel, behind an explicit
  confirm (two-step, not a native `confirm()`), posting shape 4.
  It must be visually distinct from and never adjacent to Approve.
- Existing bundle guard tests in `tests/test_dashboard_graph_v1.py` assert exact
  source substrings. Lane 2 MUST keep that file green — if a rewrite moves a
  string those tests assert, the rewrite is wrong, not the test. Read that file
  FIRST.

## Verification each lane owes

- `cd /Volumes/MainData/Developer/products/shipfactory`
- `ulimit -n 4096`
- `export PYTHONPATH=/Volumes/MainData/Developer/worktrees/hermes-shipfactory-recipe-apis`
- `export HERMES_MOBILE_PATH=/Volumes/MainData/Developer/worktrees/hermes-shipfactory-recipe-apis`
- `PY=/Volumes/MainData/Developer/products/hermes-mobile-live/.venv/bin/python`
- Lane 1: `$PY -m pytest tests/ -q` — ALL must pass. Report exact counts.
- Lane 2: `$PY -m pytest tests/test_dashboard_graph_v1.py tests/test_dashboard_approval_card.py tests/test_dashboard_plugin.py -q`
  plus `node --check dashboard/dist/index.js`.
- Report the REAL output. A claim of green without the command output is a
  failed lane. If something is already red on untouched `main`, say so
  explicitly rather than fixing unrelated code.

## Style

- Match surrounding code exactly. `dashboard/dist/index.js` is hand-written
  ES5-style IIFE using `h()` = `React.createElement` — no JSX, no arrow
  functions, no template literals, no `const`/`let`. Follow it.
- No new dependencies. No new files beyond the ones named above.
- Comments explain WHY (the finding), not what.
