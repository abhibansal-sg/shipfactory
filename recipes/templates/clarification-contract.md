<!--
Donor: https://github.com/github/spec-kit
File: templates/commands/specify.md; templates/commands/clarify.md; templates/spec-template.md
Upstream SHA: 6664cf813cb943fd9ac0ab2aab60c11798913c13
License: MIT
Deltas: Reduced Spec Kit's interactive specification workflow to a Factory selector/artifact contract; lowered the selector-visible cap to the ratified maximum of three; omitted constitution and disk workflow mechanisms.
-->

# Clarification contract

Mark only decisions with materially different outcomes and no safe default:

```text
[NEEDS CLARIFICATION: <specific question>]
```

An artifact may contain at most 3 markers. Prioritize them in this order:

1. Scope and required behavior
2. Security and privacy
3. User experience
4. Technical details

Use an informed default when impact is low: make the best context-supported guess and document it under `## Assumptions`. Do not spend a clarification marker on wording, style, or a reversible implementation detail.

Any unresolved marker means the artifact is not gate-passable. Selectors must park the source task as `blocked(kind=needs_input)` and must never instantiate work containing a marker.
