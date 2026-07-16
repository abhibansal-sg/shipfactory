<!-- Deployment-controller lane. Source: external program review §2.6.B and §5.1 order 8. -->

# LANE BRIEF — deployment controller + independently drilled rollback

Run only after serialized release queue is merged and proven. Fresh current-
main clone, branch `lane/deployment-controller`.

Read: `AGENTS.md`; review §2.6.B (normative recipe/SQL/state machine/
isolation/tests); Q7 rollback contract; release implementation and records;
`shipfactory/recipes/primitives.py`; `shipfactory/recipes/advancer.py`;
`shipfactory/store.py`; daemon supervision and action-intent code.

Baseline: paste current main count. Full suite green ×2.

## Non-negotiable laws

- New non-model `deploy` primitive; no arbitrary `on_merged` scripts.
- Deployment policy is operator-owned or loaded from the last trusted target
  revision, never from the candidate that will be deployed.
- A deployment policy remains disabled until its rollback path passes an
  independent drill. No autonomous deployment without a previously tested,
  automatically executable rollback.
- Deploy/rollback effects run outside Factory write transactions through
  durable action intents with target probes before retry.
- The daemon never kills/replaces itself inside its action transaction; self-
  deployment uses an external supervisor.
- Initial daily-driver scope is fail-closed and allowlisted: dedicated service
  account/root; no login keychain; no sudo; allowlisted launchd labels and flag
  files; explicit ports; process/disk/FD limits; external supervisor.

## Scope

1. Add strict `deploy` primitive and `shipfactory.deployment/v1` record using
   the §2.6.B recipe shape; accepts only a sealed release record and an
   operator-owned named policy.
2. Add `deployments` table exactly as normative SQL in the next numbered
   migration; partial-migration guard and compatibility tests.
3. Implement state machine queued→preflight→deploying→verifying→healthy→
   committed; failures deploying/verifying→rollback_pending→rolling_back→
   rolled_back; terminal rollback_failed/invariant_failed.
4. Policy preflight binds release/artifact/environment; checks policy hash,
   rollback-drill attestation, service account, allowlisted launchd label/
   flags/ports, resource ceilings, previous deployment availability, and
   sufficient staging disk before side effects.
5. Self-deployment protocol: versioned staging directory; shadow daemon on
   non-production/read-only health mode; health check; external-supervisor
   atomic symlink/launchd switch; old daemon retained; external supervisor
   confirms success or rolls back. No retiring-daemon self-confirmation.
6. Ambiguous crash recovery probes actual flag/symlink/process/code identity
   before retry; `healthy` requires the served code identity equals the
   released artifact, not only an open port.

## Mandatory acceptance tests — all §2.6.B cases

Health failure auto-restores prior version; process dies after flag change
before record; rollback command fails; new daemon cannot open Factory DB;
second active dispatcher; flag changes but old code served; reboot mid-deploy;
same candidate modifies deployment script; unapproved launchd label; port
owned by another production process; disk full during staging; previous
release directory wrongly pruned.

Also run an independent rollback drill before any policy becomes enabled and
persist its exact policy/artifact/result identity. RED controls for every
cross-lab finding. Required author, no AI trailers/internal tracker labels.
Do not push. Final line: `LANE_RESULT: done <summary> | blocked <reason>`
