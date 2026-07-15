<!-- Committed 2026-07-15. Adversarial suite for the A0 lane — separate lane
     by law: a build lane's own tests never count as the adversarial suite.
     Executor: claude-sonnet-5 (cross-lab from A0's gpt-5.6-sol builder).
     Dispatch AFTER the A0 PR merges. -->

# LANE BRIEF — A0X adversarial suite: process races and kill -9 failpoints

You are a non-interactive adversarial test lane. Work only in your assigned
worktree (fresh clone, branch `lane/a0x-adversarial`). Your job is to BREAK
the A0 foundation, not to praise it. Every scenario below must exist as a
real test; where the engine survives, the test proves it; where it does not,
FIX the engine (cite the review clause) and note it in your report.

Read, in order:
1. AGENTS.md
2. docs/reviews/2026-07-15-external-program-review.md §2.0 (the spec A0 built to)
3. docs/briefs/2026-07-15-a0-lane.md (what the build lane was told)
4. The A0 implementation itself (git log for the lane commits)
5. tests/ — the build lane's own tests (you must NOT duplicate them; you must
   exceed them)

## Non-negotiable test-reality rules
- Real SQLite files on disk. Real `multiprocessing`/`subprocess` OS processes.
  Thread-only concurrency tests are INSUFFICIENT and will be rejected.
- kill -9 (SIGKILL) at named failpoints, not exceptions raised in-process.
- Each test asserts on-disk state after recovery, not in-memory state.

## Scenarios — EXACTLY these 10 (review §2.0.6)
1. Two OS processes race to claim the same advance event → exactly one applies.
2. Two daemon launches → second exits nonzero before opening any board DB.
3. Crash (SIGKILL) after action-intent insertion, before the external action →
   restart performs the effect exactly once.
4. Crash after the external action, before success recording → restart probes,
   marks succeeded, no duplicate effect.
5. Lease expiry while the original holder is merely slow → no double effect;
   slow holder's late write is rejected.
6. Root-collector complete_task() returns False → no applied outcome recorded;
   fresh attempt remains possible (spent-key law respected).
7. Gate completion when the kanban task is already terminal → discarded with
   reason, not silent success.
8. A 30-second notification send while a second process writes factory state →
   second write succeeds within the busy timeout.
9. Permanent event-key retention: after every recovery above, every terminal
   advance-event key still exists unchanged.
10. Configured/required recipe mode with unreadable config → zero dispatches,
    persisted incident record.

## Constraints
- Do not weaken, skip, or delete any existing test.
- Do not change published recipe bytes.
- Full suite green ×2 at the end; paste both counts.
- Commits: author 'Abhinav Bansal <abhibansal-sg@users.noreply.github.com>',
  no AI trailers, no issue-tracker IDs. Do not push.

Final line: LANE_RESULT: done <summary> | blocked <reason>
