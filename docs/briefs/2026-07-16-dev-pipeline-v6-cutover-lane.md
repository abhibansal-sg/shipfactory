<!-- Dogfood-cutover lane. Source: review §2.4–§2.5 and immutable recipe progression §5.1. -->

# LANE BRIEF — dev-pipeline@6 closed-loop dogfood cutover

Run only after review-story/gate-binding and verification-adversarial changes
are merged. Fresh current-main clone, branch `lane/dev-pipeline-v6`.

Read: `AGENTS.md`; review §2.4.1–§2.4.10, §2.5.1–§2.5.4, §5.1;
`recipes/dev-pipeline@5.yaml`; artifact validators; verification; decision
binding; loader/advancer/primitives; artifact-discipline tests.

Baseline current main; full suite ×2. This lane is the DOGFOOD CUTOVER: after
it and one shakedown pass, remaining eligible sprint work enters ShipFactory.

## Laws

- Publish NEW `recipes/dev-pipeline@6.yaml`; @1..@5 bytes never change.
- Candidate/model claims never create evidence. Change-set and evidence are
  daemon-rederived, sealed, and revision-bound.
- Verification is the non-model primitive and runs before model review.
- Human operator is sole approval authority. No agent/policy presses approve.
- A candidate changing Factory control-plane code is evaluated by the
  previously trusted merged runtime, never the candidate machinery.

## Scope

1. Add strict `shipfactory.change-set/v1` artifact support. Required identity:
   base/head/tree SHA, ordered commits, complete rename-aware changed-path
   manifest with status and resulting blob SHA, allowed paths, dirty-tree
   state. During sealing, rederive all Git identity/ancestry/diff/blob values
   from the assigned clone; reject worker-supplied mismatch, extra/missing
   paths, foreign clone SHAs, dirty tree, symlink/path escape, or head not
   descended from base. This is the approved manifest release will consume.
2. `dev-pipeline@6` supersedes @5 and preserves typed planning. Build now
   emits the sealed change-set artifact at
   `.shipfactory-output/change-set.json`; its allowed paths come from the
   approved plan and cannot be widened by the builder.
3. Add the §2.4.1 verification step verbatim after build: inputs build
   change-set + plan; output `shipfactory.evidence/v1`; manifest
   `.shipfactory/verification.yaml`; operator profile `browser-standard`;
   environment `app`; no seat/model.
4. Add sequential correctness-review then adversarial-review after
   verification. Both receive sealed task spec, plan, exact change-set,
   evidence bundle, and failed/retry history—not builder prose. Both are
   read-only, cross-trust-domain by policy, and request changes only against
   the upstream build. Rejection invalidates downstream verification/story/
   approval and re-enters the legal cone.
5. Add review-story producer after both reviews. Output
   `shipfactory.review-story/v1`; validator proves exact diff completeness,
   requirement/evidence links, deletion/config visibility, generated-file
   honesty, and residual-risk requirements.
6. Add operator approval gate after review story. Gate decision binds exact
   instance/step/activation, candidate commit/tree, input revision, spec,
   plan, evidence bundle, story, policy hash, and one-time nonce. Any rework,
   rebase, retry-generated evidence, conflict resolution, or activation
   invalidates the pending decision/token.
7. Optional notify follows approval only. Nothing merges/releases/deploys in
   @6; it parks with a complete review story and sealed evidence for Abhi.
8. Update budgets/caps for verification, two reviews, story, and approval;
   named pools remain planning/build/review and deterministic primitives do
   not pretend to consume model tokens.

## Mandatory acceptance

- Published-recipe immutability hashes @1..@5; @6 loads exactly.
- Full real recipe fixture reaches approval waiting with sealed change-set,
  protected verification bundle, two review approvals, and complete story.
- Build claim with dropped/extra/renamed/wrong-blob path rejects.
- Dirty/post-test-mutated/wrong-worktree candidate rejects.
- Candidate removes protected manifest/case; protected baseline still runs.
- Review uses same provider under two seat names; independence rejects.
- Retry-green evidence marks ineligible and story residual risk nonempty.
- Stale/replayed/cross-instance phone token cannot advance gate.
- Rework after notification invalidates token/evidence/story and reactivates
  only the legal build cone.
- No merge/release/deploy side effect occurs.

Required author, no AI trailers/internal tracker labels. Do not push. Final:
`LANE_RESULT: done <summary> | blocked <reason>`
