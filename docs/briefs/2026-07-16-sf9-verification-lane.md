<!-- Committed 2026-07-16. Wave 5 build lane. Source: review §2.4 (WS4), §5.1 order 5. -->

# LANE BRIEF — SF-9 deterministic verification primitive + sealed evidence bundles

Fresh clone /tmp/sf-lane-verify, branch `lane/verification`. Base includes
A0+A1+SF-5+SF-6+SF-8 (artifacts/sealing/schema-v2, planning pipeline + @5,
environment sessions). Read files fresh.

Read in order: AGENTS.md; review §2.4.1 (verification primitive — NORMATIVE
YAML), §2.4.2 (profiles), §2.4.3 (repo manifest — argv-only rules), §2.4.4
(persistence — NORMATIVE SQL), §2.4.5 (state machine), §2.4.6 (commit
binding); shipfactory/artifacts.py (sealing), environments.py (app sessions),
recipes/loader.py + advancer.py + primitives.py; docs/factory-spec.md §17.

Test command: bash -c 'ulimit -n 4096; PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
Baseline: whatever main holds when you start (paste it). Green ×2 at end.

## Invariants — literal
- `verification` is a NON-MODEL primitive: no seat, invokes no model. The
  action runner executes manifest cases as supervised children (A1 run
  identity; environments.py patterns for bounded children).
- Manifest (.shipfactory/verification.yaml) pinned by blob SHA from the
  TRUSTED BASE COMMIT. argv arrays only, no shell interpolation. Unknown
  drivers fail closed. A candidate diff touching the manifest or runner is
  control-plane risk.
- Evidence sealed like artifacts (temp+fsync+rename, sha256, factory-owned
  storage under $HERMES_HOME/shipfactory/runs/<instance>/<step>/<activation>/evidence/).
  Served by ID, never by caller-supplied path.
- Protected baseline: verification runs candidate cases AND the previous
  trusted revision's protected cases — candidate-authored tests alone are
  never the oracle.
- Deterministic failures NOT auto-retried; ONE infrastructure retry allowed;
  green-after-retry bundles record the earlier failure and are marked
  not Phase-B-eligible.
- This is a justified §17 primitive amendment — update docs/factory-spec.md
  §17 in the SAME change. Published recipe bytes (@1..@5) unchanged; do NOT
  publish @6 (that happens when WS1+WS4 integrate in a later lane).

## Scope — 5 deltas
1. Schema/tables: evidence_bundles, evidence_items, verification_cases
   (§2.4.4 SQL verbatim, numbered migration).
2. Manifest parsing/validation: shipfactory.verification/v1 (§2.4.3):
   command + playwright drivers modeled (playwright execution may be stubbed
   behind driver registry if deps absent — registry fails closed on unknown);
   every case maps to ≥1 requirement id; required requirements covered;
   manifest blob-SHA pinning.
3. Verification primitive + runner: recipe-v2 `verification` step kind
   (§2.4.1 shape); runner executes cases as bounded supervised children
   against an environment session (SF-8 integration: environment: app);
   oracles evaluated structurally (exit_code, output_contains at minimum;
   assertion types recorded for playwright).
4. Bundle sealing + commit binding: bundle hash covers SHAs, manifest blob,
   case metadata, commands, exit codes, timestamps, environment identity,
   item hashes (§2.4.4 list). State machine §2.4.5 verbatim with blocked
   reasons; redaction pass (secret-pattern scrub of captured output;
   redaction_state recorded).
5. Advancer wiring: verification step ready → runner scheduled; done only
   on sealed bundle with all required cases passed; blocked reasons per
   state machine; profile limits from validated operator config (§2.4.2).

## Required regressions
Copied-video/foreign-evidence: bundle referencing an item whose sha256 is
not in this bundle's sealed set → invalid. Wrong-SHA: bundle claiming a
head_sha ≠ built candidate → invalid at seal. Post-test mutation: worktree
changed after run start → detected via tree re-hash → invalid. Manifest
tamper: manifest bytes ≠ pinned blob SHA → refuse to run. Unknown driver →
fail closed. Deterministic failure not retried; infra failure retried once,
history visible. Protected-baseline case failing → bundle fails even when
candidate cases pass. Oversized evidence → capped per profile. Secret in
captured output → redacted, redaction_state=redacted. Suite ×2.

Commits: 'Abhinav Bansal <abhibansal-sg@users.noreply.github.com>', no AI
trailers/tracker IDs. Sandbox .git failure → finish + report. Do NOT push.
Final line: LANE_RESULT: done <summary> | blocked <reason>
