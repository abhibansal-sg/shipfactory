#!/bin/sh
# ShipFactory project onboarding: stamp a target repository with its factory
# manifest kit. Usage:
#   scripts/sf-onboard.sh <repo-path> <app-start-command> <test-command>
# Example:
#   scripts/sf-onboard.sh ~/products/edulife \
#     "python -m edulife.server \$1" \
#     "python -m pytest tests/ -q"
#
# Writes into <repo>/.shipfactory/ + <repo>/scripts/: runtime.yaml,
# verification.yaml, sf-bootstrap.sh, sf-seed.sh, sf-app.sh. Review, adapt,
# and COMMIT them in the target repo — the factory reads manifests from the
# repo's trusted base commit, so nothing works until they are committed.
# Lessons baked in: identity endpoint contract (finding #87), interpreter via
# $SHIPFACTORY_PYTHON never bare PATH (finding #89), pytest_summary oracle.
set -eu
REPO="$1"; APP_CMD="$2"; TEST_CMD="$3"
[ -d "$REPO/.git" ] || { echo "error: $REPO is not a git repository" >&2; exit 1; }
mkdir -p "$REPO/.shipfactory" "$REPO/scripts"

cat > "$REPO/.shipfactory/runtime.yaml" <<EOF
schema: shipfactory.runtime/v1
bootstrap:
  argv: [scripts/sf-bootstrap.sh]
  tracked_inputs: []
  network: deny
seed:
  argv: [scripts/sf-seed.sh]
app:
  start_argv: [scripts/sf-app.sh, "\${PORT}"]
  healthcheck: {path: /healthz, timeout_seconds: 60}
EOF

cat > "$REPO/.shipfactory/verification.yaml" <<EOF
schema: shipfactory.verification/v1
cases:
  - id: protected-pytest
    requirement_ids: [REQ-1]
    driver: pytest
    argv: [sh, -c, '$TEST_CMD']
    oracle: {type: pytest_summary, min_passed: 1}
capture: {video: false, trace: false, screenshots: on-failure}
EOF

cat > "$REPO/scripts/sf-bootstrap.sh" <<'EOF'
#!/bin/sh
# Dependencies come from the operator-managed environment handed to scripts
# as $SHIPFACTORY_PYTHON (finding #89). Add project install steps here if the
# candidate worktree needs them; exit non-zero to fail the environment loudly.
set -eu
exit 0
EOF

cat > "$REPO/scripts/sf-seed.sh" <<'EOF'
#!/bin/sh
# Seed deterministic fixture state for verification here (or nothing).
set -eu
exit 0
EOF

cat > "$REPO/scripts/sf-app.sh" <<EOF
#!/bin/sh
# Verification app: serve the REAL product from the candidate worktree on the
# leased port (\$1). MUST serve GET /healthz and GET /.shipfactory/identity
# returning {"instance_id": \$SHIPFACTORY_INSTANCE_ID, "head_sha":
# \$SHIPFACTORY_HEAD_SHA} — the commit-binding probe fails closed without it
# (finding #87). Use "\${SHIPFACTORY_PYTHON:-python3}" for any interpreter —
# never bare python on ambient PATH (finding #89).
set -eu
exec $APP_CMD
EOF

chmod +x "$REPO/scripts/sf-bootstrap.sh" "$REPO/scripts/sf-seed.sh" "$REPO/scripts/sf-app.sh"
echo "Onboarding kit written to $REPO — review, adapt sf-app.sh to serve the"
echo "identity endpoint, then COMMIT in the target repo. Next steps:"
echo "  1. hermes kanban create-board <board> --default-workdir $REPO"
echo "  2. add the board to the factory daemon (--board or boards config)"
echo "  3. instantiate dev-pipeline@14 with base_sha = the repo's main HEAD"
