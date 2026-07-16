<!-- Release-queue lane. Source: external program review §2.6.A and §5.1 order 7. -->

# LANE BRIEF — serialized release queue + immutable dev-pipeline@7

Run only after review-story/gate-decision binding and verification evidence
are merged. Fresh current-main clone, branch `lane/release-queue`.

Read: `AGENTS.md`; review §2.6.A (normative recipe/SQL/state machine/
algorithm/tests); review §5.1 immutable recipe progression;
`shipfactory/recipes/primitives.py`; `shipfactory/recipes/advancer.py`;
`shipfactory/store.py`; `shipfactory/verification.py`; gate-decision code;
all published recipes and artifact-discipline tests.

Baseline: paste current main count. Full suite green ×2 at end.

## Non-negotiable laws

- The human operator owns approval. Release consumes a valid, persisted,
  unconsumed approval decision; it never creates or presses one.
- Release is a new non-model `release` primitive, not an agent task and not
  a raw merge script. No model executes Git with daemon credentials.
- The recipe references an operator-owned release policy. It cannot name
  arbitrary remotes, target branches, hooks, or commands.
- The release controller is the only writer for release state. Git/network
  effects run outside Factory write transactions through durable
  `action_intents`, with probe-before-retry after ambiguous crashes.
- No force push. Published recipes are immutable: add
  `recipes/dev-pipeline@7.yaml`; do not change @1..@6 bytes.

## Scope

1. Add `release` primitive and strict v2 loader validation. Publish
   `dev-pipeline@7` with §2.6.A recipe step verbatim: requires the approved
   change-set and sealed evidence bundle; emits `shipfactory.release/v1`.
2. Add `release_requests` and `release_actions` tables exactly as §2.6.A
   persistence SQL, in the next numbered migrations. Include partial-
   migration guards and compatibility tests.
3. Implement the exact state machine:
   requested→queued→fetching→integrating→reverifying→ready_to_push→
   pushing→remote_verified→merged; exceptional conflict,
   main_verification_failed, remote_unavailable, invariant_error states.
4. Implement the 12-step release algorithm:
   per-repo release lock; revalidate approval activation/revision/evidence;
   fetch and record target SHA; fresh integration clone; apply exact approved
   commits; compare the entire approved path+blob manifest (no drops/extras);
   run full protected verification on integration SHA; compare-and-swap push
   only if remote still equals observed SHA; fetch/probe remote equals
   intended integration SHA; record merged; retain forensic material under
   policy for later pruning.
5. Ambiguous-push recovery: intended SHA remote→succeeded; old SHA→fresh
   action attempt; moved remote→requeue integration; unclassifiable→incident
   block. Conflict never auto-resolved. Any model-assisted conflict result is
   a new candidate SHA and invalidates verification, both reviews, approval,
   and release request.
6. Hook/network safety: disable candidate Git hooks; run only operator-policy
   Git config; bounded clone/fetch/push time and disk budget; stdout/stderr
   persisted to release_actions.

## Mandatory acceptance tests — all §2.6.A cases

Main advances before integration; advances after reverification/before push;
push succeeds but response is lost; branch protection rejects; candidate
commit exists but a file is dropped; extra unapproved file; candidate tests
pass but protected tests fail; dirty operator checkout irrelevant because
fresh clone; crash at every state boundary; duplicate release event; two
repos release concurrently but one repo never has two releases; cleanup
failure after merge; Git hook side-effect attempt; disk full during clone.

Also RED controls for every cross-lab review fix. Required author, no AI
trailers or internal tracker labels. Do not push. Final line:
`LANE_RESULT: done <summary> | blocked <reason>`
