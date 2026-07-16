# ShipFactory dev-pipeline@6 — source facts

Exact workflow order:
1. explore — readonly agent task; output exploration
2. spec-draft — agent task; output task-spec
3. spec-attack — review gate of spec-draft; request_changes returns to spec-draft
4. plan-draft — agent task; output plan
5. plan-attack — review gate of plan-draft; request_changes returns to plan-draft
6. build — agent task edits approved source paths; Factory validates, journals, and creates the canonical commit/change-set
7. verify-runtime — verification primitive; output evidence bundle; app reuse requires exact instance/head/workspace identity
8. correctness-review — independent review of exact task-spec, plan, change-set, and evidence
9. adversarial-review — second independent review of the same exact sealed inputs; request_changes returns to build
10. review-story — readonly agent task; Factory canonicalizes exact cited inputs into the operator-facing story
11. approval — human operator only; one-time policy-bound decision; rejection returns to build
12. notify — notification after approval

Trust boundaries:
- Factory-owned commit identity: durable expected SHA/tree/base/run/workspace intent before compare-and-swap update-ref; public Git author/message are not authentication.
- Canonical committed diff rechecks approved and forbidden paths, including rename sources.
- Persisted recipe bytes are canonical-hashed against publication and instance-pinned hashes at enqueue and daemon apply.
- Review independence uses exact successful durable builder/reviewer runs, not mutable seat configuration.
- App identity binds instance, candidate HEAD, workspace, environment, and live process token.
- Review-executor launches include structural --strict-mcp-config.
- Human approval is never pressed by an agent.

Status evidence for final PR will be inserted only after final tests/review complete.
