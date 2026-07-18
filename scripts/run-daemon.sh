#!/bin/bash
# ShipFactory daemon launcher (post finding-#23/#27/#30).
ulimit -n 4096
mkdir -p ~/.hermes/shipfactory/runs
echo "[launcher] $(date '+%Y-%m-%d %H:%M:%S') fd limit: $(ulimit -n)" > ~/.hermes/shipfactory/runs/daemon.log
cd /Volumes/MainData/Developer/products/shipfactory
# Import Hermes `hermes_cli` from a checkout carrying the recipe-kanban APIs
# (create_blocked_task / cancel_subtree). The conventional checkout may be on an
# unrelated branch, so default to the dedicated `feat-kanban-recipe-apis`
# worktree; override with SHIPFACTORY_HERMES_PATH.
export PYTHONPATH="${SHIPFACTORY_HERMES_PATH:-/Volumes/MainData/Developer/worktrees/hermes-shipfactory-recipe-apis}"
# Verification probes resolve `python`/`pytest`/playwright from the daemon
# PATH (finding #74); bare `python` does not exist on a stock macOS PATH.
export PATH="/Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin:$PATH"
exec /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m shipfactory.cli daemon --board "${SHIPFACTORY_BOARD:-factory-shakedown8}" --require-recipes >> ~/.hermes/shipfactory/runs/daemon.log 2>&1
