import hashlib
import json
import sqlite3

import pytest

from shipfactory import store


def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    return tmp_path / "shipfactory" / "shipfactory.db"


def _insert_instance(db, instance_id, *, project_id=None, linear_issue_id=None,
                     launch_idempotency_key=None, status="running", updated_at=None,
                     recipe_id="dev-pipeline", recipe_version=14):
    now = updated_at or store._now()
    db.execute(
        "INSERT INTO recipe_instances("
        "id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,"
        "parameters_json,created_at,updated_at,project_id,linear_issue_id,"
        "launch_idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (instance_id, "board-a", f"collector-{instance_id}", recipe_id, recipe_version,
         f"hash-{instance_id}", status, "{}", now, now, project_id, linear_issue_id,
         launch_idempotency_key),
    )


def test_migration_16_is_idempotent_checksum_disciplined_and_has_exact_schema(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        first = db.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations WHERE version=16"
        ).fetchone()
        assert first is not None
        expected = hashlib.sha256(store._MIGRATIONS[-1][2].encode("utf-8")).hexdigest()
        assert first[0] == 16
        assert first[1] == store._MIGRATIONS[-1][1]
        assert first[2] == expected
        assert {row[1] for row in db.execute("PRAGMA table_info(project_recipe_policies)")} == {
            "project_id", "allowed_recipe_keys_json", "default_recipe_key", "created_at", "updated_at",
        }
        columns = {row[1] for row in db.execute("PRAGMA table_info(recipe_instances)")}
        assert {"project_id", "linear_issue_id", "launch_idempotency_key"} <= columns
        indexes = {
            row[1]: row[4]
            for row in db.execute("PRAGMA index_list(recipe_instances)")
        }
        policy_indexes = {
            row[1] for row in db.execute("PRAGMA index_list(project_recipe_policies)")
        }
        assert {
            "idx_project_recipe_policies_updated",
            "idx_recipe_instances_project_updated",
            "uq_recipe_instances_linear_issue",
            "uq_recipe_instances_launch_key",
        } <= set(indexes) | policy_indexes
        assert indexes["uq_recipe_instances_linear_issue"] == 1
        assert indexes["uq_recipe_instances_launch_key"] == 1
        sql = " ".join(
            row[4].lower() for row in db.execute(
                "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master "
                "WHERE name LIKE '%project%' OR name LIKE '%board%'"
            ) if row[4]
        )
        assert "project_board" not in sql
        assert "board_mapping" not in sql

    store.init_db()
    with sqlite3.connect(path) as db:
        again = db.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations WHERE version=16"
        ).fetchone()
    assert again[:3] == first[:3]
    assert again[3] == first[3]


def test_migration_16_checksum_drift_fails_closed(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=16")
    with pytest.raises(RuntimeError, match="schema migration 16 checksum mismatch"):
        store.init_db()


def test_old_instance_rows_remain_null_and_policy_edits_do_not_change_identity(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    with store._connect() as db:
        _insert_instance(db, "legacy")
        _insert_instance(
            db, "bound", project_id="project-1", linear_issue_id="LIN-1",
            launch_idempotency_key="launch-1",
        )
        before = dict(db.execute("SELECT * FROM recipe_instances WHERE id='bound'").fetchone())
        policy = store.save_project_recipe_policy(
            db, "project-1", ["z-recipe@2", "not-in-library@999", "a-recipe@1"], None,
        )
        assert policy["allowed_recipe_keys"] == [
            "a-recipe@1", "not-in-library@999", "z-recipe@2",
        ]
        after = dict(db.execute("SELECT * FROM recipe_instances WHERE id='bound'").fetchone())
        assert {key: after[key] for key in (
            "project_id", "linear_issue_id", "launch_idempotency_key",
        )} == {key: before[key] for key in (
            "project_id", "linear_issue_id", "launch_idempotency_key",
        )}
        legacy = db.execute(
            "SELECT project_id,linear_issue_id,launch_idempotency_key "
            "FROM recipe_instances WHERE id='legacy'"
        ).fetchone()
        assert tuple(legacy) == (None, None, None)


def test_policy_is_canonical_and_survives_reconnect(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    with store._connect() as db:
        saved = store.save_project_recipe_policy(
            db, "project-1", ["recipe-b@1", "recipe-a@2"], "recipe-a@2",
        )
        assert saved["project_id"] == "project-1"
        assert saved["default_recipe_key"] == "recipe-a@2"
        assert json.loads(
            db.execute(
                "SELECT allowed_recipe_keys_json FROM project_recipe_policies "
                "WHERE project_id='project-1'"
            ).fetchone()[0]
        ) == ["recipe-a@2", "recipe-b@1"]

    with store._connect() as db:
        assert store.load_project_recipe_policy(db, "project-1") == saved
    assert path.exists()


def test_policy_shape_type_and_default_validation_is_fail_closed_and_atomic(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with store._connect() as db:
        original = store.save_project_recipe_policy(db, "project-1", ["recipe-a@1"], "recipe-a@1")
        invalid = [
            (("recipe-a@1",), None),
            (("recipe-a@1", 7), None),
            (("recipe-a@1", "recipe-a@1"), None),
            (("recipe-a@1",), 7),
            (("recipe-a@1",), "missing@1"),
        ]
        for allowed, default in invalid:
            with pytest.raises((TypeError, ValueError)):
                store.save_project_recipe_policy(db, "project-1", allowed, default)
            assert store.load_project_recipe_policy(db, "project-1") == original


def test_policy_replace_is_atomic_and_does_not_validate_recipe_library(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with store._connect() as db:
        first = store.save_project_recipe_policy(db, "project-1", ["old@1"], None)
        created_at = db.execute(
            "SELECT created_at FROM project_recipe_policies WHERE project_id='project-1'"
        ).fetchone()[0]
        second = store.save_project_recipe_policy(
            db, "project-1", ["future-not-installed@42", "old@1"], "future-not-installed@42",
        )
        assert second["allowed_recipe_keys"] == ["future-not-installed@42", "old@1"]
        assert db.execute(
            "SELECT created_at FROM project_recipe_policies WHERE project_id='project-1'"
        ).fetchone()[0] == created_at
        assert second["updated_at"] >= first["updated_at"]


def test_identity_indexes_are_project_scoped_for_launch_and_global_for_linear(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with store._connect() as db:
        _insert_instance(db, "p1", project_id="project-1", launch_idempotency_key="same")
        _insert_instance(db, "p2", project_id="project-2", launch_idempotency_key="same")
        _insert_instance(db, "null-project-a", launch_idempotency_key="same")
        _insert_instance(db, "null-project-b", launch_idempotency_key="same")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_instance(db, "p1-duplicate", project_id="project-1", launch_idempotency_key="same")

        _insert_instance(db, "issue-1", project_id="project-1", linear_issue_id="LIN-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_instance(db, "issue-duplicate", project_id="project-2", linear_issue_id="LIN-1")


def test_project_flight_lookup_replay_seams_and_rollup(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with store._connect() as db:
        _insert_instance(
            db, "running", project_id="project-1", linear_issue_id="LIN-1",
            launch_idempotency_key="launch-1", status="running", updated_at="2026-01-03T00:00:00+00:00",
        )
        _insert_instance(
            db, "waiting", project_id="project-1", linear_issue_id="LIN-2",
            launch_idempotency_key="launch-2", status="waiting_gate", updated_at="2026-01-02T00:00:00+00:00",
        )
        _insert_instance(
            db, "done", project_id="project-1", status="done", updated_at="2026-01-01T00:00:00+00:00",
        )
        assert store.project_flight_by_idempotency_key(db, "project-1", "launch-1")["id"] == "running"
        assert store.project_flight_by_linear_issue_id(db, "LIN-2")["id"] == "waiting"
        assert store.project_flight(db, "running")["project_id"] == "project-1"
        assert store.project_flight(db, "missing") is None
        assert store.project_rollup(db, "project-1", recent_limit=3) == {
            "active": 1,
            "waiting": 1,
            "recent": [
                {
                    "instance_id": "running", "recipe": "dev-pipeline@14", "status": "running",
                    "updated_at": "2026-01-03T00:00:00+00:00", "linear_issue_id": "LIN-1",
                },
                {
                    "instance_id": "waiting", "recipe": "dev-pipeline@14", "status": "waiting_gate",
                    "updated_at": "2026-01-02T00:00:00+00:00", "linear_issue_id": "LIN-2",
                },
                {
                    "instance_id": "done", "recipe": "dev-pipeline@14", "status": "done",
                    "updated_at": "2026-01-01T00:00:00+00:00", "linear_issue_id": None,
                },
            ],
        }


@pytest.mark.parametrize("recent_limit", [0, -1, True, False, None, 1.5, "3"])
def test_project_rollup_rejects_invalid_recent_limits(tmp_path, monkeypatch, recent_limit):
    _db(tmp_path, monkeypatch)
    with store._connect() as db:
        with pytest.raises((TypeError, ValueError), match="recent_limit"):
            store.project_rollup(db, "project-1", recent_limit=recent_limit)


def test_migration_16_partial_artifacts_fail_closed(tmp_path, monkeypatch):
    path = _db(tmp_path, monkeypatch)
    with store._connect() as db:
        db.execute("DELETE FROM schema_migrations WHERE version=16")
        db.execute("DROP INDEX uq_recipe_instances_launch_key")
    with pytest.raises(RuntimeError, match="schema migration 16 is partially applied"):
        store.init_db()
