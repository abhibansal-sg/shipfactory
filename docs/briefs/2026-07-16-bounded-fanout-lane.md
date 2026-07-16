<!-- Bounded fan-out lane. Source: external program review §2.3 and §5.1 order 9. -->

# LANE BRIEF — bounded child-instance fan-out + immutable dev-pipeline@8

Run only after serialized release and rollback-capable deployment are merged
and proven. Fresh current-main clone, branch `lane/bounded-fanout`.

Read `AGENTS.md`; review §2.3.1–§2.3.10; selector child-instantiation code;
recipe loader/advancer/store/budget/resource leases; release integration.
Baseline current main; full suite ×2.

## Architecture law

Reject same-instance runtime graph mutation. Parent graph remains static and
pinned. Expansion creates deterministic CHILD recipe instances under one
expansion group and completes an existing static parent wait step. This is an
explicit §17 amendment, not a second hidden workflow engine.

## Scope

1. Add normative schema: `instance_relations`, `budget_groups`,
   `budget_reservations`, `plan_expansions`, `plan_expansion_nodes`; add
   budget-group columns to instances/charges. Numbered migrations with
   partial-application guards.
2. Add top-level v2 `expansions` declaration (§2.3.2) and immutable
   `dev-pipeline@8`; max_children=2; static `wait_for_event` completion step;
   @1..@7 byte-identical.
3. Expansion state machine exactly §2.3.3. Child ID =
   sha256(expansion_id|graph_hash|node_id). Materialize at most one child per
   action intent; all kanban collector/link effects outside Factory txns with
   probe-before-retry.
4. Hard initial caps: 2 children, 2 simultaneous write workers, 1 app
   session, 1 browser session. Raise only from measured headroom, never model
   output.
5. Every write-capable child receives an independent local clone and `.git`.
   Change-set must bind base/head/tree/commits/changed_paths/allowed_paths;
   any path outside allowed set blocks integration.
6. Deterministic integration clone: pinned base; topological then lexical
   child order; exact validated commits; verify tree/full manifest; emit
   integration change-set. Conflict emits a conflict artifact — never agent
   resolution inside release. A separate fix activation re-enters full
   verification/review/approval.
7. Immutable routing policy maps plan kind→capability-qualified seats. Record
   requested seat, configured model, actual provider/resolved model,
   executor version, profile hash, and trust domain; seat names alone never
   prove cross-lab independence.
8. Reserve worst-case first activation for every child build/review, parent
   integration, parent verification under BEGIN IMMEDIATE against group and
   board-day ceilings. Consume reservation into non-refundable admission;
   only pre-activation cancellation may release.
9. Cancellation follows reverse topology, waits for confirmed exits, retains
   artifacts/collectors, and never lets archived collectors satisfy outer
   dependencies.

## Mandatory adversarial tests — every §2.3.10 case

Concurrent controllers materialize same graph; crash each child boundary;
collector without relation and relation without collector; cycle; 3 nodes at
cap 2; undeclared same-file overlap; `.git/config` change; extra refs outside
namespace; concurrent board-budget consumption; cancel during child commit;
foreign-clone change-set SHA; one child blocked/one succeeds; integration
order stable over restart; conflict cannot drop a child; FD/resource
exhaustion leaves Factory DB healthy; parent cannot complete early; stale
completion event cannot target newer plan activation.

Required author, no AI trailers/internal tracker labels. Do not push. Final:
`LANE_RESULT: done <summary> | blocked <reason>`
