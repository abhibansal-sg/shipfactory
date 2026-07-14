"""Durable daemon run-record coverage."""

from factory import daemon, store


def test_daemon_run_records_tick_and_clean_stop(monkeypatch):
    monkeypatch.setattr(daemon, "tick", lambda *args, **kwargs: {"ok": True})

    assert daemon.run(object(), board="default", once=True) == {"ok": True}

    record = store.latest_daemon_run("default")
    assert record is not None
    assert record["pid"]
    assert record["last_tick_at"]
    assert record["ended_at"]
    assert record["exit_code"] == 0
    assert store.costs_rollup("executor", 1) == []
