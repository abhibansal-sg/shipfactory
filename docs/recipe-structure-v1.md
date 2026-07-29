# Simple Recipe — Paper Prototype

**Status:** design draft; not current engine syntax  
**Purpose:** prove the ratified box-and-arrow structure against the operator's original graph before changing engine code.

## The complete recipe

```yaml
name: plan-build-review
start: planner

boxes:
  - id: planner
    name: Plan the work
    who: planner
    instructions: Understand the request and produce a clear plan.

  - id: plan-review
    name: Review the plan
    who: plan-reviewer
    instructions: Check whether the plan is clear, complete, and suitable to execute.

  - id: builder
    name: Do the work
    who: builder
    instructions: Execute the approved plan and produce the requested work.

  - id: correctness-review
    name: Check correctness
    who: correctness-reviewer
    instructions: Review the completed work for correctness and report findings.

  - id: risk-review
    name: Check risk
    who: risk-reviewer
    instructions: Review the completed work for important risks and report findings.

  - id: simplicity-review
    name: Check simplicity
    who: simplicity-reviewer
    instructions: Review the completed work for unnecessary complexity and report findings.

  - id: synthesize
    name: Combine the reviews
    who: lead-reviewer
    instructions: Combine all active review findings and decide whether the work passes or needs correction.

  - id: human-approval
    name: Human approval
    who: human
    instructions: Review the completed work and combined review before approving or rejecting it.

  - id: final-delivery
    name: Deliver the work
    who: main-orchestrator
    instructions: Deliver the approved work and report completion to the project.
    end: true

arrows:
  - from: planner
    result: done
    to: [plan-review]

  - from: plan-review
    result: approved
    to: [builder]

  - from: plan-review
    result: revise
    to: [planner]

  - from: builder
    result: done
    to: [correctness-review, risk-review, simplicity-review]

  - from: correctness-review
    result: done
    to: [synthesize]

  - from: risk-review
    result: done
    to: [synthesize]

  - from: simplicity-review
    result: done
    to: [synthesize]

  - from: synthesize
    result: pass
    to: [human-approval]

  - from: synthesize
    result: rework
    to: [builder]

  - from: human-approval
    result: approved
    to: [final-delivery]

  - from: human-approval
    result: rejected
    to: [builder]
```

## What happens when it runs

```text
Request
  ↓
Planner
  ↓
Plan review ── revise ──→ Planner
  │ approved
  ↓
Builder
  ├─→ Correctness review ─┐
  ├─→ Risk review ────────┼─→ Combine reviews
  └─→ Simplicity review ──┘         │
                                    ├─ rework → Builder
                                    └─ pass
                                         ↓
                                  Human approval
                                    ├─ rejected → Builder
                                    └─ approved
                                         ↓
                                  Final delivery [END]
```

## Generic rules supplied by ShipFactory

These rules belong to the runner and are not repeated inside every recipe:

1. A box receives the request plus work from its active preceding boxes.
2. A completed box returns its work plus one result label.
3. ShipFactory follows the arrow matching that result.
4. One result may start several boxes together.
5. A join waits for every active incoming path, then receives their combined work.
6. A human box pauses until the operator chooses its result.
7. An End box must be explicitly marked and has no outgoing arrows.
8. Three consecutive technical failures pause and report to the project's main orchestrator.
9. Three rejected correction rounds through the same rework loop pause and report to the project's main orchestrator.
10. Each run keeps the exact recipe copy it started with.

## Completeness check

| Required behaviour | Where this recipe proves it |
|---|---|
| One starting box | `start: planner` |
| Normal sequence | Planner → plan review → builder |
| Result-based branch | Plan review: `approved` / `revise` |
| Backward rework | Plan review → planner; synthesis → builder |
| Parallel split | Builder → three reviews |
| Active-path join | Three reviews → synthesis |
| Human-only decision | Human approval |
| Explicit completion | Final delivery marked `end: true` |
| Technical-failure brake | Generic rule 8 |
| Rework-loop brake | Generic rule 9 |
| Frozen running structure | Generic rule 10 |

## Structure verdict

The original graph fits without special planning, build, review, verification, collector, or approval primitives. It needs only:

- recipe name and starting box;
- ordinary work boxes (`name`, `who`, `instructions`);
- result-labelled arrows;
- one explicit `end: true` marker;
- generic runner rules for joins, retries, escalation, and frozen runs.
