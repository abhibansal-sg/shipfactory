"""Multi-board daemon multiplexing and isolation regressions."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from shipfactory import cli, daemon, store
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import load_library


PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}


def _recipe(path: Path):
    path.mkdir()
    (path / "multiboard@1.yaml").write_text(
        """schema: shipfactory.recipe/v1
id: multiboard
version: 1
status: active
description: multi-board daemon regression
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 4, max_step_activations: 2, max_tokens: 500000}
steps:
  - id: work
    primitive: agent_task
    title: Work
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: do it, execution_profile: standard, workspace: worktree}
""",
        encoding="utf-8",
    )
    return load_library(path).get("multiboard@1")


def _step_state(instance_id: str) -> str:
    with store._connect() as db:
        return db.execute(
            "SELECT state FROM recipe_steps WHERE instance_id=? AND step_id='work'",
            (instance_id,),
        ).fetchone()["state"]


def _configure_tick(monkeypatch) -> None:
    from shipfactory import config as factory_config
    from shipfactory import watchdog
    from shipfactory.recipes import advancer
    from hermes_cli import kanban_db

    cfg = SimpleNamespace(
        company="board-a",
        recipes={
            "enabled": True,
            "dispatcher_max_in_progress": 4,
            "execution_profiles": PROFILES,
            "selector": {"enabled": False},
        },
    )
    monkeypatch.setattr(factory_config, "load_seats", lambda: cfg)
    monkeypatch.setattr(advancer, "startup_guard", lambda config: None)
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda *args, **kwargs: 0)
    monkeypatch.setattr("shipfactory.spawn.reap_finished", lambda: [])
    monkeypatch.setattr(watchdog, "tick", lambda *args, **kwargs: None)


def test_one_tick_advances_instances_on_two_boards_and_records_both_ticks(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db

    recipe = _recipe(tmp_path / "library")
    conns = {
        "board-a": kanban_db.connect(board="board-a"),
        "board-b": kanban_db.connect(board="board-b"),
    }
    try:
        instantiate(
            conns["board-a"], board="board-a", recipe=recipe,
            parameters={}, instance_id="instance-a",
        )
        instantiate(
            conns["board-b"], board="board-b", recipe=recipe,
            parameters={}, instance_id="instance-b",
        )
        _configure_tick(monkeypatch)

        result = daemon.run(
            conns, boards=["board-a", "board-b"], once=True, interval=2.0,
        )

        assert set(result["boards"]) == {"board-a", "board-b"}
        assert _step_state("instance-a") == "running"
        assert _step_state("instance-b") == "running"
        record = store.latest_daemon_run()
        assert record["boards"] == ["board-a", "board-b"]
        assert set(record["last_tick_at"]) == {"board-a", "board-b"}
        assert all(record["last_tick_at"].values())
        assert record["tick_interval_seconds"] == 2.0
    finally:
        for conn in conns.values():
            conn.close()


def test_poisoned_board_is_telemetry_logged_while_other_board_advances(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_db

    recipe = _recipe(tmp_path / "library")
    real_a = kanban_db.connect(board="board-a")
    board_b = kanban_db.connect(board="board-b")
    instantiate(
        real_a, board="board-a", recipe=recipe,
        parameters={}, instance_id="poisoned-a",
    )
    instantiate(
        board_b, board="board-b", recipe=recipe,
        parameters={}, instance_id="healthy-b",
    )
    _configure_tick(monkeypatch)
    telemetry = []
    monkeypatch.setattr("shipfactory.telemetry.append_jsonl", telemetry.append)

    class PoisonedConnection:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("poisoned board connection")

    try:
        result = daemon.run(
            {"board-a": PoisonedConnection(), "board-b": board_b},
            boards=["board-a", "board-b"], once=True,
        )

        assert "poisoned board connection" in result["boards"]["board-a"]["error"]
        assert _step_state("healthy-b") == "running"
        assert telemetry == [{
            "event": "daemon_board_tick_failure",
            "at": telemetry[0]["at"],
            "board": "board-a",
            "error": "poisoned board connection",
            "error_type": "OperationalError",
        }]
    finally:
        real_a.close()
        board_b.close()


def test_single_board_cli_delegates_connections_and_keeps_result_shape(monkeypatch):
    """The CLI must not pre-open a long-lived board connection: the production
    single-board daemon previously ran its whole life on one cached handle,
    which is the stale-WAL defect. conn=None hands connection lifecycle to the
    daemon (fresh per tick) while the bare single-board result shape stays."""
    calls = []

    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect",
        lambda *, board=None: calls.append(("connect", board)),
    )
    monkeypatch.setattr(
        daemon,
        "run",
        lambda conn, **kwargs: calls.append(("run", conn, kwargs)) or {"ok": True},
    )

    result = cli._daemon(argparse.Namespace(
        board=["solo"], boards=None, once=True, interval=5.0, sync_interval=None,
    ))

    assert result == {"ok": True}
    assert calls == [("run", None, {
        "board": "solo", "interval": 5.0, "once": True, "sync": False,
        "sync_interval": None, "require_recipes": False, "_lock_held": True,
    })]


def test_cli_accepts_comma_list_and_repeatable_board(monkeypatch):
    calls = []
    monkeypatch.setattr(
        daemon,
        "run",
        lambda conn, **kwargs: calls.append((conn, kwargs)) or {"ok": True},
    )

    cli._daemon(argparse.Namespace(
        board=["board-c"], boards=["board-a,board-b"], once=True,
        interval=5.0, sync_interval=None,
    ))

    assert calls[0][0] is None
    assert calls[0][1]["boards"] == ["board-c", "board-a", "board-b"]


class _Stop(Exception):
    """Sentinel to break the daemon loop after a fixed number of ticks."""


def test_daemon_opens_a_fresh_connection_per_tick_and_closes_it(monkeypatch):
    """Stale-WAL hygiene (Amendment G2): a daemon-opened board connection lives
    for exactly one tick. A cached long-lived connection keeps a dead inode after
    board heal/REINDEX/file swap and reads a frozen snapshot forever."""
    opened = []
    ticked = []

    class StubConnection:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    def _connect(*, board=None):
        conn = StubConnection()
        opened.append(conn)
        return conn

    sleeps = []

    def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise _Stop

    monkeypatch.setattr("hermes_cli.kanban_db.connect", _connect)
    monkeypatch.setattr(
        daemon, "tick", lambda conn, **kwargs: ticked.append(conn) or {"ok": True}
    )
    monkeypatch.setattr(daemon.time, "sleep", _sleep)

    try:
        daemon.run(None, board="fresh-board", interval=0.01)
    except _Stop:
        pass

    assert len(ticked) == 3
    assert len(opened) == 3, "expected one fresh connection per tick, got caching"
    assert [id(conn) for conn in ticked] == [id(conn) for conn in opened]
    assert [conn.closed for conn in opened] == [1, 1, 1]


def test_daemon_never_closes_borrowed_connections_even_on_tick_failure(monkeypatch):
    """Caller-provided connections are borrowed: the daemon must not close or
    discard them, even when a tick raises."""
    class Sentinel:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    conns = {"board-a": Sentinel(), "board-b": Sentinel()}

    def _tick(conn, *, board=None, **kwargs):
        if board == "board-a":
            raise RuntimeError("tick blew up")
        return {"ok": True}

    monkeypatch.setattr(daemon, "tick", _tick)
    telemetry = []
    monkeypatch.setattr("shipfactory.telemetry.append_jsonl", telemetry.append)

    result = daemon.run(conns, boards=["board-a", "board-b"], once=True)

    assert "tick blew up" in result["boards"]["board-a"]["error"]
    assert result["boards"]["board-b"] == {"ok": True}
    assert conns["board-a"].closed == 0
    assert conns["board-b"].closed == 0
    assert [record["event"] for record in telemetry] == ["daemon_board_tick_failure"]
