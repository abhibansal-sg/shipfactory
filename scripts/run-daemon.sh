#!/bin/bash
# Factory daemon launcher — raises fd limit BEFORE python starts and logs it.
# Root cause 2026-07-14 (finding #21): daemon inherited ulimit -n 256 from the
# restarted tmux server; codex spawns + sqlite WAL handles exhausted it, and
# SQLITE_IOERR ("disk I/O error") corrupted two boards' indexes in one night.
ulimit -n 4096
echo "[launcher] $(date '+%F %T') fd limit: $(ulimit -n)"
cd /Volumes/MainData/Developer/products/hermes-factory || exit 1
exec env PYTHONPATH=/Users/abbhinnav/Developer/products/hermes-mobile \
  /Users/abbhinnav/Developer/products/hermes-mobile/.venv/bin/python \
  -m factory.cli daemon "$@"
