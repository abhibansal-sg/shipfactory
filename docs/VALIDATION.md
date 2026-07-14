# Validation

Lane V1 exercises Factory through the real Hermes kanban database and real
subprocesses. The only fake is the external AI harness: a temporary `/bin/sh`
script emits captured-style Codex output (`tokens used\n1,234`) and its final
`HEADFRAME_RESULT` protocol line.

Proven paths:

- Kanban board creation, task dispatch, real `headframe_spawn`, process polling,
  result parsing, task transition, and durable run telemetry.
- Successful and blocked sentinels, including exit-0 without a sentinel being
  blocked as `no result sentinel`.
- Policy reopening via the installed Hermes CLI, citation-gated verdict, and
  satisfaction of a review stage.
- Standalone `factory/cli.py` subprocess verbs: `init`, `seats`, `org`,
  `policy show`, `costs`, `runs`, `pause`, `resume`, and `daemon --once`.

Validation command:

```sh
~/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q
# ..s...................sss.......................
# 44 passed, 4 skipped
```

Remaining gap: this lane deliberately does not invoke Codex or Claude itself;
their command lines are replaced only at the process boundary so tests remain
offline and deterministic.
