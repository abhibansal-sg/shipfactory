from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from shipfactory import store


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API = ROOT / "dashboard" / "plugin_api.py"


def _client():
    spec = importlib.util.spec_from_file_location("overlay_dashboard_api", PLUGIN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    return TestClient(app)


def _recipe() -> dict:
    return {
        "schema": "shipfactory.recipe/v2",
        "id": "overlay-fixture",
        "version": 1,
        "status": "active",
        "description": "overlay fixture",
        "intent_tags": ["test"],
        "supersedes": None,
        "verdict_contract": "shipfactory.verdict/v2",
        "parameters": {},
        "budgets": {"max_activations": 8, "step_activation_caps": {"build": 3, "review": 2}},
        "steps": [
            {
                "id": "build", "primitive": "agent_task", "title": "Build",
                "needs": [], "optional": False, "inputs": [],
                "outputs": [{"kind": "build-result", "schema": "shipfactory.build-result/v1", "path": ".shipfactory-output/build.json"}],
                "params": {"seat": "builder", "instructions": "build", "execution_profile": "build", "workspace": "worktree", "access_mode": "workspace_write", "environment": "source"},
            },
            {
                "id": "review", "primitive": "review_gate", "title": "Review",
                "needs": ["build"], "optional": False,
                "inputs": [{"from": "build", "kind": "build-result", "required": True}], "outputs": [],
                "params": {"seat": "reviewer", "instructions": "review", "execution_profile": "review", "workspace": "worktree", "access_mode": "readonly", "environment": "source"},
            },
            {
                "id": "approval", "primitive": "approval_gate", "title": "Approval",
                "needs": ["review"], "optional": False, "inputs": [], "outputs": [],
                "params": {"approvers": ["operator"], "instructions": "operator only"},
            },
        ],
    }


def _configure(monkeypatch, flags=None):
    overrides = flags if flags is not None else {}
    recipe = _recipe()
    settings = {
        "enabled": True, "graph_enabled": True, "live_overlay_enabled": True,
        "history_enabled": True, "history_fold_threshold": 1,
    }
    settings.update(overrides)

    import shipfactory.config

    monkeypatch.setattr(
        shipfactory.config,
        "load_seats",
        lambda: SimpleNamespace(
            seats={"builder": object(), "reviewer": object(), "operator": object()},
            recipes={
                "library_path": str(ROOT / "recipes"),
                "execution_profiles": {"build": {}, "review": {}},
                "projects_visual_recipes": settings,
            },
        ),
    )
    return recipe


def _seed(recipe: dict, *, malformed: bool = False) -> str:
    normalized = json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    recipe_hash = hashlib.sha256(normalized.encode()).hexdigest()
    now = store._now()
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions(id,version,hash,status,normalized_yaml,created_at) VALUES(?,?,?,?,?,?)",
            (recipe["id"], 1, recipe_hash, "active", normalized, now),
        )
        db.execute(
            """INSERT INTO recipe_instances
            (id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,project_id,linear_issue_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("overlay-1", "secret-board", "collector", recipe["id"], 1, recipe_hash,
             "waiting_gate", "{}", "project-1", "SF-1", now, now),
        )
        verdict = json.dumps({
            "schema": "shipfactory.verdict/v2", "outcome": "request_changes", "clean": False,
            "target_step": "build", "findings": [{"severity": "blocker", "location": "src/a.py:1", "summary": "fix"}],
            "summary": "one finding",
        }, separators=(",", ":"))
        rows = [
            ("build", 1, "agent_task", "done", "task-build-1", None, None, None, None),
            ("build", 2, "agent_task", "done", "task-build-2", "review", 1, 1, verdict),
            ("review", 1, "review_gate", "done", "task-review-1", None, None, 1, verdict),
            ("review", 2, "review_gate", "done", "task-review-2", None, None, 0, "{not-json" if malformed else None),
            ("approval", 1, "approval_gate", "waiting", "task-approval-1", None, None, None, None),
        ]
        for step_id, activation, primitive, state, task_id, rejected_by, rejected_activation, findings, verdict_json in rows:
            db.execute(
                """INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,
                   rejected_by_step_id,rejected_by_activation,finding_count,verdict_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("overlay-1", step_id, activation, primitive, state, task_id, rejected_by,
                 rejected_activation, findings, verdict_json, now, now),
            )
        db.execute(
            "INSERT INTO runs(task_id,seat,executor,model,started_at,result,recipe_activation) VALUES(?,?,?,?,?,?,?)",
            ("task-build-1", "builder", "codex", "gpt-test", now, "done", 1),
        )
    return recipe_hash


def test_instance_overlay_projects_latest_history_router_actor_blocker_receipts(monkeypatch):
    recipe = _configure(monkeypatch)
    digest = _seed(recipe)
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()

    assert body["graph"]["source"]["recipe_hash"] == digest
    assert body["graph"]["schema_version"] == "shipfactory.graph/v1"
    assert set(body) == {
        "graph", "instance", "next_actor", "blocker", "nodes", "history",
        "rework_edges", "receipts", "evidence",
    }
    assert "source" not in body
    assert {row["step_id"] for row in body["nodes"]} == {"build", "review", "approval", "review:verdict"}
    build = next(row for row in body["nodes"] if row["step_id"] == "build")
    assert build["current_activation"] == 2 and build["attempts"] == 2
    assert "run not yet recorded" in build["actor"]["label"]
    assert "model" not in json.dumps(build["actor"]) and "provider" not in json.dumps(build["actor"])
    assert body["next_actor"]["kind"] == "operator"
    assert body["blocker"]["reason"] == "human action required"
    assert body["receipts"] == {"available": True, "endpoint": "/instances/overlay-1/receipts"}
    assert body["evidence"] == {"status": "unavailable", "items": []}
    assert body["rework_edges"][0]["from"] == "review:verdict"
    assert body["rework_edges"][0]["to"] == "build"
    history = body["history"]["items"]
    assert len(history) == 5
    assert [row["activation"] for row in history if row["step_id"] == "build"] == [1, 2]
    assert history[1]["finding_count"] == 1
    assert "secret-board" not in json.dumps(body)


def test_overlay_flags_and_history_are_fresh_and_disabled_explicitly(monkeypatch):
    recipe = _configure(monkeypatch, {"live_overlay_enabled": False})
    _seed(recipe)
    client = _client()
    response = client.get("/api/plugins/shipfactory/instances/overlay-1/graph")
    assert response.status_code == 200
    body = response.json()
    assert body["graph"]["source"]["recipe_hash"]
    assert body["instance"] == {
        "instance_id": "overlay-1", "project_id": None, "status": None,
        "linear_issue_id": None,
    }
    assert body["nodes"] == []
    assert body["next_actor"] is None and body["blocker"] is None
    assert body["history"]["items"] == []
    assert body["history"]["reason"] == "live_overlay_disabled"
    assert body["rework_edges"] == []
    assert body["receipts"]["available"] is False

    # The next request re-loads the changed settings and exposes no history rows.
    import shipfactory.config
    shipfactory.config.load_seats().recipes["projects_visual_recipes"]["live_overlay_enabled"] = True
    shipfactory.config.load_seats().recipes["projects_visual_recipes"]["history_enabled"] = False
    body = client.get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["history"]["enabled"] is False
    assert body["history"]["items"] == []
    assert body["history"]["reason"] == "disabled"


def test_disabled_graph_does_not_initialize_or_read_overlay_state(monkeypatch):
    recipe = _configure(monkeypatch, {"live_overlay_enabled": False})
    _seed(recipe)

    def fail_init_db():
        raise AssertionError("read-only graph route must not initialize the database")

    monkeypatch.setattr(store, "init_db", fail_init_db)
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph")
    assert body.status_code == 200
    assert body.json()["nodes"] == []


def test_instance_graph_still_enforces_global_and_graph_flags(monkeypatch):
    recipe = _configure(monkeypatch, {"enabled": False})
    _seed(recipe)
    client = _client()
    response = client.get("/api/plugins/shipfactory/instances/overlay-1/graph")
    assert response.status_code == 403
    assert response.json()["error"] == "feature_disabled"

    import shipfactory.config
    settings = shipfactory.config.load_seats().recipes["projects_visual_recipes"]
    settings["enabled"] = True
    settings["graph_enabled"] = False
    response = client.get("/api/plugins/shipfactory/instances/overlay-1/graph")
    assert response.status_code == 403
    assert response.json()["error"] == "feature_disabled"


def _update_latest(instance_id: str, step_id: str, state: str, reason: str | None = None) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state=?, blocked_reason=? "
            "WHERE instance_id=? AND step_id=? AND activation=("
            "SELECT MAX(activation) FROM recipe_steps WHERE instance_id=? AND step_id=?)",
            (state, reason, instance_id, step_id, instance_id, step_id),
        )


def test_blocked_state_wins_dependency_waiting_and_dependency_waiting_is_not_a_blocker(monkeypatch):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    _update_latest("overlay-1", "build", "pending")
    _update_latest("overlay-1", "review", "blocked", "persisted review failure")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    build = next(item for item in body["nodes"] if item["step_id"] == "build")
    review = next(item for item in body["nodes"] if item["step_id"] == "review")
    assert build["blocker"] is None
    assert review["blocker"] == {
        "kind": "blocked", "reason": "persisted review failure",
        "step_id": "review", "activation": 2,
    }


def test_next_actor_prefers_state_priority_then_graph_order(monkeypatch):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    _update_latest("overlay-1", "build", "pending")
    _update_latest("overlay-1", "review", "ready")
    _update_latest("overlay-1", "approval", "waiting")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["next_actor"]["step_id"] == "review"

    _update_latest("overlay-1", "build", "running")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["next_actor"]["step_id"] == "build"

    _update_latest("overlay-1", "build", "waiting")
    _update_latest("overlay-1", "review", "blocked", "review needs input")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["next_actor"]["step_id"] == "build"
    assert body["next_actor"]["kind"] == "seat"
    assert "model" not in json.dumps(body["next_actor"])


@pytest.mark.parametrize("waiting_state", ["waiting", "waiting_gate", "needs_input"])
def test_approval_waiting_variants_surface_human_action_blocker(monkeypatch, waiting_state):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    _update_latest("overlay-1", "approval", waiting_state)
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    approval = next(item for item in body["nodes"] if item["step_id"] == "approval")
    assert approval["blocker"]["kind"] == "approval"
    assert body["next_actor"]["kind"] == "operator"
    assert body["blocker"]["reason"] == "human action required"


@pytest.mark.parametrize("state", ["blocked", "worker_blocked", "failed"])
def test_persisted_failure_states_surface_node_blockers(monkeypatch, state):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    _update_latest("overlay-1", "build", state, "persisted worker failure")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    build = next(item for item in body["nodes"] if item["step_id"] == "build")
    assert build["blocker"] is not None
    assert build["blocker"]["reason"] == "persisted worker failure"


def test_terminal_failed_node_remains_top_level_blocker_without_next_actor(monkeypatch):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    _update_latest("overlay-1", "build", "failed", "terminal build failure")
    _update_latest("overlay-1", "review", "done")
    _update_latest("overlay-1", "approval", "done")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["next_actor"] is None
    assert body["blocker"] == {
        "kind": "failed", "reason": "terminal build failure",
        "step_id": "build", "activation": 2,
    }


def test_unknown_state_has_no_authoritative_next_actor(monkeypatch):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    _update_latest("overlay-1", "build", "mystery_state")
    _update_latest("overlay-1", "review", "done")
    _update_latest("overlay-1", "approval", "done")
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["next_actor"] is None
    assert body["blocker"] == {
        "kind": "unknown", "reason": "unknown step state",
        "step_id": "build", "activation": 2,
    }


def test_rework_edges_reject_projection_sources(monkeypatch):
    recipe = _configure(monkeypatch)
    _seed(recipe)
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET rejected_by_step_id='review:verdict' "
            "WHERE instance_id='overlay-1' AND step_id='build' AND activation=2"
        )
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    assert body["rework_edges"] == []


def test_malformed_verdict_is_not_guessed(monkeypatch):
    recipe = _configure(monkeypatch)
    _seed(recipe, malformed=True)
    body = _client().get("/api/plugins/shipfactory/instances/overlay-1/graph").json()
    row = next(item for item in body["history"]["items"] if item["step_id"] == "review" and item["activation"] == 2)
    assert row["verdict"] is None
    assert row["verdict_status"] == "malformed"
