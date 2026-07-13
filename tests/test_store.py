import sqlite3

from factory import store


def test_run_policy_decisions_and_rollup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    store.init_db()
    run = store.record_run_start("T1", "dev", "codex", "gpt", 123)
    store.record_run_end(run, 0, 10, 5, 2.5, "done")
    assert store.costs_rollup("seat", 7)[0]["tokens_total"] == 15
    policy = {"stages": [{"id": "review"}]}
    store.set_policy("T1", policy)
    assert store.get_policy("T1") == policy
    store.record_decision("T1", "review", "review", "qa", "approved", "x.py:1")
    assert store.decisions_for("T1")[0]["seat"] == "qa"


def test_monitors_watchdogs_pause_and_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.add_monitor("T1", "2026-01-01T00:00:00+00:00", None, 2, "wake_owner", "check", "qa", 60)
    assert store.due_monitors("2026-01-02T00:00:00+00:00")[0]["attempt_count"] == 0
    store.advance_monitor("T1", "2026-01-02T00:00:00+00:00")
    assert not store.due_monitors("2026-01-02T00:00:00+00:00")
    row = store.due_monitors("2026-01-02T00:01:00+00:00")[0]
    assert row["attempt_count"] == 1
    assert row["interval_seconds"] == 60
    store.clear_monitor("T1")
    assert not store.due_monitors("2026-01-02T00:00:00+00:00")
    store.add_watchdog("T1", "qa", "inspect")
    store.set_watchdog_fingerprint("T1", "abc")
    assert store.watchdogs()[0]["last_fingerprint"] == "abc"
    assert not store.seat_paused("dev")
    store.set_seat_paused("dev", True)
    assert store.seat_paused("dev")
    store.sync_upsert(7, "T1", "g1", "k1")
    store.sync_upsert(7, "T2", "g2", "k2")
    assert store.sync_get(7)["task_id"] == "T2"


def test_init_db_migrates_legacy_monitor_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "factory" / "factory.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE monitors (
          task_id TEXT PRIMARY KEY, next_check_at TEXT NOT NULL, timeout_at TEXT,
          max_attempts INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
          recovery_policy TEXT NOT NULL, notes TEXT, scheduled_by TEXT)""")

    store.init_db()

    with sqlite3.connect(path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(monitors)")}
    assert columns["interval_seconds"][4] == "300"
