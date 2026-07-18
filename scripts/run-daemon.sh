#!/bin/bash
# ShipFactory daemon launcher (post finding-#23/#27/#30).
ulimit -n 4096
mkdir -p ~/.hermes/shipfactory/runs
echo "[launcher] $(date '+%Y-%m-%d %H:%M:%S') fd limit: $(ulimit -n)" > ~/.hermes/shipfactory/runs/daemon.log
cd /Volumes/MainData/Developer/products/shipfactory
export PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile
# Verification probes resolve `python`/`pytest`/playwright from the daemon
# PATH (finding #74); bare `python` does not exist on a stock macOS PATH.
export PATH="/Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin:$PATH"
exec /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m shipfactory.cli daemon --board "${SHIPFACTORY_BOARD:-factory-shakedown8}" --require-recipes >> ~/.hermes/shipfactory/runs/daemon.log 2>&1
