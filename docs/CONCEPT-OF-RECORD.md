# ShipFactory — Concept of Record (v2)

**Status:** RATIFIED BY OPERATOR (Abhi), 2026-07-18.
**This document supersedes any prior architectural description that conflicts with it.**
**Any implementation that contradicts this document is a defect, not a design choice.**

---

## 1. What ShipFactory is

ShipFactory turns a plain-English prompt into a finished, tested, independently
reviewed change waiting for **one human approval**. The human approval gate is
the product's defining feature. Nothing ships without the operator's explicit
press. No agent may synthesize approval, release, or deployment.

It is built as a **plugin on stock Hermes Agent**: stock gateway, stock Kanban,
ShipFactory adds the recipe engine, seats, verification, evidence, and the
approval surface on top. No forked Hermes core. That constraint is permanent.

Endgame (shared conceptually with Vorflux's manifesto, arrived at
independently): work fans out the moment it is defined; nothing waits on a
human except judgment. The queue Vorflux kills is the *human-wait* queue —
work waiting its turn behind one person because dispatch was manual and
execution was serial. ShipFactory removes both while keeping one judgment
point: the operator's approval card. The board is not a queue; it is the
**trust surface** that lets the operator verify work they did not watch.

Asset first. If the asset works, the same machine is rentable as a service
later (the Vorflux path), with us as its first and canonical customer.

---

## 2. The two-layer architecture (the original brief)

This is the operator's original brief from the Paperclip → Hermes transition.
It was drifted from once. It must not be drifted from again.

### Layer 1 — Workflow recipe (across cards)

A workflow recipe defines which **durable cards** exist for a journey and how
they depend on each other:

```
Explore → Specify → Plan → Build → Integrate → Approve
```

The recipe — never an agent — decides which cards exist, in what order, with
what dependencies. Recipes are immutable once published; they are replaced by
new versions, never edited.

### Layer 2 — Task recipe (inside one card)

Every card carries its own configurable role chain, composed per card:

```
Assignee → [Verifier] → [Reviewer] → [Approver]
```

Stages are optional and repeatable (`Assignee → Reviewer` is valid;
`Assignee → Verifier → Reviewer → Human Approver` is valid).

**The unbreakable rule: one card ID survives the whole chain.**

- If the reviewer rejects, the SAME card returns to the assignee with the
  feedback. No replacement card. No erased history.
- Every attempt (builder run, verifier run, review verdict, rework) is
  recorded inside the card as immutable history.
- The card is Done only when its full chain passes.
- A card must never silently change role identity (builder card becoming a
  reviewer card), and a historical failure must never visually dominate a
  later healthy attempt.

### Current-state honesty (as of 2026-07-18)

- The single-card chain machinery exists (`shipfactory/policy.py:138-184`,
  tested in `tests/test_policy.py`) but is **bypassed when recipes are
  enabled** (`policy.py:239-240`).
- The recipe engine instead flattens inner roles into separate top-level
  Kanban cards. This is the architectural defect to correct.
- Parent/child plumbing already exists and is cycle-safe
  (`hermes_cli/kanban_db.py` `task_links` table, `link_tasks`,
  `parent_ids`/`child_ids`, done-parent results). The recipe engine already
  passes dependency parents (`advancer.py:1279-1285`) and
  `instantiate(parent_tasks=...)` can already hang a whole recipe instance
  under an existing card. The links currently express only **dependency**;
  they must also express **containment** (a parent card containing its role
  chain and child cards).

The correction is composition, not construction: make every workflow card
carry its own task recipe, and render the board as parents with children
folded inside.

---

## 3. The fractal rule

There is exactly ONE building block, repeated at every scale:

> **Card = work + internal role chain.**
> A card's assignee either does the work directly (leaf card) or expands into
> a child workflow of smaller cards of the same shape.

No new machinery exists at depth N. Depth N is cards inside cards. An
"enormous" project (frontend basket, backend basket, API basket, each with its
own waterfall) is the same shape as a one-line fix — just deeper.

### The brakes (mandatory, machine-enforced)

Unbounded recursion is how autonomous systems die. Four brakes, enforced in
code, never in prompt text:

1. **Decomposition is a planning decision.** Only a plan-type step may propose
   child cards, and that plan passes a hostile review gate before any child is
   created. A builder mid-task can never spawn helpers as siblings on the
   board (its *internal* throwaway subagents are its own business — see §4).
2. **Depth and width are recipe-enforced numbers.** Max depth, max children
   per card. The engine refuses card N+1; the model is never asked to be
   reasonable.
3. **Budget is hierarchical.** Parent cards carry token budgets; children draw
   down from the parent. Explosion runs out of money before it runs out of
   control.
4. **Done rolls up, never sideways.** A parent is Done only when all children
   passed their own chains AND the parent's integration verification proved
   the pieces work together. Children never land independently; only the root
   reaches the operator's approval card.

---

## 4. The dynamic workflow mechanism (extracted from Claude Code ultra sessions)

Observed repeatedly in Claude Code sessions running ultrathink/"ultra" mode
with high parallelism (as used from Paperclip): the session does not follow a
fixed pipeline. It **generates its own phase program per task**, then executes
it in one go. Extracted mechanism:

### 4.1 What actually happens

1. **Research first.** The session studies the subject (repo, docs, web)
   before committing to a structure.
2. **It emits a phase program** — 2 to 7+ phases, shaped to the task, e.g.:
   - Phase 1: **swarm** — 20–150 parallel agents, each with a tiny scoped
     brief and a fresh context window, fanned across files/areas/questions.
   - Phase 2: **consolidate** — few agents merge/dedupe/rank the swarm output.
   - Phase 3+: further phases as needed.
3. **The phase graph is not fixed.** Observed orderings include:
   - swarm → consolidate → consolidate (funnel)
   - swarm → **critique** → swarm (round 2, narrower) → consolidate →
     **synthesize** (diamond: expand, attack, re-expand informed by the
     attack, then converge)
   - research → swarm → build-swarm → verify → synthesize
4. **Each phase's output is the next phase's input.** Contexts stay fresh:
   workers never inherit the whole transcript, only their brief + upstream
   artifacts. (Vorflux's context-window-aging point: one giant session is one
   aging mind; a team of fresh ones stays sharp.)
5. **The whole program runs in one go** — no human between phases.

### 4.2 The phase vocabulary

Five phase types cover everything observed:

| Phase type | Fan | Purpose |
|---|---|---|
| **research** | 1–few | Establish ground truth before structuring |
| **swarm** | many (20–150) | Parallel scoped exploration/production, fresh context each |
| **critique** | few, adversarial | Attack the current state; output = ranked defects/gaps |
| **consolidate** | few | Merge, dedupe, rank, resolve conflicts between outputs |
| **synthesize** | 1 | Produce the single coherent final artifact |

A **dynamic workflow = a small DAG over these five phase types**, generated
per task, where each node declares its fan-out, its input artifacts, and its
output contract.

### 4.3 How this maps into ShipFactory

The phase program is exactly a **generated child workflow** under one card —
the fractal rule already covers it. Mapping:

- The card's assignee (planner-type step) emits a **phase program artifact**:
  the DAG of typed phases with fan-outs, briefs, and output contracts.
- That artifact passes the hostile review gate (brake #1) — the phase program
  is *reviewed before it runs*, exactly like any plan.
- The engine then instantiates the phases as child cards / activations under
  the parent card, executing swarm members as parallel bounded workers with
  fresh contexts, subject to depth/width caps and the parent's budget
  (brakes #2 and #3).
- Phase outputs are sealed artifacts; the next phase's workers receive only
  their brief + the sealed upstream artifacts (fresh-context law).
- The parent card is Done when the final synthesize phase's output passes the
  parent's own chain (brake #4).

**Two grades of parallel work, distinguished deliberately:**

- **Ephemeral swarm workers** (inside one phase): not top-level board cards.
  They are activations/attempts recorded UNDER the phase card — auditable in
  its history, invisible as board clutter. A 100-agent swarm must not create
  100 cards on the operator's board.
- **Durable cards** (phases, baskets, work items): board-visible, chain-
  carrying, history-preserving.

This distinction is what keeps the board a trust surface at swarm scale.

### 4.4 What ShipFactory adds that raw Claude Code lacks

Claude Code's dynamic workflow is brilliant and unaccountable: no immutable
record of the phase program, no independent review of it, no budget wall, no
evidence sealing, no cross-provider critique, no operator gate. ShipFactory
keeps the mechanism and adds the governance: the phase program becomes a
reviewed, sealed, budgeted, replayable artifact, and its execution leaves an
audit trail a stranger can read.

---

## 5. Provenance of the concept (consolidated learnings)

### From Warflux (the earlier attempt)
1. Prompts are not enforcement — rules live in code the agent cannot bypass.
2. Agents drift — small bounded steps with mechanical checks after each.
3. Failures are preserved, never relabeled — else the system cannot be audited.

### From the ChatGPT Pro hostile-review lane
1. Immutable recipes (versions replace, never edit).
2. Evidence binds to exact SHAs; amended code invalidates old evidence.
3. Independent review from a different provider family than the builder.
4. Fail-closed everything: malformed approval = rejection; silence ≠ pass.
5. Human approval is a card for the operator, never a model step.

### From the Vorflux manifesto (studied conceptually, 2026-07-18, not copied)
1. Six bottlenecks between prompt and merged PR: machine, planning,
   orchestration, testing, review, merge. Kill each by profiling it and
   spending tokens on it.
2. The babysitting moved up a level: you supervise the *system*, not the
   session. The board answers "can I trust a merge I didn't read."
3. Code that never ran is a guess — testing is cleared by **watching**
   (browser driven through real flows, recording left behind). → ShipFactory
   should attach a watchable artifact to the approval card, not just logs.
4. Plan deserves the most tokens, not the fewest (plan-of-plans, attacked
   cross-lab before build). Matches our spec-attack/plan-attack steps.
5. The harness is the only durable asset: labs can never ship your judgment
   or neutrality. Encode judgment once; it runs losslessly forever.
6. Recurring discipline: every few weeks, profile where the operator's time
   still leaks; that is where the next recipe improvement belongs.
7. Deliberate divergence: Vorflux's endgame flips the last gate (auto-merge,
   auto-flag while you sleep). ShipFactory keeps the operator's approval gate
   irreversible and exclusively human. Same endgame otherwise; asset now,
   service later if proven.

### From the live shakedowns (SF-1 → SF-11, twenty merged PRs)
- Recipe engine + immutable `dev-pipeline@8`, seats (`seats.yaml`) mapping
  roles to real harness/model/reasoning with token ceilings and access modes.
- Real verification: deterministic floors + Playwright browser evidence,
  sealed per SHA. Cross-provider review law (Codex builds, Claude reviews).
- Defects found by dogfooding and fixed through the normal pipeline: external-
  volume SQLite corruption, repository-wide staging (finding #69 → PR #20),
  protocol-invalid review verdicts, disk-I/O spawn failures, stale-WAL reads.
  All preserved, none relabeled.
- Known open defects: flattened card composition (§2), Kanban WAL lifecycle
  instability, `/shipfactory` dashboard plugin registration failure, brittle
  prose-only review-verdict contract (needs structured `clean: true` field,
  fail-closed).

---

## 6. The twelve-step prompt-to-PR recipe (current journey of record)

1. Explore the Intent
2. Draft the Task Specification
3. Attack the Task Specification
4. Draft the Implementation Plan
5. Attack the Implementation Plan
6. Build the Approved Plan
7. Verify the Candidate Revision *(deterministic — no model)*
8. Review Candidate Correctness
9. Adversarially Review the Verified Candidate
10. Produce the Complete Review Story
11. Approve the Exact Reviewed Revision *(human only)*
12. Notify That the Reviewed Candidate Is Parked *(deterministic — no model)*

Every AI step shares one input format: machine-enforced configuration (seat,
harness, model, reasoning, access, workspace, limits, dependencies, output
schema) + natural-language instructions. Configuration is enforced by the
engine, never by prompt text.

Under the two-layer correction, these steps become the internal stages and
child cards of ONE parent journey card, not twelve sibling cards.

---

## 7. Non-negotiables (summary law)

1. Plugin on stock Hermes. No fork.
2. Two layers: workflow recipe across cards; task recipe inside each card.
3. One card ID survives its chain; rework returns to the SAME card.
4. History immutable; current attempt visually distinct from failed history.
5. Decomposition only via reviewed plans; depth/width/budget caps in code.
6. Ephemeral swarm workers are attempts under a card, never board cards.
7. Evidence sealed, SHA-bound, watchable where possible.
8. Cross-provider independent review; fail-closed verdict contracts.
9. Done rolls up; only the root reaches the approval card.
10. The approval card is the operator's alone. No auto-release. Ever.
