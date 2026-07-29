from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import sqlite3
from threading import Barrier

import pytest

from shipfactory import store
from shipfactory.graph_recipe import validate


STORE_RECIPE = validate({
    "name": "recipe",
    "start": "start",
    "boxes": [
        {"id": "start", "name": "Start", "who": "worker", "instructions": "Work."},
        {"id": "approve", "name": "Approve", "who": "human", "instructions": "Decide."},
        {"id": "end", "name": "End", "who": "worker", "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "start", "result": "done", "to": ["approve"]},
        {"from": "approve", "result": "approved", "to": ["end"]},
        {"from": "approve", "result": "rejected", "to": ["start"]},
    ],
})


GRAPH_TABLE_COLUMNS = {
    "recipe_runs_v1": [
        "id", "project_id", "board", "recipe_name", "recipe_hash",
        "recipe_snapshot_json", "request_text", "workspace_path", "launch_key",
        "state", "blocked_reason", "created_at", "updated_at", "completed_at",
    ],
    "box_attempts_v1": [
        "id", "run_id", "box_id", "ordinal", "state", "executor_run_id",
        "input_work_json", "output_work", "result", "technical_failure",
        "created_at", "updated_at", "finished_at",
    ],
    "route_tokens_v1": [
        "id", "run_id", "source_attempt_id", "arrow_index",
        "destination_box_id", "lineage_json", "work_refs_json", "state",
        "created_at", "consumed_at",
    ],
    "split_groups_v1": [
        "id", "run_id", "parent_lineage_json", "branch_ids_json", "state",
        "created_at", "closed_at",
    ],
    "run_events_v1": [
        "key", "run_id", "source", "payload_json", "state", "lease_owner",
        "lease_until", "attempt_count", "outcome", "last_error", "created_at",
        "applied_at",
    ],
    "human_box_decisions_v1": [
        "id", "attempt_id", "result", "actor_kind", "actor_id", "channel",
        "nonce_hash", "created_at", "event_key",
    ],
    "project_recipes_v1": [
        "project_id", "recipe_name", "enabled", "is_default", "created_at", "updated_at",
    ],
}


def _migration(version: int):
    return next(item for item in store._MIGRATIONS if item[0] == version)


def test_migration_17_is_checksummed_idempotent_and_has_exact_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    store.init_db()

    with store._connect() as db:
        row = db.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations WHERE version=17"
        ).fetchone()
        assert row is not None
        migration = _migration(17)
        assert row["name"] == migration[1]
        assert row["checksum"] == hashlib.sha256(migration[2].encode("utf-8")).hexdigest()
        first_applied_at = row["applied_at"]
        for table, expected in GRAPH_TABLE_COLUMNS.items():
            assert [item["name"] for item in db.execute(f"PRAGMA table_info({table})")] == expected

        indexes = {
            item["name"]: item["sql"]
            for item in db.execute(
                """SELECT name,sql FROM sqlite_master
                   WHERE type='index' AND name LIKE '%_v1%' AND sql IS NOT NULL"""
            )
        }
        assert set(indexes) == {
            "idx_recipe_runs_v1_active",
            "idx_box_attempts_v1_ready",
            "idx_route_tokens_v1_active",
            "idx_run_events_v1_pending",
        }
        expected_index_columns = {
            "idx_recipe_runs_v1_active": ["state", "updated_at"],
            "idx_box_attempts_v1_ready": ["state", "run_id", "created_at"],
            "idx_route_tokens_v1_active": ["state", "run_id", "destination_box_id"],
            "idx_run_events_v1_pending": ["state", "lease_until", "created_at"],
        }
        for index, columns in expected_index_columns.items():
            assert [item["name"] for item in db.execute(f"PRAGMA index_info({index})")] == columns
        assert {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_v1_%'"
            )
        } == {
            "trg_route_tokens_v1_logical_unique_insert",
            "trg_route_tokens_v1_logical_unique_update",
        }

    store.init_db()
    with store._connect() as db:
        again = db.execute(
            "SELECT applied_at FROM schema_migrations WHERE version=17"
        ).fetchone()[0]
    assert again == first_applied_at


def test_migration_17_is_additive_only():
    statements = store._MIGRATION_STATEMENTS[17]

    assert statements
    assert all(statement.lstrip().upper().startswith("CREATE ") for statement in statements)
    forbidden = ("ALTER TABLE", "DROP TABLE", "DROP INDEX", "DELETE FROM", "UPDATE ")
    assert not any(
        statement.lstrip().upper().startswith(token)
        for statement in statements
        for token in forbidden
    )


def test_migration_17_preserves_every_version_16_schema_object(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migrations = store._MIGRATIONS
    monkeypatch.setattr(store, "_MIGRATIONS", tuple(item for item in migrations if item[0] <= 16))
    store.init_db()
    with store._connect() as db:
        before = {
            (row["type"], row["name"]): row["sql"]
            for row in db.execute(
                """SELECT type,name,sql FROM sqlite_master
                   WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"""
            )
        }

    monkeypatch.setattr(store, "_MIGRATIONS", migrations)
    store.init_db()
    with store._connect() as db:
        after = {
            key: db.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?", key
            ).fetchone()[0]
            for key in before
        }

    assert after == before


def test_partial_migration_17_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migrations = store._MIGRATIONS
    monkeypatch.setattr(store, "_MIGRATIONS", tuple(item for item in migrations if item[0] <= 16))
    store.init_db()
    monkeypatch.setattr(store, "_MIGRATIONS", migrations)

    with sqlite3.connect(store._db_path()) as db:
        db.execute("CREATE TABLE recipe_runs_v1(id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="schema migration 17 is partially applied"):
        store.init_db()


def test_migration_17_checksum_drift_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    with store._connect() as db:
        db.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=17")

    with pytest.raises(RuntimeError, match="schema migration 17 checksum mismatch"):
        store.init_db()


def test_migration_18_preserves_runs_and_allows_only_nonterminal_escalated(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migrations = store._MIGRATIONS
    monkeypatch.setattr(
        store,
        "_MIGRATIONS",
        tuple(item for item in migrations if item[0] <= 17),
    )
    store.init_db()
    with store._connect() as db:
        _insert_run(db, "preserved-run", "preserved-launch")
        _insert_attempt(db, "preserved-attempt", "preserved-run")
        before = dict(db.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id='preserved-run'",
        ).fetchone())

    monkeypatch.setattr(store, "_MIGRATIONS", migrations)
    store.init_db()

    with store._connect() as db:
        assert db.execute(
            "SELECT MAX(version) FROM schema_migrations",
        ).fetchone()[0] == 18
        assert dict(db.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id='preserved-run'",
        ).fetchone()) == before
        assert db.execute(
            "SELECT run_id FROM box_attempts_v1 WHERE id='preserved-attempt'",
        ).fetchone()[0] == "preserved-run"
        assert [
            row["name"]
            for row in db.execute("PRAGMA index_info(idx_recipe_runs_v1_active)")
        ] == ["state", "updated_at"]

        db.execute(
            """INSERT INTO recipe_runs_v1(
                id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,
                request_text,launch_key,state,blocked_reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "escalated-run", "project", "board", "recipe", "b" * 64, "{}",
                "request", "escalated-launch", "escalated", "{}", "now", "now",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """UPDATE recipe_runs_v1
                   SET completed_at='now' WHERE id='escalated-run'"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """UPDATE recipe_runs_v1
                   SET state='unknown' WHERE id='escalated-run'"""
            )


def _insert_run(db, run_id: str, launch_key: str) -> None:
    db.execute(
        """INSERT INTO recipe_runs_v1(
            id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,request_text,
            workspace_path,launch_key,state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, "project", "board", "recipe", "a" * 64, "{}", "request",
            "/workspace", launch_key, "running", "now", "now",
        ),
    )


def _insert_attempt(db, attempt_id: str, run_id: str) -> None:
    db.execute(
        """INSERT INTO box_attempts_v1(
            id,run_id,box_id,ordinal,state,input_work_json,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (attempt_id, run_id, "box", 1, "ready", "{}", "now", "now"),
    )


def _insert_event(db, key: str, run_id: str) -> None:
    db.execute(
        """INSERT INTO run_events_v1(
            key,run_id,source,payload_json,state,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (key, run_id, "test", "{}", "pending", "now"),
    )


def test_graph_identity_columns_are_not_nullable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with store._connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO recipe_runs_v1(
                id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,request_text,
                workspace_path,launch_key,state,created_at,updated_at
            ) VALUES(NULL,'project','board','recipe',?,'{}','request','/workspace',
                     'launch','running','now','now')""",
            ("a" * 64,),
        )


def test_cross_run_route_reference_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with store._connect() as db:
        _insert_run(db, "run-a", "launch-a")
        _insert_run(db, "run-b", "launch-b")
        _insert_attempt(db, "attempt-a", "run-a")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO route_tokens_v1(
                    id,run_id,source_attempt_id,arrow_index,destination_box_id,
                    lineage_json,work_refs_json,state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "token", "run-b", "attempt-a", 0, "next", "[]", "{}",
                    "pending", "now",
                ),
            )


def test_event_lease_and_terminal_state_constraints_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with store._connect() as db:
        _insert_run(db, "run", "launch")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO run_events_v1(
                    key,run_id,source,payload_json,state,created_at
                ) VALUES('leased','run','test','{}','leased','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO run_events_v1(
                    key,run_id,source,payload_json,state,attempt_count,created_at
                ) VALUES('negative','run','test','{}','pending',-1,'now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO run_events_v1(
                    key,run_id,source,payload_json,state,created_at
                ) VALUES('applied','run','test','{}','applied','now')"""
            )


def test_graph_json_and_numeric_domains_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with store._connect() as db:
        _insert_run(db, "run", "launch")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO recipe_runs_v1(
                    id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,request_text,
                    workspace_path,launch_key,state,created_at,updated_at
                ) VALUES('bad-hash','project','board','recipe',?,'{}','request','/workspace',
                         'bad-hash','running','now','now')""",
                ("z" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO box_attempts_v1(
                    id,run_id,box_id,ordinal,state,input_work_json,created_at,updated_at
                ) VALUES('bad-json','run','box',1,'ready','not-json','now','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO box_attempts_v1(
                    id,run_id,box_id,ordinal,state,input_work_json,created_at,updated_at
                ) VALUES('bad-ordinal','run','box',0,'ready','{}','now','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO split_groups_v1(
                    id,run_id,parent_lineage_json,branch_ids_json,state,created_at
                ) VALUES('split','run','[]','["only"]','open','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO route_tokens_v1(
                    id,run_id,destination_box_id,lineage_json,work_refs_json,state,
                    created_at
                ) VALUES('bad-lineage','run','box','{}','{}','pending','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO split_groups_v1(
                    id,run_id,parent_lineage_json,branch_ids_json,state,created_at
                ) VALUES('bad-lineage','run','{}','["a","b"]','open','now')"""
            )
        db.execute(
            """INSERT INTO route_tokens_v1(
                id,run_id,destination_box_id,lineage_json,work_refs_json,state,created_at
            ) VALUES('root','run','box','[]','{}','pending','now')"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="logical identity"):
            db.execute(
                """INSERT INTO route_tokens_v1(
                    id,run_id,destination_box_id,lineage_json,work_refs_json,state,created_at
                ) VALUES('duplicate-root','run','box','[]','{}','pending','now')"""
            )
        db.execute(
            """INSERT INTO route_tokens_v1(
                id,run_id,destination_box_id,lineage_json,work_refs_json,state,created_at
            ) VALUES('other-root','run','other','[]','{}','pending','now')"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="logical identity"):
            db.execute(
                "UPDATE route_tokens_v1 SET destination_box_id='box' WHERE id='other-root'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO route_tokens_v1(
                    id,run_id,source_attempt_id,destination_box_id,lineage_json,
                    work_refs_json,state,created_at
                ) VALUES('half-root','run','attempt','box','[]','{}','pending','now')"""
            )


def test_graph_lifecycle_timestamps_match_terminal_states(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO recipe_runs_v1(
                    id,project_id,board,recipe_name,recipe_hash,recipe_snapshot_json,request_text,
                    workspace_path,launch_key,state,created_at,updated_at
                ) VALUES('done','project','board','recipe',?,'{}','request','/workspace',
                         'done','completed','now','now')""",
                ("a" * 64,),
            )
        _insert_run(db, "run", "launch")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO box_attempts_v1(
                    id,run_id,box_id,ordinal,state,input_work_json,created_at,updated_at
                ) VALUES('attempt','run','box',1,'completed','{}','now','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO route_tokens_v1(
                    id,run_id,destination_box_id,lineage_json,work_refs_json,state,
                    created_at
                ) VALUES('token','run','box','[]','{}','consumed','now')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO split_groups_v1(
                    id,run_id,parent_lineage_json,branch_ids_json,state,created_at
                ) VALUES('split','run','[]','["a","b"]','closed','now')"""
            )


def test_index_only_partial_migration_17_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migrations = store._MIGRATIONS
    monkeypatch.setattr(store, "_MIGRATIONS", tuple(item for item in migrations if item[0] <= 16))
    store.init_db()
    monkeypatch.setattr(store, "_MIGRATIONS", migrations)
    with sqlite3.connect(store._db_path()) as db:
        db.execute("CREATE TABLE unrelated_graph_probe(id TEXT)")
        db.execute(
            "CREATE INDEX idx_run_events_v1_pending ON unrelated_graph_probe(id)"
        )

    with pytest.raises(RuntimeError, match="schema migration 17 is partially applied"):
        store.init_db()


def test_trigger_only_partial_migration_17_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migrations = store._MIGRATIONS
    monkeypatch.setattr(store, "_MIGRATIONS", tuple(item for item in migrations if item[0] <= 16))
    store.init_db()
    monkeypatch.setattr(store, "_MIGRATIONS", migrations)
    with sqlite3.connect(store._db_path()) as db:
        db.execute("CREATE TABLE unrelated_trigger_probe(id TEXT)")
        db.execute(
            """CREATE TRIGGER trg_route_tokens_v1_logical_unique_insert
               BEFORE INSERT ON unrelated_trigger_probe BEGIN SELECT 1; END"""
        )

    with pytest.raises(RuntimeError, match="schema migration 17 is partially applied"):
        store.init_db()


def test_failed_migration_17_rolls_back_all_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migrations = store._MIGRATIONS
    monkeypatch.setattr(store, "_MIGRATIONS", tuple(item for item in migrations if item[0] <= 16))
    store.init_db()

    bad_statements = (
        "CREATE TABLE rollback_probe_v1(id TEXT PRIMARY KEY)",
        "THIS IS NOT SQL",
    )
    bad_text = ";\n".join(bad_statements) + ";\n"
    monkeypatch.setattr(
        store,
        "_MIGRATIONS",
        tuple(item for item in migrations if item[0] <= 16)
        + ((17, "graphrunner_v1_durable_state", bad_text),),
    )
    monkeypatch.setitem(store._MIGRATION_STATEMENTS, 17, bad_statements)

    with pytest.raises(sqlite3.OperationalError):
        store.init_db()
    with sqlite3.connect(store._db_path()) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='rollback_probe_v1'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=17"
        ).fetchone()[0] == 0


def _create_run_via_store(*, run_id: str = "run", launch_key: str = "launch", conn=None):
    return store.create_recipe_run_v1(
        run_id=run_id,
        project_id="project",
        board="board",
        recipe_name=STORE_RECIPE.name,
        recipe_hash=STORE_RECIPE.hash,
        recipe_snapshot_json=STORE_RECIPE.canonical_json,
        request_text="request",
        workspace_path="/workspace",
        launch_key=launch_key,
        conn=conn,
    )


def test_recipe_run_store_api_is_launch_idempotent_and_lists_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    first = _create_run_via_store()
    replay = _create_run_via_store(run_id="ignored")

    assert replay == first
    assert store.get_recipe_run_v1("run")["recipe_snapshot_json"] == STORE_RECIPE.canonical_json
    assert [row["id"] for row in store.list_recipe_runs_v1(project_id="project")] == ["run"]
    with pytest.raises(store.GraphStoreConflict, match="launch key"):
        store.create_recipe_run_v1(
            run_id="conflict", project_id="other", board="board", recipe_name="recipe",
            recipe_hash=STORE_RECIPE.hash,
            recipe_snapshot_json=STORE_RECIPE.canonical_json, request_text="other",
            workspace_path=None, launch_key="launch",
        )
    with pytest.raises(store.GraphStoreConflict, match="different content"):
        store.create_recipe_run_v1(
            run_id="run", project_id="project", board="board", recipe_name="recipe",
            recipe_hash=STORE_RECIPE.hash,
            recipe_snapshot_json=STORE_RECIPE.canonical_json, request_text="request",
            workspace_path="/workspace", launch_key="different-launch",
        )


def test_store_functions_share_callers_transaction(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _create_run_via_store(conn=db)
        store.insert_box_attempt_v1(
            attempt_id="attempt", run_id="run", box_id="start", ordinal=1,
            state="ready", input_work={}, conn=db,
        )
        db.rollback()

    assert store.get_recipe_run_v1("run") is None


def test_public_store_wrappers_close_connections(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    captured = []
    real_connect = store._connect

    def capture_connect():
        connection = real_connect()
        captured.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", capture_connect)
    _create_run_via_store()

    assert captured
    for connection in captured:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_box_attempt_insert_and_compare_and_swap_update(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    first = store.insert_box_attempt_v1(
        attempt_id="attempt", run_id="run", box_id="start", ordinal=1,
        state="ready", input_work={"request": "do it"},
    )
    replay = store.insert_box_attempt_v1(
        attempt_id="ignored", run_id="run", box_id="start", ordinal=1,
        state="ready", input_work={"request": "do it"},
    )
    assert replay == first

    completed = store.update_box_attempt_v1(
        "attempt", expected_state="ready", state="completed",
        output_work="done", result="done",
    )
    assert completed["state"] == "completed"
    assert completed["finished_at"] is not None
    delayed_replay = store.insert_box_attempt_v1(
        attempt_id="late-retry", run_id="run", box_id="start", ordinal=1,
        state="ready", input_work={"request": "do it"},
    )
    assert delayed_replay["id"] == "attempt"
    assert delayed_replay["state"] == "completed"
    with pytest.raises(store.GraphStoreConflict, match="state"):
        store.update_box_attempt_v1(
            "attempt", expected_state="ready", state="running",
        )


def test_route_token_insert_is_idempotent_and_consumption_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    token = store.insert_route_token_v1(
        token_id="token", run_id="run", source_attempt_id=None, arrow_index=None,
        destination_box_id="start", lineage=[], work_refs={"request": "request"},
    )
    replay = store.insert_route_token_v1(
        token_id="same-logical-token", run_id="run", source_attempt_id=None,
        arrow_index=None,
        destination_box_id="start", lineage=[], work_refs={"request": "request"},
    )
    assert replay == token
    with pytest.raises(store.GraphStoreConflict, match="token"):
        store.insert_route_token_v1(
            token_id="token", run_id="run", source_attempt_id=None, arrow_index=None,
            destination_box_id="start", lineage=[], work_refs={},
        )

    consumed = store.consume_route_tokens_v1(
        run_id="run", destination_box_id="start", token_ids=["token"],
    )
    assert [row["id"] for row in consumed] == ["token"]
    assert consumed[0]["state"] == "consumed"
    with pytest.raises(store.GraphStoreConflict, match="missing, consumed"):
        store.consume_route_tokens_v1(
            run_id="run", destination_box_id="start", token_ids=["token"],
        )


def test_route_token_consumption_rejects_partial_requested_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    store.insert_route_token_v1(
        token_id="token", run_id="run", source_attempt_id=None, arrow_index=None,
        destination_box_id="start", lineage=[], work_refs={},
    )

    with pytest.raises(store.GraphStoreConflict, match="missing, consumed"):
        store.consume_route_tokens_v1(
            run_id="run", destination_box_id="start", token_ids=["token", "missing"],
        )
    with store._connect() as db:
        assert db.execute(
            "SELECT state FROM route_tokens_v1 WHERE id='token'"
        ).fetchone()[0] == "pending"


def test_root_route_token_idempotency_is_safe_under_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    monkeypatch.setattr(store, "init_db", lambda: None)
    barrier = Barrier(2)

    def insert(token_id: str):
        barrier.wait()
        return store.insert_route_token_v1(
            token_id=token_id, run_id="run", source_attempt_id=None, arrow_index=None,
            destination_box_id="start", lineage=[], work_refs={"request": "request"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(insert, ["token-a", "token-b"]))

    assert len({row["id"] for row in rows}) == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM route_tokens_v1").fetchone()[0] == 1


def test_route_token_public_lock_precedes_idempotency_query():
    source = inspect.getsource(store.insert_route_token_v1)

    begin = source.index('db.execute("BEGIN IMMEDIATE")')
    recursive_call = source.index("return insert_route_token_v1(", begin)
    lookup = source.index("existing = _graph_row", recursive_call)

    assert begin < recursive_call < lookup


def test_public_insert_apis_distinguish_invalid_input_from_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()

    with pytest.raises(store.GraphStoreIntegrityError):
        store.create_recipe_run_v1(
            run_id="bad", project_id="project", board="board", recipe_name="recipe",
            recipe_hash="not-a-hash", recipe_snapshot_json="{}", request_text="request",
            workspace_path=None, launch_key="bad-launch",
        )
    with pytest.raises(store.GraphStoreIntegrityError, match="canonical"):
        store.create_recipe_run_v1(
            run_id="noncanonical", project_id="project", board="board",
            recipe_name=STORE_RECIPE.name, recipe_hash=STORE_RECIPE.hash,
            recipe_snapshot_json=json.dumps(json.loads(STORE_RECIPE.canonical_json), indent=2),
            request_text="request", workspace_path=None, launch_key="noncanonical",
        )
    with pytest.raises(store.GraphStoreIntegrityError, match="unknown box"):
        store.insert_box_attempt_v1(
            attempt_id="unknown-box", run_id="run", box_id="missing", ordinal=1,
            state="ready", input_work={},
        )
    with pytest.raises(store.GraphStoreIntegrityError):
        store.insert_box_attempt_v1(
            attempt_id="missing-parent", run_id="missing", box_id="start", ordinal=1,
            state="ready", input_work={},
        )
    with pytest.raises(store.GraphStoreIntegrityError):
        store.insert_route_token_v1(
            token_id="missing-run", run_id="missing", source_attempt_id=None,
            arrow_index=None, destination_box_id="start", lineage=[], work_refs={},
        )
    with pytest.raises(store.GraphStoreIntegrityError):
        store.enqueue_run_event_v1(
            key="missing-run", run_id="missing", source="test", payload={},
        )


def test_route_and_human_writes_are_bound_to_frozen_recipe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    store.insert_box_attempt_v1(
        attempt_id="bad-completion", run_id="run", box_id="start", ordinal=2,
        state="ready", input_work={},
    )
    with pytest.raises(store.GraphStoreIntegrityError, match="result is not declared"):
        store.update_box_attempt_v1(
            "bad-completion", expected_state="ready", state="completed",
            output_work="done", result="invented",
        )
    store.insert_box_attempt_v1(
        attempt_id="start-attempt", run_id="run", box_id="start", ordinal=1,
        state="ready", input_work={},
    )
    store.update_box_attempt_v1(
        "start-attempt", expected_state="ready", state="completed",
        output_work="done", result="done",
    )
    with pytest.raises(store.GraphStoreIntegrityError, match="both be null or non-null"):
        store.insert_route_token_v1(
            token_id="half-root", run_id="run", source_attempt_id="start-attempt",
            arrow_index=None, destination_box_id="approve", lineage=[], work_refs={},
        )
    with pytest.raises(store.GraphStoreIntegrityError, match="do not match recipe"):
        store.insert_route_token_v1(
            token_id="wrong-destination", run_id="run",
            source_attempt_id="start-attempt", arrow_index=0,
            destination_box_id="end", lineage=[], work_refs={},
        )
    with store._connect() as db:
        db.execute(
            """INSERT INTO box_attempts_v1(
                id,run_id,box_id,ordinal,state,input_work_json,result,
                created_at,updated_at,finished_at
            ) VALUES('forged-result','run','start',3,'completed','{}','invented',
                     'now','now','now')"""
        )
    with pytest.raises(store.GraphStoreIntegrityError, match="do not match recipe"):
        store.insert_route_token_v1(
            token_id="wrong-result", run_id="run",
            source_attempt_id="forged-result", arrow_index=0,
            destination_box_id="approve", lineage=[], work_refs={},
        )

    store.insert_box_attempt_v1(
        attempt_id="human", run_id="run", box_id="approve", ordinal=1,
        state="waiting_human", input_work={},
    )
    store.enqueue_run_event_v1(
        key="decision-event", run_id="run", source="human", payload={},
    )
    with pytest.raises(store.GraphStoreIntegrityError, match="result is not declared"):
        store.record_human_box_decision_v1(
            decision_id="bad-result", attempt_id="human", result="invented",
            actor_kind="human", actor_id="operator", channel="dashboard",
            nonce_hash="bad-result", event_key="decision-event",
        )


def test_event_lease_expiry_recovery_and_owner_fencing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    store.enqueue_run_event_v1(
        key="event", run_id="run", source="test", payload={"value": 1},
    )

    first = store.lease_run_events_v1(
        owner="first", limit=1, lease_seconds=10, now="2026-01-01T00:00:00Z",
    )
    assert first[0]["attempt_count"] == 1
    assert store.lease_run_events_v1(
        owner="second", limit=1, lease_seconds=10, now="2026-01-01T00:00:05+00:00",
    ) == []
    with pytest.raises(store.GraphStoreConflict, match="lease"):
        store.finish_run_event_v1(
            "event", owner="first", expected_attempt_count=1,
            state="applied", outcome="too late",
            now="2026-01-01T00:00:11+00:00",
        )
    second = store.lease_run_events_v1(
        owner="second", limit=1, lease_seconds=10, now="2026-01-01T00:00:11+00:00",
    )
    assert second[0]["attempt_count"] == 2
    with pytest.raises(store.GraphStoreConflict, match="lease"):
        store.finish_run_event_v1(
            "event", owner="first", expected_attempt_count=2,
            state="applied", outcome="wrong owner",
            now="2026-01-01T00:00:12+00:00",
        )
    finished = store.finish_run_event_v1(
        "event", owner="second", expected_attempt_count=2,
        state="applied", outcome="ok",
        now="2026-01-01T00:00:12+00:00",
    )
    assert finished["state"] == "applied"


def test_stale_event_discard_and_event_enqueue_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    first = store.enqueue_run_event_v1(
        key="stale", run_id="run", source="test", payload={"revision": 1},
    )
    assert store.enqueue_run_event_v1(
        key="stale", run_id="run", source="test", payload={"revision": 1},
    ) == first
    with pytest.raises(store.GraphStoreConflict, match="event"):
        store.enqueue_run_event_v1(
            key="stale", run_id="run", source="test", payload={"revision": 2},
        )
    leased = store.lease_run_events_v1(owner="daemon", limit=1)
    discarded = store.finish_run_event_v1(
        "stale", owner="daemon", expected_attempt_count=leased[0]["attempt_count"],
        state="discarded", outcome="stale activation",
    )
    assert discarded["state"] == "discarded"
    assert store.lease_run_events_v1(owner="daemon", limit=1) == []


def test_event_attempt_count_fences_same_owner_reacquisition(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    store.enqueue_run_event_v1(key="event", run_id="run", source="test", payload={})
    first = store.lease_run_events_v1(
        owner="daemon", limit=1, lease_seconds=10, now="2026-01-01T00:00:00Z",
    )[0]
    second = store.lease_run_events_v1(
        owner="daemon", limit=1, lease_seconds=10, now="2026-01-01T00:00:11Z",
    )[0]

    with pytest.raises(store.GraphStoreConflict, match="lease"):
        store.finish_run_event_v1(
            "event", owner="daemon", expected_attempt_count=first["attempt_count"],
            state="applied", now="2026-01-01T00:00:12Z",
        )
    assert store.finish_run_event_v1(
        "event", owner="daemon", expected_attempt_count=second["attempt_count"],
        state="applied", now="2026-01-01T00:00:12Z",
    )["state"] == "applied"


def test_human_decision_is_idempotent_and_bound_to_attempt_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _create_run_via_store()
    store.insert_box_attempt_v1(
        attempt_id="human", run_id="run", box_id="approve", ordinal=1,
        state="waiting_human", input_work={},
    )
    store.enqueue_run_event_v1(
        key="decision-event", run_id="run", source="human", payload={},
    )
    first = store.record_human_box_decision_v1(
        decision_id="decision", attempt_id="human", result="approved",
        actor_kind="human", actor_id="operator", channel="dashboard",
        nonce_hash="nonce", event_key="decision-event",
    )
    replay = store.record_human_box_decision_v1(
        decision_id="ignored", attempt_id="human", result="approved",
        actor_kind="human", actor_id="operator", channel="dashboard",
        nonce_hash="nonce", event_key="decision-event",
    )
    assert replay == first

    _create_run_via_store(run_id="other", launch_key="other-launch")
    store.enqueue_run_event_v1(
        key="other-event", run_id="other", source="human", payload={},
    )
    with pytest.raises(store.GraphStoreIntegrityError, match="same run"):
        store.record_human_box_decision_v1(
            decision_id="cross-run", attempt_id="human", result="approved",
            actor_kind="human", actor_id="operator", channel="dashboard",
            nonce_hash="other-nonce", event_key="other-event",
        )
