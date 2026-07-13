<!--
Donor: https://github.com/github/spec-kit
File: templates/checklist-template.md; templates/commands/specify.md
Upstream SHA: 6664cf813cb943fd9ac0ab2aab60c11798913c13
License: MIT
Deltas: Adapted feature checklist language to Factory artifacts; retained requirements-as-unit-tests doctrine; removed Spec Kit paths, generated examples, and implementation workflow.
-->

# Requirements checklist

A requirements checklist tests the quality of the written artifact, not the implementation. Every item must be answerable **yes** or **no** by quoting the artifact text.

Use this shape:

```markdown
## <Quality dimension>

- [ ] CHK001 Is <requirement quality> specified in measurable terms? [Quote/§reference]
- [ ] CHK002 Are <scope or edge conditions> explicitly bounded? [Quote/§reference]
```

Good items expose ambiguity, omissions, conflicts, unmeasurable adjectives, missing edge cases, or absent acceptance criteria:

- “Is ‘prominent’ quantified with size, position, or contrast?”
- “Are retry limits and exhausted-retry behavior stated?”
- “Can every required behavior be traced to a Done criterion?”

Never test runtime behavior or implementation:

- Not “Does the button work?”
- Not “Do unit tests pass?”
- Not “Is the API implemented?”

Treat the checklist as unit tests for requirements prose: a failed item identifies the exact text that must be clarified before execution.
