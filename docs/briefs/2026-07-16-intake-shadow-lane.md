<!-- Intake-shadow lane. Source: external program review §2.6.C and §5.1 order 10. -->

# LANE BRIEF — quarantined intake shadow mode

Run only after release, deployment, and incident attribution are merged.
Fresh current-main clone, branch `lane/intake-shadow`.

Read `AGENTS.md`; review §2.6.C; selector stage; release/deployment records;
store/config/daemon/action intents. Baseline current main; full suite ×2.

## Authority boundary

A finder never creates tasks directly. Ingestion, normalization,
deduplication, quarantine, independent validation, circuit breakers, and an
operator-owned kill switch precede any triage side effect. Initial shipped
configuration is `enabled: false`, `mode: shadow`; shadow records proposals
but creates NO triage task.

## Scope

1. Add normative `intake_signals`, fingerprint index, `intake_proposals`, and
   `intake_rate_windows` schema in numbered migrations with partial guards.
   Raw payloads are sealed, hash-bound untrusted data, never instruction text.
2. Add strict operator config exactly §2.6.C defaults: disabled, shadow,
   20 signals/hour, 5 proposals/day, 2 active self-generated, 86400s duplicate
   cooldown, control-plane quarantine, 3600s rollback-first window, structured
   test/log sources, TODO scanning off.
3. Implement exact state machine observed→normalized→deduplicated→
   quarantined→proposed→independently_validated→accepted→triaged, plus
   duplicate/rejected/suppressed states. In shadow, stop before triage.
4. Deterministic fingerprints strip request IDs/timestamps/UUIDs/volatile
   addresses. Model sees bounded quoted excerpts only. A proposal must bind
   to a real signal and sealed spec; deterministic risk class can only be
   raised, never lowered by model classification.
5. Causal rollback rule: signal shortly after attributable release pauses
   autonomous work, evaluates rollback, creates incident, rolls back first
   when policy says so, then permits follow-up proposal. Never leave damaging
   deployment active while proposing its fix.
6. Circuit breakers from §2.6.C ship day one: rate, duplicate ratio, finder
   acceptance collapse, >2 active self-generated, recurrence after fix,
   control-plane self-errors, unhealthy deployment, budget reserve, unexpected
   provider identity. Non-model operator kill switch checked before ingestion
   AND before any triage creation.
7. Triage creation (future non-shadow mode) uses durable action intent with
   probe-before-retry; DB-row/task divergence is reconciled explicitly.

## Mandatory tests — all §2.6.C cases

10,000 identical errors; same error/different UUIDs; prompt-injection log;
TODOs in vendor/generated; model invents ten features from one warning;
nonexistent signal reference; crash-loop release followed by fix proposal
instead of rollback; self-fix changes logging fingerprint; skewed clock;
DB write succeeds/task creation fails; task exists/proposal absent; duplicate
finder crons; kill switch during validation; control-plane repo self-signal.

Also prove shadow mode creates zero tasks and disabled mode ingests nothing.
Required author, no AI trailers/internal tracker labels. Do not push. Final:
`LANE_RESULT: done <summary> | blocked <reason>`
