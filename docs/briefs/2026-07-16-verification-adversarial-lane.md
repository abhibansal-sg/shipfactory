<!-- Verification adversarial lane. Source: external program review §2.4.6–§2.4.10. -->

# LANE BRIEF — verification evidence-gaming adversarial corpus

Fresh clone `/tmp/sf-lane-verification-adversarial`, branch
`lane/verification-adversarial`. Base includes deterministic verification,
protected baselines, asynchronous supervised runners, minimal child envs,
and sealed evidence bundles. Read files fresh.

Read: `AGENTS.md`; review §2.4.6–§2.4.10; `shipfactory/verification.py`;
`shipfactory/store.py`; `shipfactory/recipes/advancer.py`;
`tests/test_verification.py`.

Test command:
`bash -c 'ulimit -n 4096; PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m pytest tests/ -q'`
Baseline: 274 passed ×2 on merged main.

## Goal

Build an independent, hermetic adversarial corpus that attacks the merged
verification implementation. A named test is not enough: each case must
construct the real attack at the strongest feasible boundary and assert the
precise fail-closed outcome. If a case exposes an engine gap, fix the class
in a separate commit, add the lesson to `AGENTS.md`, and retain the attack as
a regression.

## Required attacks — every §2.4.10 bullet

1. Tests execute in the wrong worktree but pass.
2. Old video copied into the new evidence directory.
3. App URL targets a stale prior session.
4. Command prints “125 passed” but exits nonzero.
5. Tests skip/deselect everything and exit zero.
6. UI renders while required backend side effect never occurs.
7. State appears before refresh and disappears after reload.
8. Service worker/browser cache serves old assets.
9. Candidate changes after verification before review.
10. Candidate alters/deletes manifest to remove a protected case.
11. Secret appears in screenshot, trace, or HAR.
12. ffmpeg hangs after tests finish.
13. Browser exits while child app remains.
14. Truncated video has a valid container header.
15. Evidence exceeds disk budget.
16. First attempt fails; second passes — history visible and autonomous
    graduation ineligible.
17. Reviewer and builder share a provider despite different seat names.
18. Model approves without opening evidence — decision binding must require
    the exact sealed bundle, never model prose.
19. Manifest references an evidence item whose bytes change after hashing.

## Additional binding and surface attacks (§2.4.6–§2.4.9)

- Dirty tree before run; dirty tree created during run; HEAD changes; tree
  SHA changes — each invalidates the bundle.
- Runner-generated instance/head/case/timestamp identity cannot be forged by
  the app; stale instance identity rejects.
- Deterministic surface policy: UI→browser, API→API, migration→rollback,
  unknown→stricter. Model may raise, never lower.
- Redaction scans text and structured artifacts; strips cookies/auth headers;
  uncertain screenshot/trace/HAR redaction blocks sealing; evidence is never
  committed to the public repo.
- Review inputs include sealed spec, plan, exact diff, change-set, bundle,
  and failed/retry history — not builder summary scraping.

## Quality requirements

- Real bytes for size/truncation/replacement attacks; real subprocesses for
  hang/orphan where practical; deterministic synchronization, no sleep-only
  races; per-test `tmp_path`/`HERMES_HOME`; no shared `/tmp` filenames.
- Tests must fail on the pre-fix implementation when they expose a new gap.
- Published recipe bytes unchanged. Advancer remains single writer. External
  effects remain outside Factory write transactions.
- Full suite green ×2, exact counts. Required author, no AI trailers, no
  internal tracker labels in commits. Do not push.

Final line: `LANE_RESULT: done <summary> | blocked <reason>`
