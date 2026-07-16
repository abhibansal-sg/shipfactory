"""Hermetic HTTP coverage for the Factory dashboard write surface."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shipfactory import store


PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _client() -> TestClient:
    spec = importlib.util.spec_from_file_location(
        "factory_dashboard_write_test", PLUGIN_API
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    return TestClient(app)


def _recipe_text(recipe_id: str, first_step: str) -> str:
    return f"""schema: shipfactory.recipe/v1
id: {recipe_id}
version: 1
status: active
description: Dashboard write fixture for {recipe_id}.
intent_tags: [test]
supersedes: null
parameters:
  request: {{type: string, required: true, default: null}}
  effort: {{type: integer, required: false, default: 2}}
  urgent: {{type: boolean, required: false, default: false}}
  lane: {{type: enum, required: false, default: standard, values: [standard, expedited]}}
  due_at: {{type: datetime, required: false, default: null}}
budgets: {{max_activations: 4, max_step_activations: 2, max_tokens: 50000}}
steps:
  - id: {first_step}
    primitive: notify
    title: Primary notification
    needs: []
    optional: false
    params: {{target: test, message: "Run ${{request}}"}}
  - id: extra
    primitive: notify
    title: Optional notification
    needs: [{first_step}]
    optional: true
    params: {{target: test, message: Optional}}
"""


def _configure_library(tmp_path: Path, monkeypatch) -> Path:
    library = tmp_path / "recipes"
    library.mkdir()
    (library / "route-a@1.yaml").write_text(
        _recipe_text("route-a", "first"), encoding="utf-8"
    )
    (library / "route-b@1.yaml").write_text(
        _recipe_text("route-b", "replacement"), encoding="utf-8"
    )
    recipes = {
        "enabled": True,
        "library_path": str(library),
        "bare_task_recipe": "route-a@1",
        "notify_target": "test",
        "board_day_token_ceiling": 100000,
        "dispatcher_max_in_progress": 2,
        "execution_profiles": {},
    }
    import shipfactory.config

    monkeypatch.setattr(
        shipfactory.config,
        "load_seats",
        lambda: SimpleNamespace(seats={}, recipes=recipes),
    )
    return library


def _payload(recipe: str = "route-a") -> dict:
    return {
        "recipe": recipe,
        "version": 1,
        "board": "default",
        "parameters": {
            "request": "ship dashboard controls",
            "effort": 3,
            "urgent": True,
            "lane": "expedited",
            "due_at": "2026-07-15T12:00:00+00:00",
        },
        "skip_steps": ["extra"],
    }


def test_recipes_endpoint_lists_schema_and_optional_steps(tmp_path, monkeypatch):
    _configure_library(tmp_path, monkeypatch)

    response = _client().get("/api/plugins/shipfactory/recipes")

    assert response.status_code == 200
    recipe = response.json()[0]
    assert recipe["id"] == "route-a"
    assert recipe["parameters"]["effort"]["type"] == "integer"
    assert recipe["parameters"]["lane"]["values"] == ["standard", "expedited"]
    assert recipe["optional_steps"] == [{"id": "extra", "title": "Optional notification"}]


def test_instances_endpoint_instantiates_and_rejects_invalid_parameters(
    tmp_path, monkeypatch
):
    _configure_library(tmp_path, monkeypatch)
    client = _client()

    created = client.post("/api/plugins/shipfactory/instances", json=_payload())

    assert created.status_code == 200
    instance_id = created.json()["instance_id"]
    assert created.json()["parameters"]["urgent"] is True
    with store._connect() as db:
        skipped = db.execute(
            "SELECT state FROM recipe_steps WHERE instance_id=? AND step_id='extra'",
            (instance_id,),
        ).fetchone()
        before = db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0]
    assert skipped["state"] == "skipped"

    invalid = _payload()
    invalid["parameters"]["effort"] = "three"
    rejected = client.post("/api/plugins/shipfactory/instances", json=invalid)

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "parameter effort has wrong type"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == before


def test_triage_endpoint_creates_a_real_triage_task(tmp_path, monkeypatch):
    _configure_library(tmp_path, monkeypatch)
    client = _client()

    response = client.post(
        "/api/plugins/shipfactory/triage",
        json={"title": "Investigate release blocker", "body": "Collect context", "board": "default"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "triage"
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board="default")
    try:
        task = kanban_db.get_task(conn, response.json()["task_id"])
        assert task.title == "Investigate release blocker"
        assert task.body == "Collect context"
        assert task.status == "triage"
    finally:
        conn.close()


def test_reroute_endpoint_uses_cli_replacement_path(tmp_path, monkeypatch):
    _configure_library(tmp_path, monkeypatch)
    client = _client()
    created = client.post("/api/plugins/shipfactory/instances", json=_payload()).json()

    response = client.post(
        f"/api/plugins/shipfactory/instances/{created['instance_id']}/reroute",
        json={
            "recipe": "route-b",
            "version": 1,
            "parameters": _payload()["parameters"],
        },
    )

    assert response.status_code == 200
    assert response.json()["activated"] is False
    assert response.json()["replacement"]["instance_id"] == created["instance_id"]
    with store._connect() as db:
        instance = db.execute(
            "SELECT recipe_id FROM recipe_instances WHERE id=?",
            (created["instance_id"],),
        ).fetchone()
    assert instance["recipe_id"] == "route-b"


def test_cancel_requires_preview_then_explicit_confirm(tmp_path, monkeypatch):
    _configure_library(tmp_path, monkeypatch)
    client = _client()
    created = client.post("/api/plugins/shipfactory/instances", json=_payload()).json()
    instance_id = created["instance_id"]
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board="default")
    try:
        task_id = kanban_db.create_task(conn, title="Active recipe work")
    finally:
        conn.close()
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='running',kanban_task_id=? "
            "WHERE instance_id=? AND step_id='first'",
            (task_id, instance_id),
        )

    from shipfactory.spawn import _RUNNING

    _RUNNING[98765] = {
        "task_id": task_id,
        "proc": SimpleNamespace(pid=98765),
        "executor": "codex",
    }
    try:
        preview = client.get(
            f"/api/plugins/shipfactory/instances/{instance_id}/cancel"
        )
    finally:
        _RUNNING.pop(98765, None)

    assert preview.status_code == 200
    assert preview.json()["suppressed"] == [task_id]
    assert preview.json()["workers"] == [
        {"task_id": task_id, "pid": 98765, "executor": "codex"}
    ]
    with store._connect() as db:
        assert db.execute(
            "SELECT status FROM recipe_instances WHERE id=?", (instance_id,)
        ).fetchone()["status"] == "running"

    confirmed = client.post(
        f"/api/plugins/shipfactory/instances/{instance_id}/cancel"
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "cancelled"
    with store._connect() as db:
        assert db.execute(
            "SELECT status FROM recipe_instances WHERE id=?", (instance_id,)
        ).fetchone()["status"] == "cancelled"


def test_status_endpoint_reports_stopped_and_live_daemon(tmp_path, monkeypatch):
    _configure_library(tmp_path, monkeypatch)
    client = _client()

    stopped = client.get("/api/plugins/shipfactory/status")

    assert stopped.status_code == 200
    assert stopped.json() == {
        "running": False,
        "pid": None,
        "last_tick_at": None,
        "board": None,
        "boards": [],
        "tick_interval_seconds": 5.0,
        "config": {
            "recipes_enabled": True,
            "library_path": str(tmp_path / "recipes"),
            "bare_task_recipe": "route-a@1",
        },
    }

    run_id = store.record_daemon_start("default", os.getpid())
    ticked_at = store.record_daemon_tick(run_id, "default")
    running = client.get("/api/plugins/shipfactory/status")

    assert running.status_code == 200
    assert running.json()["running"] is True
    assert running.json()["pid"] == os.getpid()
    assert running.json()["last_tick_at"] == ticked_at
    assert running.json()["board"] == "default"
    assert running.json()["boards"][0]["board"] == "default"
    assert running.json()["boards"][0]["stale"] is False

    store.record_daemon_start("default", 999999)
    monkeypatch.setattr(os, "kill", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    stale = client.get("/api/plugins/shipfactory/status")
    assert stale.json()["running"] is False
    assert stale.json()["pid"] is None


def test_status_uses_daemon_boards_and_marks_ticks_over_three_intervals_stale(
    tmp_path, monkeypatch
):
    _configure_library(tmp_path, monkeypatch)
    run_id = store.record_daemon_start(
        "served-a", os.getpid(), boards=["served-a", "served-b"], tick_interval=10,
    )
    store.record_daemon_tick(run_id, "served-a")
    store.record_daemon_tick(run_id, "served-b")
    with store._connect() as db:
        row = db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()
        payload = json.loads(row["result"])
        payload["last_tick_at"]["served-b"] = "2020-01-01T00:00:00+00:00"
        db.execute(
            "UPDATE runs SET result=? WHERE id=?",
            (json.dumps(payload), run_id),
        )

    status = _client().get("/api/plugins/shipfactory/status").json()

    assert status["board"] == "served-a"
    assert [item["board"] for item in status["boards"]] == ["served-a", "served-b"]
    assert status["boards"][0]["stale"] is False
    assert status["boards"][1]["stale"] is True
    assert status["boards"][1]["last_tick_age_seconds"] > 30
