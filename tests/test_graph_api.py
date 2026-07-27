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
from shipfactory.recipes.loader import load_library


PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"


def _client() -> TestClient:
    spec = importlib.util.spec_from_file_location("graph_dashboard_api", PLUGIN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    return TestClient(app)


def _recipe_text(version: int, title: str) -> str:
    return f"""schema: shipfactory.recipe/v1
id: graph-fixture
version: {version}
status: active
description: {title}
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 1}}
steps:
  - id: first
    primitive: notify
    title: {title}
    needs: []
    optional: false
    params: {{target: test:graph, message: {title}}}
"""


def _configure(tmp_path: Path, monkeypatch, *, flags=None, calls=None) -> Path:
    library = tmp_path / "recipes"
    library.mkdir()
    (library / "graph-fixture@1.yaml").write_text(
        _recipe_text(1, "Version one"), encoding="utf-8"
    )
    (library / "graph-fixture@2.yaml").write_text(
        _recipe_text(2, "Version two"), encoding="utf-8"
    )
    flag_overrides = flags if flags is not None else {}
    recipes = {
        "enabled": True,
        "library_path": str(library),
        "execution_profiles": {},
    }
    import shipfactory.config

    def load_seats():
        if calls is not None:
            calls.append(True)
        settings = {"enabled": True, "graph_enabled": True}
        settings.update(flag_overrides)
        recipes["projects_visual_recipes"] = settings
        return SimpleNamespace(seats={}, recipes=recipes)

    monkeypatch.setattr(shipfactory.config, "load_seats", load_seats)
    return library


def _seed_instance(library: Path, instance_id: str = "instance-1") -> str:
    recipe = load_library(library, persist=False).get("graph-fixture@1")
    normalized = json.dumps(
        recipe.document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    store.init_db()
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions(id,version,hash,status,normalized_yaml,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("graph-fixture", 1, digest, "active", normalized, now),
        )
        db.execute(
            "INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,"
            "recipe_hash,status,parameters_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (instance_id, "secret-board", "collector", "graph-fixture", 1, digest,
             "running", "{}", now, now),
        )
    return digest


def test_recipe_graph_resolves_exact_version_without_publishing(tmp_path, monkeypatch):
    library = _configure(tmp_path, monkeypatch)
    store.init_db()
    client = _client()

    response = client.get(
        "/api/plugins/shipfactory/recipes/graph-fixture/versions/1/graph"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["version"] == 1
    assert body["source"]["pinned"] is True
    assert body["nodes"][0]["title"] == "Version one"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recipe_versions").fetchone()[0] == 0
    assert library.exists()


def test_graph_config_is_fresh_and_projects_all_layout_values(tmp_path, monkeypatch):
    calls = []
    flags = {
        "graph_direction": "RL",
        "graph_rank_gap": 101,
        "graph_lane_gap": 43,
        "graph_node_width": 222,
        "graph_node_height": 77,
        "graph_diamond_size": 35,
    }
    _configure(tmp_path, monkeypatch, flags=flags, calls=calls)
    client = _client()
    first = client.get("/api/plugins/shipfactory/recipes/graph-fixture/versions/2/graph")
    flags["graph_direction"] = "LR"
    second = client.get("/api/plugins/shipfactory/recipes/graph-fixture/versions/2/graph")

    assert first.status_code == second.status_code == 200
    assert first.json()["layout"] == {
        "direction": "RL", "rank_gap": 101, "lane_gap": 43,
        "node_width": 222, "node_height": 77, "diamond_size": 35,
    }
    assert second.json()["layout"]["direction"] == "LR"
    assert len(calls) == 2


@pytest.mark.parametrize("disabled_flag", ["enabled", "graph_enabled"])
def test_graph_requires_each_stage_zero_flag_in_an_isolated_seed(
    tmp_path, monkeypatch, disabled_flag,
):
    flags = {disabled_flag: False}
    library = _configure(tmp_path, monkeypatch, flags=flags)
    instance_id = f"instance-{disabled_flag}"
    _seed_instance(library, instance_id=instance_id)
    client = _client()
    disabled = client.get(
        f"/api/plugins/shipfactory/instances/{instance_id}/graph"
    )
    assert disabled.status_code == 403
    assert disabled.json()["error"] == "feature_disabled"

def test_graph_reports_unknown_versions_and_invalid_version_format(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, flags={"enabled": True, "graph_enabled": True})
    client = _client()
    unknown_version = client.get(
        "/api/plugins/shipfactory/recipes/graph-fixture/versions/9/graph"
    )
    invalid_version = client.get(
        "/api/plugins/shipfactory/recipes/graph-fixture/versions/nope/graph"
    )
    assert unknown_version.status_code == 404
    assert invalid_version.status_code == 400


def test_instance_graph_uses_only_authenticated_persisted_identity(tmp_path, monkeypatch):
    library = _configure(tmp_path, monkeypatch)
    digest = _seed_instance(library)
    client = _client()

    response = client.get("/api/plugins/shipfactory/instances/instance-1/graph")

    assert response.status_code == 200
    body = response.json()
    assert body["graph"]["source"]["recipe_hash"] == digest
    assert body["graph"]["source"]["pinned"] is True
    assert "secret-board" not in json.dumps(body)
    assert client.get("/api/plugins/shipfactory/instances/missing/graph").status_code == 404


def test_instance_graph_rejects_tampered_bytes_hash_and_instance_identity(
    tmp_path, monkeypatch
):
    library = _configure(tmp_path, monkeypatch)
    digest = _seed_instance(library)
    client = _client()

    with store._connect() as db:
        db.execute(
            "UPDATE recipe_versions SET normalized_yaml=? WHERE id=? AND version=1",
            ('{"description":"tampered"}', "graph-fixture"),
        )
    assert client.get("/api/plugins/shipfactory/instances/instance-1/graph").status_code == 409

    with store._connect() as db:
        recipe = load_library(library, persist=False).get("graph-fixture@1")
        normalized = json.dumps(
            recipe.document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        db.execute(
            "UPDATE recipe_versions SET normalized_yaml=?,hash=? WHERE id=? AND version=1",
            (normalized, "0" * 64, "graph-fixture"),
        )
    assert client.get("/api/plugins/shipfactory/instances/instance-1/graph").status_code == 409

    with store._connect() as db:
        db.execute(
            "UPDATE recipe_versions SET hash=? WHERE id=? AND version=1",
            (digest, "graph-fixture"),
        )
        db.execute(
            "UPDATE recipe_instances SET recipe_hash=? WHERE id=?",
            ("1" * 64, "instance-1"),
        )
    assert client.get("/api/plugins/shipfactory/instances/instance-1/graph").status_code == 409
