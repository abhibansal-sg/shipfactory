"""Subprocess coverage for Factory's standalone command surface."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES = Path.home() / "Developer/products/hermes-mobile"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"HERMES_HOME": str(home), "PYTHONPATH": os.pathsep.join((str(ROOT), str(HERMES)))}
    return subprocess.run([sys.executable, str(ROOT / "shipfactory" / "cli.py"), *args], text=True, capture_output=True, env=env)


def test_cli_subprocess_verbs_against_real_factory_state(tmp_path):
    """Ensure documented commands execute with a fresh Hermes home."""
    init = _run(tmp_path, "init")
    assert init.returncode == 0 and '"initialized": true' in init.stdout
    for profile in ("release", "verifier", "architect", "dev-backend"):
        (tmp_path / "profiles" / profile).mkdir(parents=True)

    seats = _run(tmp_path, "seats")
    policy = _run(tmp_path, "policy", "show", "no-task")
    costs = _run(tmp_path, "costs")
    runs = _run(tmp_path, "runs")
    pause = _run(tmp_path, "pause", "verifier")
    resume = _run(tmp_path, "resume", "verifier")
    daemon = _run(tmp_path, "daemon", "--once")

    assert seats.returncode == policy.returncode == costs.returncode == runs.returncode == 0
    assert pause.returncode == resume.returncode == daemon.returncode == 0
    assert '"name": "verifier"' in seats.stdout
    assert policy.stdout.strip() == ""
    assert costs.stdout.strip() == "[]"
    assert runs.stdout.strip() == "[]"
    assert '"paused": true' in pause.stdout
    assert '"paused": false' in resume.stdout
