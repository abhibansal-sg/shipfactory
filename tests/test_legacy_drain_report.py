"""Read-only legacy drain report contracts (Milestone E Task 21)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shipfactory import legacy_drain, store


def test_backup_root_follows_the_live_store_location(tmp_path, monkeypatch):
    db_path = tmp_path / "shipfactory" / "shipfactory.db"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    assert legacy_drain._backup_root() == db_path.parent / "backups"


def _seed_legacy_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    store.init_db()
    with store._connect() as db:
        db.execute(
            """INSERT INTO recipe_instances(
                 id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,
                 status,parameters_json,activation_count,tokens_charged,created_at,
                 updated_at,project_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-active", "legacy-board", "collector", "dev-pipeline", 14,
                "recipe-hash", "running", "{}", 1, 0,
                "2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00",
                "project-legacy",
            ),
        )
        db.execute(
            """INSERT INTO project_recipe_policies(
                 project_id,allowed_recipe_keys_json,default_recipe_key,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (
                "project-legacy", '["dev-pipeline@14"]', "dev-pipeline@14",
                "2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00",
            ),
        )
    backups = tmp_path / "backups"
    archive = backups / "archive-20260702"
    archive.mkdir(parents=True)
    (archive / "shipfactory.db").write_bytes(b"backup")
    (archive / "README.txt").write_text("read-only archive", encoding="utf-8")
    monkeypatch.setattr(legacy_drain, "_backup_root", lambda: backups)


def test_legacy_drain_report_is_read_only_complete_and_fail_closed(tmp_path, monkeypatch):
    _seed_legacy_state(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "init_db", lambda: (_ for _ in ()).throw(
        AssertionError("read-only drain report must not initialize or migrate"),
    ))

    report = legacy_drain.build_report()

    assert report["active_legacy_runs"] == [{
        "id": "legacy-active", "project_id": "project-legacy",
        "board": "legacy-board", "recipe": "dev-pipeline@14",
        "status": "running", "updated_at": "2026-07-02T00:00:00+00:00",
    }]
    assert report["legacy_project_defaults"] == [{
        "project_id": "project-legacy", "recipe": "dev-pipeline@14",
        "updated_at": "2026-07-02T00:00:00+00:00",
    }]
    assert report["last_legacy_activity_at"] == "2026-07-02T00:00:00+00:00"
    assert report["legacy_api_clients"] == {
        "observable": False, "observed_count": None,
        "reason": "legacy API access telemetry is not available",
    }
    assert report["backup_archive"]["ready"] is True
    assert report["deletion_eligible"] is False
    assert report["conditions"]["no_active_legacy_runs"]["passed"] is False
    assert report["conditions"]["no_legacy_project_defaults"]["passed"] is False
    assert report["conditions"]["no_observed_legacy_api_clients"]["passed"] is False
    assert report["conditions"]["graph_capability_journeys_complete"]["passed"] is False
    assert report["conditions"]["dashboard_and_cli_legacy_calls_retired"]["passed"] is False


def test_legacy_drain_report_api_and_cli_are_read_only_projections(monkeypatch):
    fixture = {
        "active_legacy_runs": [], "legacy_project_defaults": [],
        "legacy_api_clients": {"observable": False},
        "last_legacy_activity_at": None, "backup_archive": {"ready": False},
        "conditions": {}, "deletion_eligible": False,
    }
    monkeypatch.setattr(legacy_drain, "build_report", lambda: fixture)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "shipfactory_drain_report_test_api", root / "dashboard" / "plugin_api.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/shipfactory")
    client = TestClient(app)
    monkeypatch.setattr(module, "build_legacy_drain_report", lambda: fixture)

    response = client.get("/api/plugins/shipfactory/v1/legacy-drain-report")
    assert response.status_code == 200
    assert response.json() == fixture

    from shipfactory import cli
    assert cli.main(["legacy-drain-report"]) == fixture
