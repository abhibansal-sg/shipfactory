<!-- Committed 2026-07-15. Wave 3 build lane. Source: external program review
     §2.2.2 (artifact persistence), §2.2.3 (recipe schema v2), §5.1 order 2.
     Executor: codex gpt-5.6-sol, fresh clone. -->

# LANE BRIEF — SF-5 artifact & revision identity foundation + recipe schema v2

You are a non-interactive build lane in a fresh clone at /tmp/sf-lane-art,
branch `lane/artifact-foundation`. Base includes merged A0 (single-writer,
action intents, event leasing) and A1 (durable runs, resource leases) — read
files fresh; never assume pre-A0/A1 shapes.

Read, in order:
1. AGENTS.md
2. docs/reviews/2026-07-15-external-program-review.md §2.2.1–§2.2.3, §2.2.5–§2.2.7, §2.2.9 (artifact identity, persistence, schema v2, artifact shapes, idempotency) — implement §2.2.2 and §2.2.3 EXACTLY as specified (SQL and YAML shapes are normative)
3. shipfactory/recipes/loader.py, advancer.py, primitives.py, instantiate.py
4. shipfactory/store.py (migrations pattern), spawn.py (worktree layout)
5. recipes/dev-pipeline@4.yaml (published, IMMUTABLE — reference only)

Test command:
bash -c 'ulimit -n 4096; PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
Baseline: 165 passed. Must stay green; run twice at the end, paste both counts.

## Invariants — literal
- Published recipe bytes (dev-pipeline@1..@4) unchanged. v2 schema lands as
  LOADER capability + new tables; no new published pipeline this lane (that
  is the planning lane's job).
- advance-event keys permanently spent; only the daemon applies events; only
  the advancer writes recipe-step state; external effects via action_intents.
- Capacity/limits from validated operator config only.
- No Hermes-core changes.

## Scope — EXACTLY these 5 deltas

### ART-1 — artifacts + artifact_edges tables (§2.2.2 verbatim)
Numbered schema migration adding `artifacts` and `artifact_edges` exactly as
specified, plus `input_artifact_set_hash`/`output_artifact_set_hash` columns
on recipe_steps. Artifact states: candidate → sealed | rejected.

### ART-2 — the sealing pipeline
Daemon-side, post-process-exit, for each declared output: open the expected
candidate path under `.shipfactory-output/` WITHOUT following symlinks
(O_NOFOLLOW semantics); enforce size ceiling from config
(recipes.artifact_max_bytes, default 2 MiB); validate JSON schema by `kind`
+ `schema_version`; validate repository references (base_sha/head_sha/
repo_tree_sha exist in the worktree's repo); copy into factory-owned storage
($HERMES_HOME/shipfactory/artifacts/<instance>/<step>/<activation>/);
sha256 the SEALED bytes; mark sealed; compute output_artifact_set_hash
(sorted kind:sha256 pairs). Any failure → artifact `rejected` with
validation_error persisted and the step blocked with a visible reason —
never a silent pass-through. Workers NEVER hand paths in prose.

### ART-3 — revision identity
Every artifact binds to base_sha + repo_tree_sha at production time. A sealed
artifact whose base_sha no longer matches the instance's current base is
STALE — provide `artifact_is_stale(artifact, instance)` and stale detection
tests. artifact_edges records derivation (spec derived-from exploration, etc.).

### ART-4 — recipe schema v2 loader
Loader accepts BOTH v1 (existing published recipes must load byte-identical
semantics) and v2. v2 steps have exactly: id, primitive, title, needs,
optional, inputs, outputs, params (§2.2.3 shapes normative; unknown keys
REJECTED with precise errors — match parse_verdict's strictness). inputs
entries: {from, kind, required}. outputs entries: {kind, schema, path — must
be under .shipfactory-output/}. Validation: inputs reference existing
producer steps; DAG acyclic via needs; output paths never escape the
output dir (.. and absolute rejected).

### ART-5 — advancer wiring
On v2 step activation: compute input_artifact_set_hash from sealed inputs
(missing required input → step blocked artifact_missing). On completion:
sealing pipeline runs before the step may reach `done`; a v2 agent_task
whose declared outputs fail sealing is NOT done (worker_failed with reason).
v1 recipes: zero behavior change — prove it.

## Required regressions — fail before, pass after
1. Hash tampering: sealed file byte-flipped on disk → detected on read-back.
2. Stale base: artifact sealed at base X, instance advanced to Y → stale.
3. Symlink attack: candidate path is a symlink to a file outside the
   worktree → rejected, never followed.
4. Oversized artifact → rejected with size reason.
5. Wrong-worktree: artifact referencing SHAs absent from its repo → rejected.
6. Duplicate seal attempt (same instance/step/activation/kind) → idempotent,
   single row.
7. v1 recipes load and run byte-identically (existing suite is the proof).
8. v2 loader rejects: unknown step keys, cyclic needs, input from
   nonexistent step, output path escaping the output dir.
9. Suite green ×2.

## Rules
Commits: logical units, author 'Abhinav Bansal
<abhibansal-sg@users.noreply.github.com>', no AI trailers, no tracker IDs.
Do NOT push. Report honestly; DONE_WITH_CONCERNS acceptable.

Final line: LANE_RESULT: done <summary> | blocked <reason>
