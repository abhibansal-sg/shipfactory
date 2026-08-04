# GraphRecipe v2 contract

GraphRecipe v2 extends the immutable v1 execution recipe with explicit workspace and capability policy. Existing v1 documents remain valid and unchanged.

```yaml
version: 2
name: issue-build-and-review
start: builder

capability_sets:
  coding:
    skills: [test-driven-development, systematic-debugging]
    toolsets: [terminal, file, github]
    plugins: [gbrain]

boxes:
  - id: builder
    name: Builder
    who: builder
    instructions: Implement the issue and produce a candidate with evidence.
    workspace:
      lane: build
      access: write
    capabilities: coding

  - id: delivery
    name: Delivery
    who: release-manager
    instructions: Deliver the approved candidate.
    workspace:
      lane: build
      access: read
    capabilities: coding
    end: true

arrows:
  - from: builder
    result: done
    to: [delivery]
```

## Exact grammar

- v2 top level: `version`, `name`, `start`, `capability_sets`, `boxes`, `arrows`; optional `approval_artifact` remains supported.
- `version` is exactly integer `2`.
- A capability-set key is a non-empty identifier matching `^[a-z][a-z0-9-]*$`.
- Every capability set has exactly `skills`, `toolsets`, and `plugins`.
- Each capability list contains unique non-empty strings. Empty lists are valid.
- Every v2 Box has exactly `id`, `name`, `who`, `instructions`, `workspace`, and `capabilities`; an End Box additionally has `end: true`.
- `workspace` has exactly `lane` and `access`.
- `workspace.lane` is a non-empty string.
- `workspace.access` is exactly `read` or `write`.
- `capabilities` references an existing top-level capability-set key.
- Arrow grammar, reachability, one-End invariant, result labels, splits, joins, and backward-arrow rework semantics are unchanged from v1.

## Runtime snapshot

- Recipe publication freezes logical workspace lanes and exact capability-set definitions.
- Run launch resolves one physical worktree per referenced lane and freezes effective access.
- Box activation receives the resolved path and capability allowlists; it makes no new policy choice.
- An executor that cannot enforce the requested policy must fail closed.

## Visual adapter

- Export preserves Box declaration order because GraphRunner uses declaration order to classify backward arrows.
- Multiple React Flow edges sharing `(source, result)` export as one arrow with ordered destinations.
- Import expands each arrow destination into one React Flow edge.
- Trigger configuration and canvas positions are visual/launch metadata, not GraphRecipe fields.
- Importing v1 supplies explicit editor-only legacy defaults; exporting the edited workflow produces v2.
