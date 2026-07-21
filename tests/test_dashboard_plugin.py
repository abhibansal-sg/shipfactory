"""Hermetic HTTP coverage for the Hermes Factory dashboard plugin."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import re
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
    # A missing profile is rejected only for a hermes seat (its `hermes -p`
    # argv needs it). A non-hermes seat is decoupled from the profiles dir.
    invalid = client.post("/api/plugins/shipfactory/seats", json={"name": "bad", "profile": "missing", "executor": "hermes", "model": "gpt", "role": "engineer"})
    assert invalid.status_code == 400 and "does not exist" in invalid.json()["detail"]
    decoupled = client.post("/api/plugins/shipfactory/seats", json={"name": "spec-author", "executor": "codex", "model": "gpt", "role": "author"})
    assert decoupled.status_code == 201 and decoupled.json()["name"] == "spec-author"
    created = client.post("/api/plugins/shipfactory/seats", json={"name": "builder", "profile": "builder", "executor": "hermes", "model": "claude-sonnet-5", "role": "engineer", "provider_config": {"provider": "proxy", "base_url": "http://proxy", "model": "claude-sonnet-5"}})
    assert created.status_code == 201 and created.json()["name"] == "builder"
    updated = client.put("/api/plugins/shipfactory/seats/builder", json={"max_concurrent": 2})
    assert updated.status_code == 200 and updated.json()["max_concurrent"] == 2
    rows = {row["name"]: row for row in client.get("/api/plugins/shipfactory/seats").json()}
    assert rows["builder"]["profile_model"] == "claude-sonnet-5" and rows["builder"]["model_mismatch"] is False
    # The decoupled codex seat has no profile and reports no profile_model.
    assert rows["spec-author"]["profile"] is None and rows["spec-author"]["profile_model"] is None


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


def test_bundle_registers_under_the_manifest_name() -> None:
    """Drift guard for the tab registration name (Amendment G1).

    The Hermes host resolves the tab component via getPluginComponent(manifest.name)
    after the bundle's script.onload fires; a bundle that registers under any other
    string renders the NO_REGISTER error page. The Headframe -> ShipFactory rename
    orphaned the register() call once already; this pins bundle, manifest, and
    conformance harness to one name forever.
    """
    root = Path(__file__).resolve().parents[1] / "dashboard"
    manifest = json.loads((root / "manifest.json").read_text())
    bundle = (root / manifest["entry"]).read_text()
    calls = re.findall(r"__HERMES_PLUGINS__\.register\(\s*['\"]([^'\"]+)['\"]", bundle)
    assert calls, "bundle never calls window.__HERMES_PLUGINS__.register(name, Component)"
    assert calls == [manifest["name"]], (
        f"bundle registers {calls!r} but manifest.json name is {manifest['name']!r}; "
        "the host resolves the tab via getPluginComponent(manifest.name)"
    )
    harness = (root / "conformance-harness.js").read_text()
    assert "manifest.name" in harness or f'"{manifest["name"]}"' in harness, (
        "conformance harness must gate registration on the manifest name"
    )


def test_seat_dialog_exposes_every_supported_executor() -> None:
    from shipfactory.config import EXECUTORS

    bundle = (
        Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js"
    ).read_text()
    selector = re.search(
        r'id: "seat-executor".*?\}, \[(.*?)\]\.map\(function \(executor\)',
        bundle,
    )
    assert selector, "seat executor select is missing from the dashboard bundle"
    rendered = selector.group(1)
    for executor in EXECUTORS:
        assert f'"{executor}"' in rendered
    reasoning = re.search(
        r'id: "seat-reasoning".*?\}, \[(.*?)\]\.map\(function \(reasoning\)',
        bundle,
    )
    assert reasoning and '"max"' in reasoning.group(1)


def _seed_receipts_instance(home: Path) -> dict[str, int]:
    """Seed one instance with a rework attempt, run rows, and real run files."""
    store.init_db()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    recipe = {
        "id": "receipt-recipe",
        "version": 1,
        # Recipe order intentionally differs from alphabetical order.
        "steps": [{"id": "build"}, {"id": "audit"}],
    }
    normalized = json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    recipe_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions VALUES(?,?,?,?,?,?)",
            ("receipt-recipe", 1, recipe_hash, "active", normalized, now),
        )
        db.execute(
            """INSERT INTO recipe_instances
            (id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,
             activation_count,tokens_charged,parent_tasks_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("receipts-instance", "test", "collector-r", "receipt-recipe", 1,
             recipe_hash, "running", "{}", 2, 0, '["t_parent_collector"]', now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,verdict_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("receipts-instance", "audit", 1, "review_gate", "blocked", "t_a1",
             json.dumps({"outcome": "request_changes", "target_step": "build"}), now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("receipts-instance", "build", 1, "agent_task", "done", "t_b1", now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,"
            "rejected_by_step_id,rejected_by_activation,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("receipts-instance", "build", 2, "agent_task", "running", "t_b2", "audit", 1, now, now),
        )
    runs_dir = home / "shipfactory" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_b1 = runs_dir / "t_b1-stamp.log"
    log_b1.write_text("build attempt one log\n", encoding="utf-8")
    prompt_b1 = runs_dir / "t_b1-stamp.prompt"
    prompt_b1.write_text("build attempt one prompt\n", encoding="utf-8")
    log_b2 = runs_dir / "t_b2-stamp.log"
    log_b2.write_bytes(b"x" * (300 * 1024) + b"TAIL-MARKER")
    alien_log = runs_dir / "task-x-stamp.log"
    alien_log.write_text("not a recipe run\n", encoding="utf-8")
    run_b1 = store.record_run_start(
        "t_b1", "builder", "codex", "gpt", board="test", workspace_path="/tmp/w1",
        log_path=log_b1, prompt_path=prompt_b1, provider="openai",
        resolved_model="gpt-5", access_enforcement_level="full", recipe_activation=1,
    )
    store.record_run_end(run_b1, 0, 10, 5, 1.5, "done")
    run_b2 = store.record_run_start(
        "t_b2", "builder", "codex", "gpt", board="test", workspace_path="/tmp/w2",
        log_path=log_b2, provider="openai", resolved_model="gpt-5",
        recipe_activation=2,
    )
    # Legacy pre-migration row: no paths, no recipe_activation.
    run_a1 = store.record_run_start("t_a1", "verifier", "codex", "gpt")
    run_alien = store.record_run_start(
        "task-x", "builder", "codex", "gpt", log_path=alien_log,
    )
    return {"b1": run_b1, "b2": run_b2, "a1": run_a1, "alien": run_alien}


def test_receipts_join_runs_per_attempt_and_strip_paths(hermetic_hermes_home: Path):
    runs = _seed_receipts_instance(hermetic_hermes_home)
    client = _client()

    assert client.get("/api/plugins/shipfactory/instances/nope/receipts").status_code == 404
    receipts = client.get("/api/plugins/shipfactory/instances/receipts-instance/receipts")
    assert receipts.status_code == 200
    rows = receipts.json()
    assert [(row["step_id"], row["activation"], row["run_id"]) for row in rows] == [
        ("audit", 1, runs["a1"]), ("build", 1, runs["b1"]), ("build", 2, runs["b2"]),
    ]
    b1 = rows[1]
    assert b1["kanban_task_id"] == "t_b1"
    assert (b1["seat"], b1["executor"], b1["provider"], b1["resolved_model"]) == (
        "builder", "codex", "openai", "gpt-5",
    )
    assert (b1["tokens_in"], b1["tokens_out"], b1["tokens_total"]) == (10, 5, 15)
    assert b1["exit_code"] == 0 and b1["result"] == "done"
    assert b1["access_enforcement_level"] == "full"
    assert b1["has_log"] is True and b1["has_prompt"] is True
    assert rows[2]["has_log"] is True and rows[2]["has_prompt"] is False
    assert rows[0]["has_log"] is False and rows[0]["has_prompt"] is False
    for row in rows:
        assert "log_path" not in row and "prompt_path" not in row
        assert "workspace_path" not in row


def test_run_log_and_prompt_endpoints_serve_capped_db_resolved_paths(hermetic_hermes_home: Path):
    runs = _seed_receipts_instance(hermetic_hermes_home)
    client = _client()

    log = client.get(f"/api/plugins/shipfactory/runs/{runs['b1']}/log")
    assert log.status_code == 200
    assert log.json() == {
        "run_id": runs["b1"], "kind": "log",
        "content": "build attempt one log\n", "truncated": False,
    }
    prompt = client.get(f"/api/plugins/shipfactory/runs/{runs['b1']}/prompt")
    assert prompt.status_code == 200
    assert prompt.json()["kind"] == "prompt"
    assert prompt.json()["content"] == "build attempt one prompt\n"

    tail = client.get(f"/api/plugins/shipfactory/runs/{runs['b2']}/log").json()
    assert tail["truncated"] is True
    assert tail["content"].endswith("TAIL-MARKER")
    assert len(tail["content"]) == 256 * 1024

    # 404s: unknown run, legacy NULL path, and a run outside any recipe step.
    assert client.get("/api/plugins/shipfactory/runs/999999/log").status_code == 404
    assert client.get(f"/api/plugins/shipfactory/runs/{runs['a1']}/log").status_code == 404
    assert client.get(f"/api/plugins/shipfactory/runs/{runs['alien']}/log").status_code == 404
    # A recorded path whose file is gone is also a 404, not a 500.
    missing = hermetic_hermes_home / "shipfactory" / "runs" / "t_b1-stamp.log"
    missing.unlink()
    assert client.get(f"/api/plugins/shipfactory/runs/{runs['b1']}/log").status_code == 404


def test_instances_fold_latest_steps_in_recipe_order_with_overlay_columns(hermetic_hermes_home: Path):
    _seed_receipts_instance(hermetic_hermes_home)
    client = _client()

    items = client.get("/api/plugins/shipfactory/instances").json()
    item = next(row for row in items if row["id"] == "receipts-instance")
    assert item["parent_tasks_json"] == '["t_parent_collector"]'
    # Recipe order (build before audit), not the alphabetical fallback.
    assert [(step["step_id"], step["step_position"]) for step in item["latest_steps"]] == [
        ("build", 1), ("audit", 2),
    ]
    build_latest = item["latest_steps"][0]
    assert build_latest["activation"] == 2
    assert build_latest["rejected_by_step_id"] == "audit"
    assert build_latest["rejected_by_activation"] == 1
    assert build_latest["verdict_json"] is None

    detail = client.get("/api/plugins/shipfactory/instances/receipts-instance").json()
    assert detail["parent_tasks_json"] == '["t_parent_collector"]'
    audit = detail["activations"]["audit"][0]
    assert json.loads(audit["verdict_json"])["outcome"] == "request_changes"
    assert audit["rejected_by_step_id"] is None
    assert [step["step_id"] for step in detail["steps"]] == ["build", "build", "audit"]
