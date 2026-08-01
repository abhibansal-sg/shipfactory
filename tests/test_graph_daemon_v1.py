"""GraphRunner v1 daemon composition tests (Milestone C Task 14).

The unchanged singleton boundary is exercised by
``test_a0_single_writer.py::test_second_daemon_process_exits_before_opening_its_board``.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from shipfactory import (
    config, daemon, graph_runner, graph_runtime, spawn, store, watchdog,
)
from shipfactory.graph_recipe import validate
from hermes_cli import kanban_db


class _CountConnection:
    def execute(self, _sql, _parameters=()):
        return self

    def fetchone(self):
        return (0,)


def _install_tick_shell(monkeypatch, calls):
    cfg = SimpleNamespace(company="board", recipes={})
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda required=False: cfg)
    monkeypatch.setattr(config, "recipe_runtime_config", lambda _recipes: {"max_workers": 2})
    monkeypatch.setattr(
        spawn, "restore_running",
        lambda **_kwargs: calls.append("restore") or {"restored": [], "crashed": []},
    )
    reaps = iter(([], [], [], []))
    monkeypatch.setattr(
        spawn, "reap_finished", lambda: calls.append("reap") or next(reaps),
    )
    dispatched = SimpleNamespace(
        skipped_nonspawnable=[], spawned=[], respawn_guarded=[],
    )
    monkeypatch.setattr(
        kanban_db, "dispatch_once",
        lambda *_args, **_kwargs: calls.append("legacy") or dispatched,
    )
    monkeypatch.setattr(store, "available_resource_units", lambda *_args: 2)
    monkeypatch.setattr(
        watchdog, "tick", lambda *_args, **_kwargs: calls.append("watchdog") or "ok",
    )
    monkeypatch.setattr(daemon, "_board_db_health_pass", lambda *_args: None)
    return dispatched


def test_empty_graph_tick_is_a_noop_without_seat_configuration(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert daemon._tick_graph_v1(board="empty", max_workers=2) == {
        "events": {"leased": 0, "applied": 0, "discarded": 0, "failed": 0},
        "reconciled": [],
        "spawned": [],
    }


def test_graph_tick_reconciles_and_spawns_only_its_board(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    recipe = validate({
        "name": "board-isolation",
        "start": "approve",
        "boxes": [
            {
                "id": "approve",
                "name": "Approve",
                "who": "human",
                "instructions": "Choose the declared result.",
            },
            {
                "id": "finish",
                "name": "Finish",
                "who": "human",
                "instructions": "Acknowledge completion.",
                "end": True,
            },
        ],
        "arrows": [{"from": "approve", "result": "approved", "to": ["finish"]}],
    })
    runs = {
        board: graph_runner.start_run(
            project_id=f"project-{board}", board=board, recipe=recipe,
            request_text=f"Review {board}", workspace_path=str(workspace),
            launch_key=f"board-{board}",
        )
        for board in ("a", "b")
    }
    with store._connect() as db:
        graph_runner.reconcile_run(db, runs["b"]["id"])
    assert graph_runtime.spawn_ready(1, board="b") == []
    with store._connect() as db:
        b_attempt = db.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (runs["b"]["id"],),
        ).fetchone()
    event_key = "board-b:human-completed"
    store.enqueue_run_event_v1(
        key=event_key,
        run_id=runs["b"]["id"],
        source="human",
        payload={
            "type": "box_completed",
            "run_id": runs["b"]["id"],
            "attempt_id": b_attempt["id"],
            "expected_state": "waiting_human",
            "result": "approved",
            "work": "approved on board b",
        },
    )

    result = daemon._tick_graph_v1(board="a", max_workers=2)

    assert [item["run_id"] for item in result["reconciled"]] == [runs["a"]["id"]]
    with store._connect() as db:
        states = {
            board: db.execute(
                "SELECT state FROM box_attempts_v1 WHERE run_id=?",
                (run["id"],),
            ).fetchone()
            for board, run in runs.items()
        }
    assert states["a"]["state"] == "waiting_human"
    assert states["b"]["state"] == "waiting_human"
    with store._connect() as db:
        event = db.execute(
            "SELECT state FROM run_events_v1 WHERE key=?", (event_key,),
        ).fetchone()
    assert event["state"] == "pending"


def test_tick_orders_graph_before_unchanged_legacy_path(monkeypatch):
    calls = []
    dispatched = _install_tick_shell(monkeypatch, calls)
    monkeypatch.setattr(
        daemon, "_tick_graph_v1",
        lambda **_kwargs: calls.append("graph") or {
            "events": {"leased": 0, "applied": 0, "discarded": 0, "failed": 0},
            "reconciled": [],
            "spawned": [],
        },
    )

    result = daemon.tick(_CountConnection(), board="board")

    assert result["graph"] == {
        "events": {"leased": 0, "applied": 0, "discarded": 0, "failed": 0},
        "reconciled": [],
        "spawned": [],
    }
    assert result["dispatch"] is dispatched
    assert calls.index("reap") < calls.index("graph") < calls.index("legacy")
    assert calls == ["restore", "reap", "graph", "legacy", "reap", "watchdog"]


def test_graph_mode_advances_real_graph_run_without_mutating_legacy_instance(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    recipe = validate({
        "name": "graph-only-integration",
        "start": "approve",
        "boxes": [{
            "id": "approve",
            "name": "Approve",
            "who": "human",
            "instructions": "Choose the declared result.",
            "end": True,
        }],
        "arrows": [],
    })
    run = graph_runner.start_run(
        project_id="project-graph", board="graph-board", recipe=recipe,
        request_text="Prove graph-only mode", workspace_path=str(workspace),
        launch_key="graph-only-integration",
    )
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_instances "
            "(id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,"
            "parameters_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-frozen", "graph-board", "legacy-task", "legacy", 1,
                "legacy-hash", "blocked", "{}", "before", "before",
            ),
        )
        before = dict(db.execute(
            "SELECT * FROM recipe_instances WHERE id='legacy-frozen'"
        ).fetchone())

    cfg = SimpleNamespace(company="graph-board", recipes={"runner_mode": "graph"})
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda required=False: cfg)
    monkeypatch.setattr(
        spawn, "restore_running", lambda **_kwargs: {"restored": [], "crashed": []},
    )
    monkeypatch.setattr(spawn, "reap_finished", lambda: [])
    monkeypatch.setattr(daemon, "_board_db_health_pass", lambda *_args: None)

    conn = sqlite3.connect(":memory:")
    try:
        result = daemon.tick(conn, board="graph-board")
    finally:
        conn.close()

    assert result["dispatch"] is None
    assert [item["run_id"] for item in result["graph"]["reconciled"]] == [run["id"]]
    with store._connect() as db:
        attempt = db.execute(
            "SELECT state FROM box_attempts_v1 WHERE run_id=?", (run["id"],)
        ).fetchone()
        after = dict(db.execute(
            "SELECT * FROM recipe_instances WHERE id='legacy-frozen'"
        ).fetchone())
    assert attempt["state"] == "waiting_human"
    assert after == before


def test_mixed_to_graph_mode_flip_stops_legacy_on_next_tick(monkeypatch):
    calls = []
    dispatched = _install_tick_shell(monkeypatch, calls)
    modes = iter(("mixed", "graph"))
    monkeypatch.setattr(
        config,
        "recipe_runtime_config",
        lambda _recipes: {"max_workers": 2, "runner_mode": next(modes)},
    )
    monkeypatch.setattr(
        daemon, "_tick_graph_v1",
        lambda **_kwargs: calls.append("graph") or {
            "events": {"leased": 0, "applied": 0, "discarded": 0, "failed": 0},
            "reconciled": [],
            "spawned": [],
        },
    )

    mixed = daemon.tick(_CountConnection(), board="board")
    graph = daemon.tick(_CountConnection(), board="board")

    assert mixed["dispatch"] is dispatched
    assert graph["dispatch"] is None
    assert calls.count("graph") == 2
    assert calls.count("legacy") == 1
    assert calls.count("watchdog") == 1


def test_graph_failure_is_reported_without_suppressing_legacy_tick(monkeypatch):
    calls = []
    dispatched = _install_tick_shell(monkeypatch, calls)

    def fail_graph(**_kwargs):
        calls.append("graph")
        raise RuntimeError("graph board failed")

    monkeypatch.setattr(daemon, "_tick_graph_v1", fail_graph)

    result = daemon.tick(_CountConnection(), board="board")

    assert result["graph"] == {
        "error": "graph board failed",
        "error_type": "RuntimeError",
    }
    assert result["dispatch"] is dispatched
    assert calls == ["restore", "reap", "graph", "legacy", "reap", "watchdog"]


def test_real_graph_human_run_and_legacy_dispatch_advance_in_same_tick(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    recipe = validate({
        "name": "daemon-human",
        "start": "approve",
        "boxes": [{
            "id": "approve",
            "name": "Approve",
            "who": "human",
            "instructions": "Choose the declared result.",
            "end": True,
        }],
        "arrows": [],
    })
    run = graph_runner.start_run(
        project_id="project",
        board="board",
        recipe=recipe,
        request_text="Review this",
        workspace_path=str(workspace),
        launch_key="mixed-tick",
    )
    calls = []
    dispatched = _install_tick_shell(monkeypatch, calls)

    result = daemon.tick(_CountConnection(), board="board")

    assert result["dispatch"] is dispatched
    assert "error" not in result["graph"]
    assert result["graph"]["spawned"] == []
    assert result["graph"]["reconciled"][0]["run_id"] == run["id"]
    with store._connect() as db:
        attempt = db.execute(
            "SELECT state FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone()
    assert attempt["state"] == "waiting_human"
    assert calls == ["restore", "reap", "legacy", "reap", "watchdog"]
