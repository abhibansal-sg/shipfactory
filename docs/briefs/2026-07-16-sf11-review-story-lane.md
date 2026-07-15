<!-- Committed 2026-07-16. Wave 6 build lane. Source: review §2.5 (WS5 merged into WS4), §5.1 order 6. -->

# LANE BRIEF — SF-11 review-story artifact + gate-decision binding + phone approval

Fresh clone /tmp/sf-lane-story, branch `lane/review-story`. Base includes
A0+A1+SF-5..SF-9 (artifacts, planning @5, environments, verification +
evidence bundles). Read files fresh.

Read in order: AGENTS.md; review §2.5.1 (story artifact — NORMATIVE JSON),
§2.5.2 (gate_decisions — NORMATIVE SQL), §2.5.3 (phone approval rules),
§2.5.4 (adversarial tests); shipfactory/artifacts.py (per-kind validation),
recipes/advancer.py (gate application, action_intents), dashboard/plugin_api.py
(current approval endpoints ~:578), cli.py (_recipe_gate).

Test command: bash -c 'ulimit -n 4096; PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'
Baseline: paste what main gives you. Green ×2 at end, both counts.

## Invariants — literal
- The ONE LAW: agents never press approve. This lane builds the BINDING
  machinery for the human's decision — nothing in it may auto-decide.
- Decisions remain ENQUEUE-ONLY (A0): a bound decision inserts a
  gate_decision row + queued event; the daemon applies it. No synchronous
  apply path. Stale decision → HTTP conflict, no event enqueued.
- No model seat or worker environment can read the signing key (store it
  under $HERMES_HOME/shipfactory/keys/, mode 0600, generated on first use;
  worker env construction must provably exclude it).
- Published recipe bytes unchanged. No Hermes-core changes.

## Scope — 4 deltas
1. **review-story artifact**: kind review-story, schema
   shipfactory.review-story/v1 (§2.5.1 verbatim). Machine validation: every
   changed path exactly once; every requirement in ≥1 change or explicit
   not-implemented; safety claims link to evidence case ids that EXIST in
   the bundle; generated-classification cannot hide lockfiles/workflows/
   deletions; residual_risks non-empty when verification had retries/skips/
   warnings. Story is narrative — diff/spec/evidence stay authoritative.
2. **gate_decisions table** (§2.5.2 SQL verbatim, numbered migration):
   decision requests must carry instance/step/activation/revision_hash/
   evidence_bundle_hash/nonce; binding validated against CURRENT state;
   stale → conflict; consumed decision links its advance_event_key (UNIQUE).
3. **Decision service + phone approval** (§2.5.3): signed expiring deep-link
   token (≤10 min, one-time nonce, HMAC over the exact decision tuple);
   duplicate click = no-op returning the recorded decision; ANY rework/new
   activation invalidates outstanding tokens; operator identity recorded
   (actor_kind/actor_id/channel); Telegram delivery is not authority — the
   persisted row is. Wire dashboard/plugin_api.py approve/reject to require
   the full tuple; CLI gains the same fields.
4. **Advancer consumption**: gate_decision applied on tick only when its
   binding still matches (activation + revision + bundle hash); mismatch →
   decision marked stale with reason, gate stays waiting, operator re-decides.

## Required regressions (§2.5.4 all nine + binding)
Old link clicked after rework → conflict, no event. Cross-instance link
tamper → signature fails. Nonce replay → no-op. Story omitting a deleted
security check → validation reject. Lockfile-as-generated → reject.
HTML/script payload in story fields → stored escaped, dashboard-safe.
Truncated large diff omitting files → completeness check rejects. Bundle
replaced after notification, before click → hash mismatch conflict.
Approval valid for activation 1 while 2 waits → stale, not applied.
Plus: signing key unreadable from worker env (construct a worker env and
prove exclusion). Suite ×2.

Commits: 'Abhinav Bansal <abhibansal-sg@users.noreply.github.com>', no AI
trailers/tracker IDs. Sandbox .git failure → finish + report. Do NOT push.
Final line: LANE_RESULT: done <summary> | blocked <reason>
