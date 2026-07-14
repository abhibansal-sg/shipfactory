# Factory write-surface conformance evidence

Captured at 1440 × 1000 with Playwright Chromium 145 against the unchanged
Factory bundle mounted in the host-token conformance harness. The harness uses
the host's real `Button`, `Badge`, `Card`, and `CardContent` components.

- `run-recipe-form.png` — schema-generated string, integer, boolean, enum, and datetime fields plus optional-step skips.
- `new-triage-task.png` — title/body/board creation form with the stopped-daemon routing warning.
- `daemon-running.png` — running daemon chip with last-tick age.
- `daemon-stopped.png` — destructive stopped-daemon chip and frozen-instance explanation.
- `cancel-confirm.png` — dry-run consequence review with active workers, suppressed tasks, nonterminal steps, and explicit confirmation.

Browser automation also submitted both creation forms, exercised reroute, and
confirmed that cancellation only posts after the preview dialog. Console error
count: zero.
