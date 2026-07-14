"""Multi-board daemon multiplexing and isolation regressions."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from factory import cli, daemon, store
from factory.recipes.instantiate import instantiate
from factory.recipes.loader import load_library


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
        """schema: factory.recipe/v1
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
    from factory import config as factory_config
    from factory import watchdog
    from factory.recipes import advancer
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
    monkeypatch.setattr("factory.spawn.reap_finished", lambda: [])
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
    monkeypatch.setattr("factory.telemetry.append_jsonl", telemetry.append)

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


def test_single_board_cli_keeps_original_connection_and_result_shape(monkeypatch):
    calls = []

    class Connection:
        def close(self):
            calls.append(("close",))

    connection = Connection()
    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect",
        lambda *, board=None: calls.append(("connect", board)) or connection,
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
    assert calls[0] == ("connect", "solo")
    assert calls[1][0:2] == ("run", connection)
    assert calls[1][2]["board"] == "solo"
    assert "boards" not in calls[1][2]
    assert calls[2] == ("close",)


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
