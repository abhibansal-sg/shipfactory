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
    store.add_monitor("T1", "2026-01-01T00:00:00+00:00", None, 2, "wake_owner", "check", "qa")
    assert store.due_monitors("2026-01-02T00:00:00+00:00")[0]["attempt_count"] == 0
    store.bump_monitor("T1")
    assert store.due_monitors("2026-01-02T00:00:00+00:00")[0]["attempt_count"] == 1
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
