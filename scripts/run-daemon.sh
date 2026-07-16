#!/bin/bash
# ShipFactory daemon launcher (post finding-#23/#27/#30).
ulimit -n 4096
mkdir -p ~/.hermes/shipfactory/runs
echo "[launcher] $(date '+%Y-%m-%d %H:%M:%S') fd limit: $(ulimit -n)" > ~/.hermes/shipfactory/runs/daemon.log
cd /Volumes/MainData/Developer/products/shipfactory
export PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile
exec /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python -m shipfactory.cli daemon --board factory-shakedown5 --require-recipes >> ~/.hermes/shipfactory/runs/daemon.log 2>&1
