"""Durable daemon run-record coverage."""

import os

import pytest

from shipfactory import daemon, store


def test_daemon_run_records_tick_and_clean_stop(monkeypatch):
    monkeypatch.setattr(daemon, "tick", lambda *args, **kwargs: {"ok": True})

    assert daemon.run(object(), board="default", once=True) == {"ok": True}

    record = store.latest_daemon_run("default")
    assert record is not None
    assert record["pid"]
    assert record["process_start_token"]
    assert record["boards"] == ["default"]
    assert record["last_tick_at"]["default"]
    assert record["board"] == "default"
    assert "one release" in record["board_deprecation"]
    assert record["ended_at"]
    assert record["exit_code"] == 0
    assert store.costs_rollup("executor", 1) == []


def test_daemon_start_reconciles_stale_row_and_persists_current_token(monkeypatch):
    stale_id = store.record_daemon_start(
        "old", 4242, process_start_token="old-token",
    )
    current_pid = os.getpid()

    def token(pid):
        return "current-token" if pid == current_pid else None

    monkeypatch.setattr(daemon, "_process_start_token", token)
    monkeypatch.setattr(daemon, "tick", lambda *args, **kwargs: {"ok": True})

    assert daemon.run(object(), board="default", once=True) == {"ok": True}

    stale = store.run_row(stale_id)
    assert stale["ended_at"]
    assert stale["exit_code"] == -1
    assert stale["result"] == "crashed: pid dead or start token unavailable"
    current = store.latest_daemon_run("default")
    assert current["process_start_token"] == "current-token"


def test_daemon_start_closes_pid_reuse_by_token_mismatch(monkeypatch):
    stale_id = store.record_daemon_start(
        "old", 4242, process_start_token="old-token",
    )
    current_pid = os.getpid()
    monkeypatch.setattr(
        daemon, "_process_start_token",
        lambda pid: "current-token" if pid == current_pid else "new-process-token",
    )
    monkeypatch.setattr(daemon, "tick", lambda *args, **kwargs: {"ok": True})

    daemon.run(object(), board="default", once=True)

    stale = store.run_row(stale_id)
    assert stale["result"] == "crashed: pid reused: start token mismatched"


def test_daemon_start_fails_closed_for_matching_live_identity(monkeypatch):
    stale_id = store.record_daemon_start(
        "old", 4242, process_start_token="live-token",
    )
    current_pid = os.getpid()
    monkeypatch.setattr(
        daemon, "_process_start_token",
        lambda pid: "current-token" if pid == current_pid else "live-token",
    )

    with pytest.raises(RuntimeError, match="live daemon run"):
        daemon.run(object(), board="default", once=True)

    stale = store.run_row(stale_id)
    assert stale["ended_at"] is None
    assert store.latest_daemon_run("old")["id"] == stale_id


def test_daemon_reconciliation_happens_after_lock_and_before_board_open(
    monkeypatch,
):
    calls = []

    class Connection:
        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(daemon, "_process_start_token", lambda pid: "current-token")
    monkeypatch.setattr(
        store, "reconcile_daemon_runs",
        lambda probe: calls.append(("reconcile", probe)) or [],
    )
    monkeypatch.setattr(
        store, "record_daemon_start",
        lambda *args, **kwargs: calls.append(("row",)) or 1,
    )
    monkeypatch.setattr(store, "record_daemon_tick", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "record_daemon_end", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect",
        lambda **kwargs: calls.append(("open",)) or Connection(),
    )
    monkeypatch.setattr(
        daemon, "tick", lambda *args, **kwargs: calls.append(("tick",)) or {"ok": True},
    )

    result = daemon.run(None, board="default", once=True)

    assert result == {"ok": True}
    assert [item[0] for item in calls] == ["reconcile", "row", "open", "tick", "close"]


def test_empty_board_once_tick_has_no_workers_or_approval_action(monkeypatch):
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board="empty-board")
    try:
        result = daemon.run(conn, board="empty-board", once=True)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    assert task_count == 0
    assert result["dispatch"].spawned == []
    assert result["dispatch"].crashed == []
    assert result["dispatch"].auto_blocked == []
    assert result["dispatch"].timed_out == []
    record = store.latest_daemon_run("empty-board")
    assert record["ended_at"]
    assert record["exit_code"] == 0
    assert record["last_tick_at"]["empty-board"]
