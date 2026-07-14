"""Hermetic HTTP coverage for the Hermes Factory dashboard plugin."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory import store


PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _client() -> TestClient:
    spec = importlib.util.spec_from_file_location("factory_dashboard_plugin_test", PLUGIN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/factory")
    return TestClient(app)


def _seed() -> None:
    store.init_db()
    now = "2026-07-14T00:00:00+00:00"
    recipe = {
        "id": "factory-shakedown",
        "version": 1,
        "budgets": {"max_tokens": 1000},
    }
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions VALUES(?,?,?,?,?,?)",
            ("factory-shakedown", 1, "recipe-hash", "active", json.dumps(recipe), now),
        )
        db.execute(
            """INSERT INTO recipe_instances
            (id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,
             activation_count,tokens_charged,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("4d3d10d6", "factory-shakedown", "collector", "factory-shakedown", 1, "recipe-hash", "running", "{}", 1, 200, now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("4d3d10d6", "build", 1, "agent_task", "ready", now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("4d3d10d6", "approve", 1, "approval_gate", "waiting", "gate-task", now, now),
        )
        db.execute(
            "INSERT INTO decisions(task_id,stage_id,stage_type,seat,outcome,body,at) VALUES(?,?,?,?,?,?,?)",
            ("gate-task", "approve", "approval_gate", "operator", "pending", "", now),
        )
        db.execute(
            "INSERT INTO runs(task_id,seat,executor,model,started_at,tokens_total) VALUES(?,?,?,?,?,?)",
            ("task-1", "builder", "codex", "gpt", now, 321),
        )


def test_dashboard_plugin_routes_are_readable_and_queue_decisions(monkeypatch):
    _seed()
    import factory.config

    seat = SimpleNamespace(name="builder", profile="standard", executor="codex", model="gpt", reasoning="", reports_to=None, role="engineer", max_concurrent=1)
    monkeypatch.setattr(factory.config, "load_seats", lambda: SimpleNamespace(seats={"builder": seat}))
    client = _client()

    instances = client.get("/api/plugins/factory/instances")
    assert instances.status_code == 200
    item = instances.json()[0]
    assert item["id"] == "4d3d10d6"
    assert item["board"] == "factory-shakedown"
    assert item["step_states"] == {"ready": 1, "waiting": 1}
    assert item["tokens"] == {"charged": 200, "budget": 1000, "remaining": 800}

    detail = client.get("/api/plugins/factory/instances/4d3d10d6")
    assert detail.status_code == 200
    assert len(detail.json()["steps"]) == 2
    assert detail.json()["activations"]["approve"][0]["state"] == "waiting"
    assert detail.json()["decisions"][0]["task_id"] == "gate-task"

    waiting = client.get("/api/plugins/factory/waiting")
    assert waiting.status_code == 200
    assert waiting.json()[0]["step_id"] == "approve"
    assert client.get("/api/plugins/factory/seats").json()[0]["name"] == "builder"
    assert client.get("/api/plugins/factory/costs").json()[0]["tokens_total"] == 321

    approved = client.post("/api/plugins/factory/approve", json={"instance": "4d3d10d6", "step": "approve"})
    assert approved.status_code == 200 and approved.json()["key"]
    with store._connect() as db:
        event = db.execute("SELECT source,payload_json,state FROM advance_events").fetchone()
    assert tuple(event)[0] == "gate_decision"
    assert json.loads(tuple(event)[1])["decision"] == "approve"
    assert tuple(event)[2] == "pending"

    # A ready build is not a human gate: both action endpoints fail cleanly.
    assert client.post("/api/plugins/factory/approve", json={"instance": "4d3d10d6", "step": "build"}).status_code == 400
    assert client.post("/api/plugins/factory/reject", json={"instance": "4d3d10d6", "step": "build", "reason": "no"}).status_code == 400


def test_seat_endpoints_create_update_and_reject_missing_profile(hermetic_hermes_home: Path):
    (hermetic_hermes_home / "profiles" / "builder").mkdir(parents=True)
    client = _client()
    assert client.get("/api/plugins/factory/profiles").json() == ["default", "builder"]
    invalid = client.post("/api/plugins/factory/seats", json={"name": "bad", "profile": "missing", "executor": "codex", "model": "gpt", "role": "engineer"})
    assert invalid.status_code == 400 and "does not exist" in invalid.json()["detail"]
    created = client.post("/api/plugins/factory/seats", json={"name": "builder", "profile": "builder", "executor": "hermes", "model": "claude-sonnet-5", "role": "engineer", "provider_config": {"provider": "proxy", "base_url": "http://proxy", "model": "claude-sonnet-5"}})
    assert created.status_code == 201 and created.json()["name"] == "builder"
    updated = client.put("/api/plugins/factory/seats/builder", json={"max_concurrent": 2})
    assert updated.status_code == 200 and updated.json()["max_concurrent"] == 2
    detail = client.get("/api/plugins/factory/seats").json()[0]
    assert detail["profile_model"] == "claude-sonnet-5" and detail["model_mismatch"] is False
