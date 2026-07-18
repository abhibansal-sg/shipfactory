# Concept of Record — Amendment 2 (v2.2)

**Status: RATIFIED BY OPERATOR (Abhi), 2026-07-18 (verbal, in-session).**
**Date:** 2026-07-18. Product of the operator/Claude scale-vision discussion
(the "10,000-task SaaS" question). Amends CONCEPT-OF-RECORD.md (v2) and
Amendment 1 (v2.1). Where this amendment conflicts with either, this
amendment governs.

**Governing constraint, operator-stated:** a beautiful simple solution that
just works. Every item below is reuse plus at most one small new part. Any
future design that adds machinery where composition would do violates this
amendment.

---

## A. The decomposition artifact (makes §3 brake 1 buildable)

Depth-N is ONE mechanism: **a plan-type step whose sealed output artifact
proposes child work items.** Each child names its recipe — a stock library
reference (`id@N`) or an inline recipe document. The artifact:

1. is validated by the SAME loader that validates stock recipes (schema law,
   machine-enforced — a malformed or rule-breaking recipe cannot be emitted);
2. passes the SAME hostile review gate every plan passes today;
3. on approval, is instantiated with the EXISTING `instantiate(parent_tasks=…)`
   containment, children rolling up through the EXISTING collector machinery.

No new primitives of trust: validation, review, instantiation, and rollup all
already exist. The new part is one artifact schema
(`shipfactory.decomposition/v1`) and the advancer applying it. Because a child
journey may itself contain a plan-type step, depth-N follows with zero
additional machinery — the fractal rule realized by composition.

Reconciliation with Amendment 1 item C: unchanged. Ephemeral swarm workers
inside one card remain harness-native with a receipt. The decomposition
artifact governs the OTHER grade — durable child journeys on the board.
Two grades, one line: **inside a card, swarm freely; below a card, only
through a reviewed decomposition.**

## B. Recipes say WHAT and WHO-BY-ROLE; seats say HOW (recipe schema law)

The recipe schema gains NOTHING for scale. A recipe declares: steps in order,
`needs` dependencies, gates, per-step instructions, input/output artifact
contracts, count caps — and each AI step names a **seat by role**
(builder, reviewer, architect…). The seat binds harness + model + reasoning +
profile (skills, tools). Per-step capability is resolved through this one
join, never inlined into recipe documents.

Rationale: inlining models/tools into thousands of recipe steps rots on the
first provider change. Swapping a model in one seat file upgrades the entire
library. This division already exists and flew (first-light-14); it is hereby
law. A free consequence: because steps name seats by role, outcome evidence
aggregates per seat across every recipe — cross-recipe learning signals for
the curator (item E) with zero added machinery.

## C. The law for generated recipes (amends nothing; extends §7 item 8 / A1-D)

An agent may author a recipe — inline in a decomposition artifact or via the
recipe-designer (item D). The loader (code, never prompt text) enforces the
non-negotiables on every generated recipe:

1. it MUST contain a review gate;
2. it MUST NOT declare an approval gate (Amendment 1 item D: machine-generated
   workflows can structurally never summon the operator);
3. control-plane paths carry their mandatory risk tags, exactly as today.

Inline recipes are **ephemeral by default**: they live only in their journey's
sealed artifacts. Promotion to the library is never automatic — and its
trigger is now defined: **recurring inline shapes are the promotion signal.**
When similar inline recipes keep appearing across journeys, the curator
(item E) files a promotion proposal through the recipe-designer journey. The
library grows only from proven patterns.

## D. The recipe-designer is a recipe, with two intake sources

Recipe authoring with agent help is not a feature; it is a library entry.
A `recipe-designer` recipe: agent drafts recipe YAML → loader validates →
hostile review attacks it → operator approves → published as `@1` (or `@N+1`
with `supersedes`). The factory builds its own tooling through its own gates.

Two intake sources, adopted from the Hermes skill-review mechanism:

1. **Demand-driven** — the operator asks, or no stock recipe fits a task.
2. **Experience-driven** — the curator (item E) reviews completed journeys
   and files proposals from evidence: a step rejected in most runs, an
   inline shape recurring, a recipe that always sails through. Hermes reads
   conversation history and infers; we read journey outcomes and know.

**Anti-sprawl preference order (law, adopted from the skill-review prompt):**
prefer proposing `@N+1` of an existing recipe → extending an existing recipe
→ only lastly a NEW recipe. Target shape of the library: class-level
recipes, never a long flat list of narrow one-journey-one-recipe entries.

One deliberate tightening over Hermes: their background reviewer writes
skills directly; our curator NEVER writes the library — every proposal is a
recipe-designer journey through the gates. Same learning loop, stricter
custody.

## E. The recipe curator (repurposes the Hermes skill-curator pattern)

Adopt the skill curator's lifecycle, improved by data ShipFactory already has:

- **Usage is free and evidence-based.** Every `recipe_instances` row is a
  usage record WITH an outcome (approved / rejected / stalled / cap-blown).
  Lifecycle judgments use evidence, not just recency.
- **States ride the existing schema.** `status: active → stale → archived`
  (archived = selector stops matching; `archived` becomes a valid status).
  Published bytes are never deleted — immutability law unchanged. Pinning
  protects a recipe from lifecycle transitions.
- **Provenance-scoped custody (Hermes invariant, adopted).** Recipes carry an
  origin mark (operator-authored vs agent-authored/promoted). Automatic
  lifecycle transitions touch ONLY agent-authored recipes; operator-authored
  recipes change state only by operator action.
- **Improvement is a proposal, never an edit.** The curator files a
  recipe-designer journey proposing `@N+1` (`supersedes: @N`) through the
  normal gates. The curator has no direct write access to the library.
- **Trigger model (Hermes pattern, no cron):** a post-journey review when an
  instance reaches a terminal state, plus idle daemon-tick maintenance for
  lifecycle transitions.

v1 scope: usage view, stale/archive transitions, pin, provenance mark. LLM
consolidate/improve passes come later and arrive as journeys.

## F. Sequencing (ratified order)

1. **Engine first** — the decomposition artifact (item A). A wrong tree is
   worse than an unrendered one.
2. **Board second, schema-informed.** Before building A, fix the three board
   queries the containment schema must answer cheaply: (i) every node
   awaiting the operator, across the whole tree; (ii) fold/unfold a subtree
   ("vertical") without loading it; (iii) a root's rollup state. The engine
   records what the explorer will need; the explorer is built after.
3. **Control last, by operator decision.** Noted honestly: Amendment 1
   deferred depth/width caps because "nothing engine-visible exists to cap."
   Item A creates the engine-visible thing. The deferral reason expires when
   A ships; the control discussion reopens then — and not before.

## G. The simplicity law (summary)

The entire scale vision = existing machinery plus exactly:
one artifact schema (A) + one loader rule set for generated recipes (C) +
one recipe (D) + one status value, one provenance mark, and a small curator
loop (E). Anything beyond this list requires its own amendment.
