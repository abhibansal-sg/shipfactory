# Concept of Record — Amendment 1 (v2.1)

**Status: RATIFIED BY OPERATOR (Abhi), 2026-07-18 (verbal, in-session).**
**Date:** 2026-07-18. Product of the operator/Claude vision discussion for SF-17.
Amends CONCEPT-OF-RECORD.md (v2, ratified 2026-07-18). Where this amendment
conflicts with v2, this amendment governs.

**Operator-stated endgame (recorded for the roadmap, not this MVP):**
auto-merge is the eventual goal — the ShipFactory ladder ends where Vorflux's
does. The MVP keeps the human approval gate; each delivery below must be a
rung toward earned auto-merge (trust via gates), never a detour from it.

---

## A. The one-card law is a presentation law (amends §2)

The trust surface shows ONE logical card per unit of work; rework visibly
returns to that same logical card with the rejecting verdict attached, and
attempts stack under it as immutable history.

Storage is explicitly NOT required to reuse one mutable kanban row. The
engine's per-activation immutable tasks — idempotency keys, crash-recovery
re-activation, review-input body binding, producer-run fencing — are preserved
as-is. The board folds physical attempts into the logical card; the substrate
keeps its audit spine.

## B. Containment is a ShipFactory overlay (amends §2)

Parent/child containment is recorded in shipfactory.db (ShipFactory-owned
overlay), which is authoritative for rendering. Stock Hermes kanban remains
the execution substrate, untouched: no `task_links` schema change, no reliance
on Hermes' dormant forward-compat columns. Dependency edges keep their stock
semantics.

The folded parent/child view lives in the `/shipfactory` dashboard tab. The
stock kanban tab is not overridden.

## C. Dynamic workflow = harness-native, with a receipt (amends §4)

A card's assignee may fan out internal parallel subagents inside its own
harness session — fresh contexts, scoped briefs, enforced output contracts —
exactly as §3 already permits ("its internal throwaway subagents are its own
business"). The engine contracts only the card's final artifact, which passes
the normal gates (verification, cross-provider review, approval) unchanged.
Trust comes from the gates, not from watching the process.

**The receipt:** the harness's execution journal (per-worker briefs, results,
transcripts as produced by the harness) is preserved and attached to the
card's history so a stranger can audit how a swarm ran after the fact.

**Explicitly deferred** until dogfooding shows the receipt is insufficient:
engine-visible phase programs, pre-run hostile review of fan-out plans, phase
child cards, the five-phase vocabulary as law, and depth/width caps (nothing
engine-visible exists to cap).

## D. Approval granularity (amends §3 brake 4 and §7 item 9)

Approval gates exist only where a HUMAN-AUTHORED recipe declares them — one
gate per independently shippable unit. Machine-generated workflows may never
declare an approval gate. "Only the root reaches the approval card" is
replaced by this rule: fragments can structurally never summon the operator;
independently shippable units each get their own gate as their recipe
declares. The gate itself remains the operator's alone (§7 item 10 unchanged).

## E. Budgets: no new machinery (amends §3 brake 3)

Zero new budget work. Existing flat per-instance budgets (activation caps,
token pools, board-day ceiling) remain exactly as-is. Hierarchical
parent→child draw-down is removed from the vision.

## F. Verdict contract v2 (confirms §5 open defect, scopes the fix)

Structured, fail-closed, machine-readable verdicts (explicit clean/findings/
target fields), shipped as dev-pipeline@9 per the immutability law. One
change closes four holes: prose-regex approve exemption, uncountable
finding-count stall-detector bypass, citation-less `request_changes`
acceptance, and malformed-verdict block reasons with no operator release
path. Judgment call adopted: cross-provider independence enforcement extends
to plan/spec attack reviews, not only change-set reviews.

## G. Delivery order (ratified sequencing)

1. **Dashboard registration fix** — `dist/index.js` registers `"factory"`,
   manifest says `"shipfactory"` (rename-orphaned since 2026-07-15); fix the
   name and add a minimal guard so bundle and manifest cannot drift again.
2. **Daemon WAL connection lifecycle** — replace the daemon's long-lived
   per-board connections (stale-WAL reads) with a fresh-per-tick or
   explicit-invalidation scheme.
3. **Verdict v2 + dev-pipeline@9** — item F.
4. **Composition correction** — containment overlay, folded `/shipfactory`
   rendering, logical-card rework routing (items A/B), swarm receipt (item C).

Each lane ships as small PRs through the normal pipeline; suite stays green.

## H. Verification obligation

Before relying on review-input inlining during lane 3/4 work: verify whether
the deployed Hermes `build_worker_context` caps task bodies (8KB observed in
one checkout), which would truncate inlined sealed review inputs while the
body-binding check still passes. If real, fix the delivery channel for review
inputs as part of lane 3.
