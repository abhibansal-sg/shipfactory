## Summary

<!-- One sentence of BEHAVIOR, not implementation. `Closes #N` when applicable. -->

## Changes

<!-- Terse bullets with file paths. -->

-

## Behaviour

<!-- Before/after prose — for features AND fixes alike. -->

**Before:**

**After:**

## Validation

<!-- Table. Every row executed first-hand — no lane self-reports.
     Include a RED-control row for fixes: revert the fix keeping the tests,
     show the regressions FAIL, restore, show green. -->

| Check | Result |
|---|---|
| Full suite (run 1) | |
| Full suite (run 2) | |
| RED control | |

## Checklist

- [ ] Full suite green ×2 consecutively (counts in the table)
- [ ] No published recipe bytes changed
- [ ] Spec §15/§17 updated if any module signature or primitive changed
- [ ] New findings landed in AGENTS.md in this same PR
- [ ] No secrets, tokens, or private paths
- [ ] Author `Abhinav Bansal <abhibansal-sg@users.noreply.github.com>`, no AI trailers, no tracker IDs

## Infographic

<!-- Last. Fix PRs get one too. -->
