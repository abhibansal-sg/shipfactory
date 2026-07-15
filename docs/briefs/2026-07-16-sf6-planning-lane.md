<!-- Committed 2026-07-16. Wave 4 build lane. Source: review §2.2.3–§2.2.10, §5.1 order 3. -->

# LANE BRIEF — SF-6 serial planning pipeline + dev-pipeline@5

Fresh clone /tmp/sf-lane-plan, branch `lane/planning-pipeline`. Base includes
A0+A1+SF-5 (artifacts, sealing, schema v2 loader) — read files fresh.

Read in order: AGENTS.md; review §2.2.4 (required pipeline), §2.2.5
(exploration artifact), §2.2.6 (task-spec artifact), §2.2.7 (plan artifact),
§2.2.9 (idempotency), §2.2.10 (budget fields); shipfactory/artifacts.py;
shipfactory/recipes/loader.py (v2), advancer.py, primitives.py;
recipes/dev-pipeline@4.yaml (immutable reference).

Test command: bash -c 'ulimit -n 4096; PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
Baseline: 192 passed. Green ×2 at the end, paste both counts.

## Invariants — literal
- dev-pipeline@1..@4 bytes unchanged. @5 is a NEW publish, v2 schema.
- Review verdicts may only target UPSTREAM agent_tasks (finding #29):
  spec-attack targets spec-draft; plan-attack targets plan-draft.
- Artifact rules from SF-5 apply: outputs sealed before done; workers
  never pass paths in prose. No Hermes-core changes.

## Scope — 4 deltas
1. **Artifact JSON schemas**: shipfactory.exploration/v1 (§2.2.5 verbatim —
   reference statuses existing/proposed/generated/external, existing paths
   must exist at base_sha, blob+text hashes on line refs, untrusted_directives
   captured), shipfactory.task-spec/v1 (§2.2.6 — REQ ids with behavior/
   oracle/risk; clarifications MUST be empty before spec-attack may approve),
   shipfactory.plan/v1 (§2.2.7 shapes). Register with SF-5's per-kind
   validation.
2. **dev-pipeline@5** (v2 YAML): explore → spec-draft → spec-attack
   (review_gate → spec-draft) → plan-draft → plan-attack (review_gate →
   plan-draft) → build. Explorer params: access_mode readonly, execution
   profile planning. Publish + sha-pin like @4.
3. **Budget fields** (§2.2.10): v2 budgets block — max_activations,
   max_tokens, step_activation_caps, token_pools; every activation charged
   to instance AND named pool; non-refundable admission preserved.
4. **Gate enforcement**: spec-attack cannot approve while clarifications
   non-empty (advancer-enforced, not prompt-hoped); rejection reactivates
   ONLY the targeted cone (spec cone, not exploration).

## Required regressions (fail before, pass after)
spec-attack rejection reactivates spec-draft only; plan-attack rejection
does not rerun exploration; clarifications non-empty blocks approve;
exploration referencing a nonexistent 'existing' path fails sealing;
@5 loads, @4 byte-identical; pool exhaustion blocks with visible reason;
step_activation_cap exceeded blocks instance (budget_exhausted). Suite ×2.

Commits: 'Abhinav Bansal <abhibansal-sg@users.noreply.github.com>', no AI
trailers/tracker IDs. Sandbox .git failure → finish + report, orchestrator
commits. Do NOT push.
Final line: LANE_RESULT: done <summary> | blocked <reason>
