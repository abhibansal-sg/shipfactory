"""Lane V2 adversarial tests for slow, malformed, and concurrent boundaries."""

from __future__ import annotations

import http.client
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import github_sync, store
import factory.daemon as daemon
import factory.spawn as spawn
from factory.policy import citation_ok


def _install_tick_stubs(monkeypatch):
    calls = []
    kanban = types.ModuleType("hermes_cli.kanban_db")
    kanban.dispatch_once = lambda conn, **kw: calls.append(("dispatch", kw)) or "dispatched"
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    child_spawn = types.ModuleType("factory.spawn")
    child_spawn.factory_spawn = object()
    child_spawn.reap_finished = lambda: []
    watchdog = types.ModuleType("factory.watchdog")
    watchdog.tick = lambda conn, board=None: "watched"
    for name, module in (
        ("hermes_cli", hermes),
        ("hermes_cli.kanban_db", kanban),
        ("factory.spawn", child_spawn),
        ("factory.watchdog", watchdog),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    import factory as factory_package

    monkeypatch.setattr(factory_package, "spawn", child_spawn, raising=False)
    monkeypatch.setattr(factory_package, "watchdog", watchdog, raising=False)
    monkeypatch.setattr(factory_package, "github_sync", github_sync, raising=False)
    return calls


def test_slow_github_call_has_timeout_and_does_not_wedge_tick(monkeypatch):
    """A timed-out gh child must leave the dispatch tick bounded."""
    _install_tick_stubs(monkeypatch)
    monkeypatch.setenv("HERMES_GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(github_sync, "_COMMAND_TIMEOUT", 0.01, raising=False)

    def slow_run(command, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            time.sleep(0.40)
            return SimpleNamespace(stdout="[]")
        time.sleep(timeout + 0.005)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(github_sync.subprocess, "run", slow_run)
    started = time.monotonic()
    result = daemon.tick(object(), board="demo", sync=True)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert result["sync"] is None
    assert "timed out" in result["sync_error"]


def test_reap_does_not_wait_for_hung_executor_past_claim_ttl():
    """An alive child remains owned by kanban stale-claim recovery, not reaping."""
    process = subprocess.Popen(["/bin/sleep", "5"])
    spawn._RUNNING.clear()
    spawn._RUNNING[process.pid] = {
        "proc": process,
        "run_id": 1,
        "task_id": "T-hung",
        "executor": "codex",
        "board": "demo",
        "log_path": Path("unused.log"),
        "started": time.monotonic() - 3600,
    }
    started = time.monotonic()
    try:
        assert spawn.reap_finished() == []
        assert time.monotonic() - started < 0.10
        assert process.pid in spawn._RUNNING
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=1)
        spawn._RUNNING.clear()


# NOTE: test_dashboard_slow_accessor removed with the retired standalone
# dashboard server (§10-v2 delete-on-sight); the plugin tab's API is covered
# by tests/test_dashboard_plugin.py.


@pytest.mark.parametrize(
    ("log_text", "result", "summary"),
    [
        ("garbage\nFACTORY_RESULT: done mid-log\nmore garbage", "blocked", "no result sentinel"),
        ("FACTORY_RESULT: done first\nFACTORY_RESULT: blocked last", "blocked", "last"),
        ("FACTORY_RESULT: blocked first\nFACTORY_RESULT: done last", "done", "last"),
        ("FACTORY_RESULT: done valid\nFACTORY_RESULT: ??? malformed", "blocked", "no result sentinel"),
    ],
)
def test_factory_result_parser_is_malformed_and_last_line_safe(log_text, result, summary):
    """Only the final non-empty line controls the result, without exceptions."""
    assert spawn._parse_result(log_text, 0) == (result, summary)


@pytest.mark.parametrize(
    ("log_text", "result", "summary"),
    [
        # Verdict alone on the final line (the pre-#25 happy path).
        ('FACTORY_VERDICT: {"outcome":"approve","body":"ok"}',
         "done", 'FACTORY_VERDICT: {"outcome":"approve","body":"ok"}'),
        # Finding #25: disciplined review workers emit BOTH sentinels —
        # verdict first, FACTORY_RESULT last. The verdict must win or the
        # advancer's parse_verdict fuses the review gate.
        ('FACTORY_VERDICT: {"outcome":"approve","body":"ok"}\nFACTORY_RESULT: done review complete',
         "done", 'FACTORY_VERDICT: {"outcome":"approve","body":"ok"}'),
        # Two verdicts: the LATEST wins (retry-within-run semantics).
        ('FACTORY_VERDICT: {"outcome":"approve","body":"old"}\nFACTORY_VERDICT: {"outcome":"request_changes","target_step":"build","body":"new"}\nFACTORY_RESULT: done wrapped up',
         "done", 'FACTORY_VERDICT: {"outcome":"request_changes","target_step":"build","body":"new"}'),
        # A verdict buried mid-log still wins over trailing prose.
        ('FACTORY_VERDICT: {"outcome":"approve","body":"ok"}\ntrailing chatter',
         "done", 'FACTORY_VERDICT: {"outcome":"approve","body":"ok"}'),
    ],
)
def test_verdict_sentinel_beats_factory_result(log_text, result, summary):
    """FACTORY_VERDICT anywhere in the log outranks the final-line RESULT."""
    assert spawn._parse_result(log_text, 0) == (result, summary)


def test_store_concurrent_writers_use_wal_and_do_not_leak_locked_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    errors = []

    def write_rows(worker):
        try:
            for index in range(50):
                task_id = f"{worker}-{index}"
                store.record_run_start(task_id, "dev", "codex", "gpt", worker * 1000 + index)
                store.set_policy(task_id, {"mode": "normal", "stages": []})
        except Exception as exc:  # pragma: no cover - assertion reports this path
            errors.append(exc)

    threads = [threading.Thread(target=write_rows, args=(worker,)) for worker in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    with store._connect() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout >= 1000


def test_citation_ok_handles_fuzz_and_ten_megabyte_body():
    assert not citation_ok("")
    assert not citation_ok("λ漢字🙂 without proof")
    assert not citation_ok(r".*+?^$[](){}|\\")
    assert citation_ok("APPROVE: clean pass; no findings")
    body = "x" * (10 * 1024 * 1024) + "\nfactory/policy.py:29"
    started = time.monotonic()
    assert citation_ok(body)
    assert time.monotonic() - started < 2.0


def test_huge_prompt_to_non_reading_child_does_not_wedge_dispatch(tmp_path, monkeypatch):
    """#16-OPERATOR F1: a >64KB prompt handed to a child that never reads
    stdin must NOT block factory_spawn (the dispatch thread). Before the
    file-stdin fix, the blocking pipe write wedged the daemon forever with
    no exception — the #62496 sweeper class. Proven live 07-12."""
    import sys as _sys, time, types

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seat = types.SimpleNamespace(executor="codex", model="m", profile="p")
    cfg = types.SimpleNamespace(seats={"s1": seat}, hierarchy_gates={})
    config = types.ModuleType("factory.config"); config.load_seats = lambda path=None: cfg
    store = types.ModuleType("factory.store")
    store.seat_paused = lambda s: False
    store.record_run_start = lambda *a, **k: 1
    kanban = types.ModuleType("hermes_cli.kanban_db")
    # worker context bigger than any pipe buffer
    kanban.build_worker_context = lambda conn, tid: "x" * 300_000
    kanban.connect = lambda board=None: types.SimpleNamespace(close=lambda: None)
    hermes = types.ModuleType("hermes_cli"); hermes.kanban_db = kanban
    for name, mod in (("factory.config", config), ("factory.store", store),
                      ("hermes_cli", hermes), ("hermes_cli.kanban_db", kanban)):
        monkeypatch.setitem(_sys.modules, name, mod)
    import factory as _pkg
    monkeypatch.setattr(_pkg, "config", config, raising=False)
    monkeypatch.setattr(_pkg, "store", store, raising=False)
    # executor command = a child that sleeps WITHOUT reading stdin
    monkeypatch.setenv("FACTORY_EXECUTOR_CMD_CODEX", "/bin/sleep 20")
    from factory.spawn import factory_spawn, _RUNNING
    t0 = time.monotonic()
    pid = factory_spawn({"id": "t-f1", "assignee": "s1"}, str(tmp_path / "ws"))
    elapsed = time.monotonic() - t0
    assert pid is not None
    assert elapsed < 5, f"factory_spawn blocked {elapsed:.1f}s on a non-reading child"
    # cleanup: kill the sleeper
    rec = _RUNNING.pop(pid, None)
    if rec:
        rec["proc"].kill(); rec["proc"].wait()
