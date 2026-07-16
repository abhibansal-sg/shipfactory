<!--
Donor: https://github.com/gsd-build/get-shit-done
File: get-shit-done/templates/continue-here.md
Upstream SHA: bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815
License: MIT
Deltas: Replaced phase/task file state with Factory instance/step identifiers and kanban comment fields; made the note ephemeral and consumable by a RESUMED marker instead of a disk file.
-->

# Continue-here resume note

```markdown
CONTINUE-HERE
Instance: <instance id>
Step: <step id>
Status: blocked / needs_input
Updated: <ISO-8601 timestamp>

## Where We Are
<Immediate context and why the instance is parked.>

## Done
<Completed upstream summary one-liners, with step ids.>

## Left
<Work that remains after the gate or event is satisfied.>

## Decisions and Why
<Decisions already made and their rationale; write “None recorded” when empty.>

## Blockers
<The exact approval or event being awaited and what answer unblocks it.>

## Next Action
<The first concrete action after resume.>
```

The note must be specific enough that a fresh seat instance understands immediately. It is ephemeral: after gate consumption, append `RESUMED <ISO-8601 timestamp>` to the same kanban task; consumers must treat the earlier note as deleted.
