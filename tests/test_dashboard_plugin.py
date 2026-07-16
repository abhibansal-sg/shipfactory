"""Hermetic HTTP coverage for the Hermes Factory dashboard plugin."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shipfactory import store


PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _client() -> TestClient:
    spec = importlib.util.spec_from_file_location("factory_dashboard_plugin_test", PLUGIN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    return TestClient(app)


def _seed() -> None:
    store.init_db()
    # Dynamic timestamp: the /costs endpoint defaults to since_days=1, so a
    # hardcoded date turns this test into a time bomb the day after it's written.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    recipe = {
        "id": "factory-shakedown",
        "version": 1,
        "budgets": {"max_tokens": 1000},
    }
    normalized_recipe = json.dumps(
        recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    recipe_hash = hashlib.sha256(normalized_recipe.encode("utf-8")).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions VALUES(?,?,?,?,?,?)",
            ("factory-shakedown", 1, recipe_hash, "active", normalized_recipe, now),
        )
        db.execute(
            """INSERT INTO recipe_instances
            (id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,
             activation_count,tokens_charged,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("4d3d10d6", "factory-shakedown", "collector", "factory-shakedown", 1,
             recipe_hash, "running", "{}", 1, 200, now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("4d3d10d6", "build", 1, "agent_task", "ready", now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,input_revision_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("4d3d10d6", "approve", 1, "approval_gate", "waiting", "gate-task", "a" * 64, now, now),
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
    import shipfactory.config

    seat = SimpleNamespace(name="builder", profile="standard", executor="codex", model="gpt", reasoning="", reports_to=None, role="engineer", max_concurrent=1)
    monkeypatch.setattr(shipfactory.config, "load_seats", lambda: SimpleNamespace(seats={"builder": seat}))
    client = _client()

    instances = client.get("/api/plugins/shipfactory/instances")
    assert instances.status_code == 200
    item = instances.json()[0]
    assert item["id"] == "4d3d10d6"
    assert item["board"] == "factory-shakedown"
    assert item["step_states"] == {"ready": 1, "waiting": 1}
    assert item["tokens"] == {"charged": 200, "budget": 1000, "remaining": 800}

    detail = client.get("/api/plugins/shipfactory/instances/4d3d10d6")
    assert detail.status_code == 200
    assert len(detail.json()["steps"]) == 2
    assert detail.json()["activations"]["approve"][0]["state"] == "waiting"
    assert detail.json()["decisions"][0]["task_id"] == "gate-task"

    waiting = client.get("/api/plugins/shipfactory/waiting")
    assert waiting.status_code == 200
    assert waiting.json()[0]["step_id"] == "approve"
    assert client.get("/api/plugins/shipfactory/seats").json()[0]["name"] == "builder"
    assert client.get("/api/plugins/shipfactory/costs").json()[0]["tokens_total"] == 321

    tuple_fields = {
        "activation": 1, "revision_hash": "a" * 64,
        "evidence_bundle_hash": None, "nonce": "dashboard-nonce",
        "actor_kind": "operator", "actor_id": "test-operator", "channel": "dashboard",
    }
    approved = client.post("/api/plugins/shipfactory/approve", json={
        "instance": "4d3d10d6", "step": "approve", **tuple_fields,
    })
    assert approved.status_code == 200 and approved.json()["key"]
    with store._connect() as db:
        event = db.execute("SELECT source,payload_json,state FROM advance_events").fetchone()
    assert tuple(event)[0] == "gate_decision"
    assert json.loads(tuple(event)[1])["decision"] == "approve"
    assert tuple(event)[2] == "pending"

    # A ready build is not a human gate: both action endpoints fail cleanly.
    assert client.post("/api/plugins/shipfactory/approve", json={"instance": "4d3d10d6", "step": "build", **tuple_fields, "nonce": "build-a"}).status_code == 409
    assert client.post("/api/plugins/shipfactory/reject", json={"instance": "4d3d10d6", "step": "build", "reason": "no", **tuple_fields, "nonce": "build-r"}).status_code == 409


def test_seat_endpoints_create_update_and_reject_missing_profile(hermetic_hermes_home: Path):
    (hermetic_hermes_home / "profiles" / "builder").mkdir(parents=True)
    client = _client()
    assert client.get("/api/plugins/shipfactory/profiles").json() == ["default", "builder"]
    invalid = client.post("/api/plugins/shipfactory/seats", json={"name": "bad", "profile": "missing", "executor": "codex", "model": "gpt", "role": "engineer"})
    assert invalid.status_code == 400 and "does not exist" in invalid.json()["detail"]
    created = client.post("/api/plugins/shipfactory/seats", json={"name": "builder", "profile": "builder", "executor": "hermes", "model": "claude-sonnet-5", "role": "engineer", "provider_config": {"provider": "proxy", "base_url": "http://proxy", "model": "claude-sonnet-5"}})
    assert created.status_code == 201 and created.json()["name"] == "builder"
    updated = client.put("/api/plugins/shipfactory/seats/builder", json={"max_concurrent": 2})
    assert updated.status_code == 200 and updated.json()["max_concurrent"] == 2
    detail = client.get("/api/plugins/shipfactory/seats").json()[0]
    assert detail["profile_model"] == "claude-sonnet-5" and detail["model_mismatch"] is False


def test_waiting_gate_returns_complete_inert_operator_review_card(hermetic_hermes_home: Path):
    from shipfactory.recipes.instantiate import revision_vector

    store.init_db()
    now = store._now()
    base = "a" * 40
    recipe = {
        "schema": "shipfactory.recipe/v2", "id": "story-card", "version": 1,
        "status": "active", "description": "card", "intent_tags": ["test"],
        "supersedes": None, "parameters": {},
        "budgets": {
            "max_activations": 2, "max_tokens": 2,
            "step_activation_caps": {"story": 1}, "token_pools": {"review": 2},
        },
        "steps": [
            {
                "id": "story", "primitive": "agent_task", "title": "Story",
                "needs": [], "optional": False, "inputs": [],
                "outputs": [{"kind": "review-story", "schema": "shipfactory.review-story/v1", "path": ".shipfactory-output/story.json"}],
                "params": {"seat": "writer", "instructions": "story", "execution_profile": "review", "workspace": "worktree", "access_mode": "readonly", "environment": "source"},
            },
            {
                "id": "approval", "primitive": "approval_gate", "title": "Approve",
                "needs": ["story"], "optional": False,
                "inputs": [{"from": "story", "kind": "review-story", "required": True}],
                "outputs": [], "params": {"approvers": ["operator"], "instructions": "approve"},
            },
        ],
    }
    malicious = '<img src=x onerror="window.pwned=1">'
    story = {
        "schema": "shipfactory.review-story/v1", "instance_id": "story-card-instance",
        "revision_hash": "b" * 64, "task_spec_sha256": "c" * 64,
        "plan_sha256": "d" * 64, "change_set_sha256": "e" * 64,
        "evidence_bundle_sha256": "f" * 64, "headline": malicious,
        "changes": [{
            "importance": 1, "requirement_ids": ["REQ-1"],
            "files": ['src/<unsafe&"path>.py'], "why": malicious,
            "risk": "ui", "evidence_case_ids": ["case-1"],
        }],
        "generated_or_mechanical_files": [], "not_changed": [],
        "residual_risks": [malicious],
    }
    data = json.dumps(story, sort_keys=True, separators=(",", ":")).encode()
    sealed_path = hermetic_hermes_home / "story.json"
    sealed_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    normalized_recipe = json.dumps(
        recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    recipe_hash = hashlib.sha256(normalized_recipe.encode("utf-8")).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions VALUES(?,?,?,?,?,?)",
            ("story-card", 1, recipe_hash, "active", normalized_recipe, now),
        )
        db.execute(
            "INSERT INTO recipe_instances"
            "(id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,"
            "activation_count,tokens_charged,created_at,updated_at,base_sha) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("story-card-instance", "test", "collector", "story-card", 1, recipe_hash,
             "waiting_gate", "{}", 1, 1, now, now, base),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,output_revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("story-card-instance", "story", 1, "agent_task", "done", 1, now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("story-card-instance", "approval", 1, "approval_gate", "waiting", "gate", now, now),
        )
        db.execute(
            "INSERT INTO artifacts(id,instance_id,step_id,activation,kind,schema_version,state,"
            "candidate_path,sealed_path,sha256,size_bytes,producer,base_sha,head_sha,repo_tree_sha,created_at,sealed_at) "
            "VALUES(?,?,'story',1,'review-story',1,'sealed',?,?,?,?,'test',?,?,?,?,?)",
            ("story-artifact", "story-card-instance", ".shipfactory-output/story.json",
             str(sealed_path), digest, len(data), base, base, base, now, now),
        )
        approval = dict(db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id='story-card-instance' AND step_id='approval'",
        ).fetchone())
        revision = revision_vector(db, "story-card-instance", approval, recipe)
        db.execute(
            "UPDATE recipe_steps SET input_revision_hash=? WHERE instance_id='story-card-instance' AND step_id='approval'",
            (revision,),
        )

    client = _client()
    gate = client.get("/api/plugins/shipfactory/waiting").json()[0]
    assert gate["review_story"]["headline"] == "&lt;img src=x onerror=&quot;window.pwned=1&quot;&gt;"
    assert gate["review_story"]["changes"][0]["files"][0].startswith("src/&lt;unsafe")
    detail = client.get("/api/plugins/shipfactory/instances/story-card-instance").json()
    assert detail["review_story"] == gate["review_story"]

    bundle = (Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js").read_text()
    assert "function ReviewStoryCard" in bundle
    assert "gate.review_story" in bundle and "detail.review_story" in bundle
    assert "dangerouslySetInnerHTML" not in bundle and ".innerHTML" not in bundle
