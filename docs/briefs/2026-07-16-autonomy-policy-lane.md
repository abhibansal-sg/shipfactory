<!-- Autonomy-policy lane. Source: external program review §2.6.D, §3.1, Phase B criteria. -->

# LANE BRIEF — Phase-B graduation machinery (all switches remain disabled)

Run after release, rollback-capable deployment, fan-out, and intake shadow are
merged. Fresh current-main clone, branch `lane/autonomy-policy`.

Read `AGENTS.md`; review §2.6.D; §3.1 failure table; Phase-B criteria
§2790–2804; gate/release/deploy implementations and policy hashes. Baseline
current main; suite ×2.

## Hard boundary

Build the deterministic policy/graduation machinery only. DO NOT enable
`auto_approve`, `auto_release`, or `auto_deploy`. Actual graduation is an
operator action after runtime evidence satisfies every criterion. One clean
cycle is nowhere near the bar.

## Scope

1. Add normative `autonomy_policy_versions`, `risk_assessments`, and
   `autonomy_decisions` tables in numbered migrations. Policy documents are
   independently immutable/versioned, not recipe prose.
2. Implement deterministic policy engine (not an agent). Three independent
   booleans: auto_approve, auto_release, auto_deploy. Defaults false and
   fail-closed when missing/invalid. A class may allow approval while release
   and deployment still park.
3. Auto-approval, if ever operator-enabled, emits the SAME queued bound gate
   decision as a human and records actor_kind=policy,
   actor_id=autonomy-policy-engine, policy/revision/evidence hashes. It never
   bypasses the daemon or mutates step state synchronously.
4. Deterministic risk class encodes sensitive paths/ecosystems. Docs-only,
   test-only, patch dependency, lockfile, submodule, generated/minified,
   control-plane/policy/verifier/deployer changes are NOT blanket low-risk.
   Model class may raise, never lower.
5. Graduation ledger requires ALL: ≥50 eligible instances in named class;
   ≥30 calendar days; zero escaped sev-1/2; zero incorrect releases; zero
   failed rollbacks; ≥10% random human audit; seeded-fault detection; no
   unresolved provider/model change. No fabricated/synthetic passage of
   time or count in production path.
6. Automatic regression to Phase A and switch disablement after ANY policy,
   verifier, model/provider, deployment-system, trust-domain, or protected-
   baseline change; also on escaped severe defect, incorrect release, failed
   rollback, seeded-fault miss, or policy invariant failure.
7. Enforce §3.1 controls: protected baselines/diff policy/mutation where
   eligible; daemon evidence binding; provider attestation; untrusted-data
   boundaries; rename/full-tree/generated/lockfile inspection; moving-main
   reverification; failed-attempt preservation; holdout/seeded-fault
   calibration; secret-publication block; canary window and rollback.
   Budget is resource predicate only, never safety evidence.
8. Produce read-only dashboard/CLI status explaining each unmet criterion
   and each independent switch. Enabling remains an explicit operator action
   outside this lane.

## Mandatory tests

Defaults all false; recipe/model cannot enable; one class approval does not
release/deploy; stale policy/revision/evidence hash rejects; provider alias
resolves same trust domain and fails independence; docs/test/dependency
misclassification; flaky green ineligible; 49 instances/29 days fail; missing
10% audit fails; seeded fault miss fails; each automatic Phase-A regression
trigger disables switches atomically and leaves audit record; policy engine
queues but never directly applies; operator can inspect exact reasons.

Required author, no AI trailers/internal tracker labels. Do not push. Final:
`LANE_RESULT: done <summary> | blocked <reason>`
