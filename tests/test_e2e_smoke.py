"""Real-path Factory spawn, reaping, telemetry, and policy smoke tests."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path


HERMES = Path.home() / "Developer/products/hermes-mobile"
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from hermes_cli import kanban_db

from headframe import policy, store
from headframe.spawn import _RUNNING, headframe_spawn, reap_finished


def _write_seats(home: Path) -> None:
    (home / "profiles" / "dev").mkdir(parents=True)
    (home / "profiles" / "verifier").mkdir(parents=True)
    factory = home / "headframe"
    factory.mkdir()
    (factory / "seats.yaml").write_text("""company: e2e
seats:
  dev:
    profile: dev
    executor: codex
    model: test
    role: engineer
  verifier:
    profile: verifier
    executor: codex
    model: test
    role: qa
hierarchy_gates:
  landers: [dev]
  verdicts: [verifier]
""", encoding="utf-8")


def _harness(tmp_path: Path, name: str, output: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + output, encoding="utf-8")
    script.chmod(0o755)
    return script


def _reap_one() -> dict:
    for _ in range(100):
        completed = reap_finished()
        if completed:
            return completed[0]
        time.sleep(0.01)
    raise AssertionError("real harness did not exit")


def _run_row(home: Path, task_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(home / "headframe" / "headframe.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM runs WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def test_real_dispatch_spawn_reap_and_policy(monkeypatch, tmp_path):
    """Drive actual kanban, Factory, and /bin/sh paths for all result rules."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _RUNNING.clear()
    _write_seats(tmp_path)
    store.init_db()
    kanban_db.create_board("e2e")
    done = _harness(tmp_path, "done.sh", "printf 'tokens used\\n1,234\\nHEADFRAME_RESULT: done shipped it\\n'\n")
    blocked = _harness(tmp_path, "blocked.sh", "printf 'tokens used\\n1,234\\nHEADFRAME_RESULT: blocked needs input\\n'\n")
    missing = _harness(tmp_path, "missing.sh", "printf 'tokens used\\n1,234\\nordinary output\\n'\n")

    conn = kanban_db.connect(board="e2e")
    try:
        done_task = kanban_db.create_task(conn, title="done", assignee="dev")
        monkeypatch.setenv("FACTORY_EXECUTOR_CMD_CODEX", str(done))
        dispatched = kanban_db.dispatch_once(conn, spawn_fn=headframe_spawn, board="e2e")
        assert [row[0] for row in dispatched.spawned] == [done_task]
        assert _reap_one()["result"] == "done"
        assert kanban_db.get_task(conn, done_task).status == "done"

        row = _run_row(tmp_path, done_task)
        assert row["result"] == "done"
        assert row["exit_code"] == 0
        assert row["tokens_total"] == 1234
        assert row["ended_at"]

        sentinel_blocked_task = kanban_db.create_task(conn, title="blocked sentinel", assignee="dev")
        monkeypatch.setenv("FACTORY_EXECUTOR_CMD_CODEX", str(blocked))
        kanban_db.dispatch_once(conn, spawn_fn=headframe_spawn, board="e2e")
        outcome = _reap_one()
        assert outcome["task_id"] == sentinel_blocked_task
        assert outcome["result"] == "blocked"
        assert outcome["summary"] == "needs input"
        assert kanban_db.get_task(conn, sentinel_blocked_task).status == "blocked"

        blocked_task = kanban_db.create_task(conn, title="missing sentinel", assignee="dev")
        monkeypatch.setenv("FACTORY_EXECUTOR_CMD_CODEX", str(missing))
        kanban_db.dispatch_once(conn, spawn_fn=headframe_spawn, board="e2e")
        outcome = _reap_one()
        assert outcome["task_id"] == blocked_task
        assert outcome["result"] == "blocked"
        assert outcome["summary"] == "no result sentinel"
        assert kanban_db.get_task(conn, blocked_task).status == "blocked"

        store.set_policy(done_task, {"mode": "normal", "stages": [
            {"id": "review", "type": "review", "approvalsNeeded": 1, "participants": ["verifier"]},
        ]})
        reopened = policy.on_complete(done_task, "e2e", "dev", "needs review")
        assert reopened == {"action": "reopen", "next_stage": "review"}
        refreshed = kanban_db.connect(board="e2e")
        try:
            assert kanban_db.get_task(refreshed, done_task).status == "ready"
            assert kanban_db.get_task(refreshed, done_task).assignee == "verifier"
        finally:
            refreshed.close()
        assert policy.record_verdict(done_task, "review", "approve", "factory/spawn.py:42 verified", "verifier") == {
            "action": "allow", "next_stage": None,
        }
    finally:
        conn.close()
