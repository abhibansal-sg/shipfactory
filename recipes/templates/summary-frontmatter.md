<!--
Donor: https://github.com/gsd-build/get-shit-done
File: get-shit-done/templates/summary.md
Upstream SHA: bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815
License: MIT
Deltas: Adapted phase/plan metadata to Factory instance/step vocabulary; reduced the body to the recipe step-output contract; made token usage optional.
-->

# Step output contract

Every completed `agent_task` writes a summary with this frontmatter:

```yaml
---
step_id: <recipe step id>
activation: <positive integer>
subsystem: <primary area, such as auth, api, database, infra, or testing>
tags: [<searchable technical terms>]
requires: [<upstream artifact or capability dependencies>]
provides: [<artifacts or capabilities delivered>]
affects: [<downstream step ids, subsystems, or keywords>]
key_files: [<important created or modified paths>]
key_decisions: [<decision and brief rationale>]
tokens_used: <integer or omit when unknown>
---
```

Follow it with a substantive one-liner. Say what shipped, for example, “JWT auth with refresh rotation using jose,” never “auth implemented” or “step complete.”

## Task Commits

List each task commit as `<subject> — <short SHA>`, or `None` when the step did not create commits.

## Files Created/Modified

List each path and its purpose. Do not hide generated, deleted, or configuration files.

## Deviations

List every deviation as `[Rule N - category] description`, matching the numbered rules in `executor-discipline.md`. If none, say `None — executed within the requested scope`.

## Issues

Record encountered and deferred issues. Pre-existing failures belong here and must not be silently fixed.

## Next-Step Readiness

State what downstream steps can rely on, any blocker, and the first useful next action.
