"""HTTP coverage for the frozen W2-B Projects API contract."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import importlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shipfactory import store


PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _client() -> TestClient:
    spec = importlib.util.spec_from_file_location("projects_dashboard_api", PLUGIN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    return TestClient(app)


def _recipe_text(recipe_id: str = "route-a") -> str:
    return f"""schema: shipfactory.recipe/v1
id: {recipe_id}
version: 1
status: active
description: W2-B fixture
intent_tags: [test]
supersedes: null
parameters:
  request: {{type: string, required: true, default: null}}
  urgent: {{type: boolean, required: false, default: false}}
budgets: {{max_activations: 4, max_step_activations: 2, max_tokens: 50000}}
steps:
  - id: first
    primitive: notify
    title: Primary notification
    needs: []
    optional: false
    params: {{target: test:dashboard, message: "Run ${{request}}"}}
  - id: extra
    primitive: notify
    title: Optional notification
    needs: [first]
    optional: true
    params: {{target: test:dashboard, message: Optional}}
"""


def _configure(tmp_path: Path, monkeypatch, *, projects_visual_recipes=None, calls=None) -> None:
    library = tmp_path / "recipes"
    library.mkdir()
    (library / "route-a@1.yaml").write_text(_recipe_text(), encoding="utf-8")
    recipes = {
        "enabled": True,
        "library_path": str(library),
        "bare_task_recipe": "route-a@1",
        "execution_profiles": {},
    }
    if projects_visual_recipes is not None:
        recipes["projects_visual_recipes"] = projects_visual_recipes
    import shipfactory.config

    def load_seats():
        if calls is not None:
            calls.append(True)
        return SimpleNamespace(seats={}, recipes=recipes)

    monkeypatch.setattr(
        shipfactory.config,
        "load_seats",
        load_seats,
    )


def _projects(monkeypatch, projects: list[SimpleNamespace]) -> None:
    from hermes_cli import projects_db

    @contextmanager
    def connect_closing():
        yield object()

    monkeypatch.setattr(projects_db, "connect_closing", connect_closing)
    monkeypatch.setattr(
        projects_db,
        "list_projects",
        lambda _conn, include_archived=False: list(projects),
    )
    monkeypatch.setattr(
        projects_db,
        "get_project",
        lambda _conn, value: next(
            (p for p in projects if p.id == value or p.slug == value), None
        ),
    )


def _project(project_id: str, slug: str, board_slug: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=project_id, slug=slug, name=slug.title(), board_slug=board_slug)


def _attach(client, project_id: str = "p-1", *, allowed=None, default="route-a@1"):
    if allowed is None:
        allowed = ["route-a@1"]
    return client.put(
        f"/api/plugins/shipfactory/projects/{project_id}/recipe-policy",
        json={"allowed_recipe_keys": allowed, "default_recipe_key": default},
    )


def test_projects_exposes_fresh_runtime_config_and_unclassified_rollup(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    project = _project("p-1", "factory", "board-a")
    _projects(monkeypatch, [project])
    store.init_db()
    with store._connect() as db:
        now = store._now()
        db.execute(
            "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
            "recipe_hash,status,parameters_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "orphan-board", "collector", "route-a", 1, "hash", "running", "{}", now, now),
        )

    response = _client().get("/api/plugins/shipfactory/projects")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_config"]["recent_flight_limit"] == 20
    assert body["projects"] == [{
        "id": "p-1", "slug": "factory", "name": "Factory", "binding": "bound",
        "recipes": {"allowed": [], "default": None},
        "rollup": {"active": 0, "waiting": 0, "recent": []},
    }]
    assert body["unclassified"]["rollup"]["active"] == 1
    assert "board" not in body and "board_slug" not in body
    assert "board" not in body["projects"][0]


def test_policy_write_filters_recipes_and_survives_reopen(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    project = _project("p-1", "factory", "board-a")
    _projects(monkeypatch, [project])
    client = _client()

    written = client.put(
        "/api/plugins/shipfactory/projects/p-1/recipe-policy",
        json={"allowed_recipe_keys": ["route-a@1"], "default_recipe_key": "route-a@1"},
    )
    assert written.status_code == 200
    assert written.json()["default_recipe_key"] == "route-a@1"

    reopened = _client().get("/api/plugins/shipfactory/projects/p-1/recipes")
    assert reopened.status_code == 200
    assert reopened.json()["default_recipe"] == "route-a@1"
    assert reopened.json()["recipes"][0]["key"] == "route-a@1"
    assert reopened.json()["recipes"][0]["default"] is True


def test_launch_resolves_hidden_board_and_replays_project_scoped_identity(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    project = _project("p-1", "factory", "board-a")
    _projects(monkeypatch, [project])
    client = _client()
    assert client.put(
        "/api/plugins/shipfactory/projects/p-1/recipe-policy",
        json={"allowed_recipe_keys": ["route-a@1"], "default_recipe_key": "route-a@1"},
    ).status_code == 200

    captured: dict[str, object] = {}

    def fake_instantiate(conn, **kwargs):
        captured.update(kwargs)
        instance_id = kwargs["instance_id"]
        recipe = kwargs["recipe"]
        now = store._now()
        with store._connect() as db:
            db.execute(
                "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
                "recipe_hash,status,parameters_json,created_at,updated_at,project_id,linear_issue_id,"
                "launch_idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (instance_id, kwargs["board"], "collector", recipe.document["id"], recipe.document["version"],
                     recipe.hash, "running", '{"request":"ship","urgent":false}', now, now, kwargs["project_id"],
                     kwargs["linear_issue_id"], kwargs["launch_idempotency_key"]),
            )
            db.execute(
                "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (instance_id, "first", 1, "notify", "pending", now, now),
            )
            db.execute(
                "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (instance_id, "extra", 1, "notify", "skipped", now, now),
            )
        return {"instance_id": instance_id}

    instantiate_module = importlib.import_module("shipfactory.recipes.instantiate")
    monkeypatch.setattr(instantiate_module, "instantiate", fake_instantiate)
    request = {
        "recipe": "route-a", "version": 1, "parameters": {"request": "ship"},
        "skip_steps": ["extra"], "linear_issue_id": "LIN-1", "idempotency_key": "key-1",
    }
    created = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=request)
    replay = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=request)

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == created.json()
    assert captured["board"] == "board-a"
    assert captured["project_id"] == "p-1"
    assert "board" not in created.json()
    assert created.json()["linear_backlink"]["status"] == "unavailable"
    assert created.json()["skip_steps"] == ["extra"]


def test_unbound_and_ambiguous_projects_cannot_launch(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    projects = [
        _project("p-none", "none", None),
        _project("p-a", "a", "same-board"),
        _project("p-b", "b", "same-board"),
    ]
    _projects(monkeypatch, projects)
    client = _client()
    payload = {
        "recipe": "route-a", "version": 1, "parameters": {"request": "ship"},
        "idempotency_key": "key-1",
    }
    assert client.post("/api/plugins/shipfactory/projects/p-none/flights", json=payload).status_code == 409
    assert client.post("/api/plugins/shipfactory/projects/p-a/flights", json=payload).status_code == 409


def test_projects_reload_config_once_per_request_and_enforces_enabled(tmp_path, monkeypatch):
    calls = []
    flags = {"enabled": True}
    _configure(tmp_path, monkeypatch, projects_visual_recipes=flags, calls=calls)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    client = _client()

    assert client.get("/api/plugins/shipfactory/projects").status_code == 200
    flags["enabled"] = False
    denied = client.get("/api/plugins/shipfactory/projects")

    assert denied.status_code == 403
    assert denied.json() == {
        "error": "feature_disabled",
        "message": "Projects feature enabled is disabled",
    }
    assert len(calls) == 2


def test_projects_policy_and_launch_flags_are_server_enforced(tmp_path, monkeypatch):
    flags = {"policy_editing_enabled": False, "launch_enabled": False}
    _configure(tmp_path, monkeypatch, projects_visual_recipes=flags)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    client = _client()

    policy = _attach(client)
    launch = client.post(
        "/api/plugins/shipfactory/projects/p-1/flights",
        json={"recipe": "route-a", "version": 1, "idempotency_key": "key-1"},
    )

    assert policy.status_code == 403
    assert launch.status_code == 403
    store.init_db()
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM project_recipe_policies").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == 0


def test_projects_config_type_errors_are_structured_on_every_route(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    import shipfactory.config

    monkeypatch.setattr(shipfactory.config, "load_seats", lambda: (_ for _ in ()).throw(TypeError("bad config")))
    client = _client()
    requests = [
        ("get", "/api/plugins/shipfactory/projects", None),
        ("get", "/api/plugins/shipfactory/projects/p-1/recipes", None),
        ("put", "/api/plugins/shipfactory/projects/p-1/recipe-policy", {"allowed_recipe_keys": [], "default_recipe_key": None}),
        ("post", "/api/plugins/shipfactory/projects/p-1/flights", {"recipe": "route-a", "version": 1, "idempotency_key": "key-1"}),
    ]
    for method, path, payload in requests:
        response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert response.status_code == 400
        assert response.json()["error"] in {"projects_unavailable", "launch_failed"}
        assert set(response.json()) >= {"error", "message"}


def test_policy_rejects_unknown_detached_and_defaultless_recipes_and_persists_valid_policy(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    client = _client()

    unknown = _attach(client, allowed=["missing@1"], default="missing@1")
    detached = _attach(client, allowed=["route-a@1"], default="missing@1")
    defaultless = _attach(client, allowed=["route-a@1"], default=None)
    valid = _attach(client)

    assert unknown.status_code == detached.status_code == defaultless.status_code == 400
    assert valid.status_code == 200
    assert valid.json()["allowed_recipe_keys"] == ["route-a@1"]
    with store._connect() as db:
        assert store.load_project_recipe_policy(db, "p-1")["default_recipe_key"] == "route-a@1"
    reopened = _client().get("/api/plugins/shipfactory/projects/p-1/recipes")
    assert reopened.status_code == 200
    assert reopened.json()["default_recipe"] == "route-a@1"


def test_projects_reject_board_request_fields_and_never_return_board_fields(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    project = _project("p-1", "factory", "board-a")
    _projects(monkeypatch, [project])
    client = _client()
    assert _attach(client).status_code == 200

    base = {"recipe": "route-a", "version": 1, "idempotency_key": "key-1"}
    for field in ("board", "board_slug"):
        payload = {**base, field: "attacker-selected"}
        assert client.post("/api/plugins/shipfactory/projects/p-1/flights", json=payload).status_code == 422
    assert client.put(
        "/api/plugins/shipfactory/projects/p-1/recipe-policy",
        json={"allowed_recipe_keys": [], "default_recipe_key": None, "board": "x"},
    ).status_code == 422

    response = client.get("/api/plugins/shipfactory/projects")
    recipes = client.get("/api/plugins/shipfactory/projects/p-1/recipes")
    assert response.status_code == recipes.status_code == 200
    assert "board" not in response.json() and "board_slug" not in response.json()
    assert "board" not in recipes.json() and "board_slug" not in recipes.json()


def test_same_key_changed_facts_conflict_and_same_issue_exact_facts_replay(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    projects = [_project("p-1", "one", "board-a"), _project("p-2", "two", "board-b")]
    _projects(monkeypatch, projects)
    client = _client()
    assert _attach(client, "p-1").status_code == 200
    assert _attach(client, "p-2").status_code == 200

    captured = []

    def fake_instantiate(conn, **kwargs):
        captured.append(kwargs)
        instance_id = kwargs["instance_id"]
        now = store._now()
        with store._connect() as db:
            db.execute(
                "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
                "recipe_hash,status,parameters_json,created_at,updated_at,project_id,linear_issue_id,"
                "launch_idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (instance_id, kwargs["board"], "collector", kwargs["recipe"].document["id"], 1,
                 kwargs["recipe"].hash, "running", '{"request":"ship","urgent":false}', now, now,
                 kwargs["project_id"], kwargs["linear_issue_id"], kwargs["launch_idempotency_key"]),
            )
            for step_id, state in (("first", "pending"), ("extra", "skipped")):
                db.execute(
                    "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (instance_id, step_id, 1, "notify", state, now, now),
                )
        return {"instance_id": instance_id}

    instantiate_module = importlib.import_module("shipfactory.recipes.instantiate")
    monkeypatch.setattr(instantiate_module, "instantiate", fake_instantiate)
    payload = {
        "recipe": "route-a", "version": 1, "parameters": {"request": "ship"},
        "skip_steps": ["extra"], "linear_issue_id": "LIN-1", "idempotency_key": "key-1",
    }
    created = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=payload)
    replay = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=payload)
    changed = client.post(
        "/api/plugins/shipfactory/projects/p-1/flights",
        json={**payload, "parameters": {"request": "changed"}},
    )
    issue_changed_key = client.post(
        "/api/plugins/shipfactory/projects/p-1/flights",
        json={**payload, "idempotency_key": "key-2"},
    )
    issue_changed_project = client.post(
        "/api/plugins/shipfactory/projects/p-2/flights",
        json={**payload, "idempotency_key": "key-2"},
    )

    assert created.status_code == 201
    assert replay.status_code == 200 and replay.json() == created.json()
    assert changed.status_code == issue_changed_key.status_code == issue_changed_project.status_code == 409
    assert len(captured) == 1


def test_replay_keeps_captured_identity_after_policy_and_binding_change(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    project = _project("p-1", "factory", "board-a")
    _projects(monkeypatch, [project])
    client = _client()
    assert _attach(client).status_code == 200

    instantiate_module = importlib.import_module("shipfactory.recipes.instantiate")
    captured = {}

    def fake_instantiate(conn, **kwargs):
        captured.update(kwargs)
        now = store._now()
        with store._connect() as db:
            db.execute(
                "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
                "recipe_hash,status,parameters_json,created_at,updated_at,project_id,linear_issue_id,"
                "launch_idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kwargs["instance_id"], kwargs["board"], "collector", "route-a", 1,
                 kwargs["recipe"].hash, "running", '{"request":"ship","urgent":false}', now, now,
                 kwargs["project_id"], kwargs["linear_issue_id"], kwargs["launch_idempotency_key"]),
            )
            for step_id, state in (("first", "pending"), ("extra", "skipped")):
                db.execute(
                    "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (kwargs["instance_id"], step_id, 1, "notify", state, now, now),
                )
        return {"instance_id": kwargs["instance_id"]}

    monkeypatch.setattr(instantiate_module, "instantiate", fake_instantiate)
    payload = {"recipe": "route-a", "version": 1, "parameters": {"request": "ship"}, "skip_steps": ["extra"], "linear_issue_id": "LIN-1", "idempotency_key": "key-1"}
    created = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=payload)
    assert created.status_code == 201

    assert _attach(client, allowed=[] , default=None).status_code == 200
    project.board_slug = "board-b"
    replay = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=payload)

    assert replay.status_code == 200
    assert replay.json() == created.json()
    assert captured["board"] == "board-a"
    assert "board" not in replay.json() and "board_slug" not in replay.json()


def test_whitespace_identity_is_rejected_without_effect(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    client = _client()
    assert _attach(client).status_code == 200

    for payload in (
        {"recipe": "route-a", "version": 1, "idempotency_key": "   "},
        {"recipe": "route-a", "version": 1, "idempotency_key": "key-1", "linear_issue_id": "\t"},
    ):
        response = client.post("/api/plugins/shipfactory/projects/p-1/flights", json=payload)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == 0


def test_integrity_retry_probes_key_and_global_issue_and_replays_exact_facts(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    client = _client()
    assert _attach(client).status_code == 200
    instantiate_module = importlib.import_module("shipfactory.recipes.instantiate")
    probes = {"key": 0, "issue": 0}
    real_key_probe = store.project_flight_by_idempotency_key
    real_issue_probe = store.project_flight_by_linear_issue_id

    def key_probe(db, project_id, key):
        probes["key"] += 1
        return real_key_probe(db, project_id, key)

    def issue_probe(db, issue_id):
        probes["issue"] += 1
        return real_issue_probe(db, issue_id)

    monkeypatch.setattr(store, "project_flight_by_idempotency_key", key_probe)
    monkeypatch.setattr(store, "project_flight_by_linear_issue_id", issue_probe)

    def racing_instantiate(conn, **kwargs):
        now = store._now()
        with store._connect() as db:
            db.execute(
                "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
                "recipe_hash,status,parameters_json,created_at,updated_at,project_id,linear_issue_id,"
                "launch_idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kwargs["instance_id"], kwargs["board"], "collector", "route-a", 1,
                 kwargs["recipe"].hash, "running", '{"request":"ship","urgent":false}', now, now,
                 kwargs["project_id"], kwargs["linear_issue_id"], kwargs["launch_idempotency_key"]),
            )
            for step_id, state in (("first", "pending"), ("extra", "skipped")):
                db.execute(
                    "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (kwargs["instance_id"], step_id, 1, "notify", state, now, now),
                )
        raise sqlite3.IntegrityError("raced identity")

    monkeypatch.setattr(instantiate_module, "instantiate", racing_instantiate)
    response = client.post(
        "/api/plugins/shipfactory/projects/p-1/flights",
        json={"recipe": "route-a", "version": 1, "parameters": {"request": "ship"}, "skip_steps": ["extra"], "linear_issue_id": "LIN-1", "idempotency_key": "key-1"},
    )

    assert response.status_code == 200
    assert probes["key"] >= 2 and probes["issue"] >= 2


def test_launch_uses_real_instantiate_seam_on_isolated_board(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _projects(monkeypatch, [_project("p-1", "factory", "board-a")])
    client = _client()
    assert _attach(client).status_code == 200

    response = client.post(
        "/api/plugins/shipfactory/projects/p-1/flights",
        json={
            "recipe": "route-a", "version": 1,
            "parameters": {"request": "real seam"},
            "linear_issue_id": "LIN-REAL", "idempotency_key": "real-key",
        },
    )

    assert response.status_code == 201
    from hermes_cli import kanban_db

    with store._connect() as db:
        row = dict(db.execute(
            "SELECT * FROM recipe_instances WHERE id=?",
            (response.json()["instance_id"],),
        ).fetchone())
    assert row["board"] == "board-a"
    assert row["project_id"] == "p-1"
    assert row["linear_issue_id"] == "LIN-REAL"
    assert row["launch_idempotency_key"] == "real-key"
    conn = kanban_db.connect(board="board-a")
    try:
        task = kanban_db.get_task(conn, row["collector_task_id"])
        assert task.status == "blocked"
    finally:
        conn.close()
