"""SQLite persistence for Hermes Factory state."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DAEMON_RUN_TASK_ID = "__shipfactory_daemon__"


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, seat TEXT NOT NULL,
  executor TEXT NOT NULL, model TEXT NOT NULL, pid INTEGER, started_at TEXT NOT NULL,
  ended_at TEXT, exit_code INTEGER, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  tokens_total INTEGER DEFAULT 0, duration_s REAL, result TEXT);
CREATE TABLE IF NOT EXISTS policies (task_id TEXT PRIMARY KEY, policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, stage_id TEXT NOT NULL,
  stage_type TEXT NOT NULL, seat TEXT NOT NULL, outcome TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitors (
  task_id TEXT PRIMARY KEY, next_check_at TEXT NOT NULL, timeout_at TEXT,
  max_attempts INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
  recovery_policy TEXT NOT NULL, notes TEXT, scheduled_by TEXT,
  interval_seconds INTEGER NOT NULL DEFAULT 300);
CREATE TABLE IF NOT EXISTS watchdogs (
  root_task_id TEXT PRIMARY KEY, agent TEXT NOT NULL, instructions TEXT NOT NULL, last_fingerprint TEXT);
CREATE TABLE IF NOT EXISTS seat_state (seat TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sync (
  gh_number INTEGER PRIMARY KEY, task_id TEXT NOT NULL, gh_updated TEXT, k_updated TEXT, last_synced_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_versions (
  id TEXT NOT NULL, version INTEGER NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL,
  normalized_yaml TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS recipe_instances (
  id TEXT PRIMARY KEY, board TEXT NOT NULL, collector_task_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL, recipe_version INTEGER NOT NULL, recipe_hash TEXT NOT NULL,
  status TEXT NOT NULL, parameters_json TEXT NOT NULL, activation_count INTEGER NOT NULL DEFAULT 0,
  tokens_charged INTEGER NOT NULL DEFAULT 0, blocked_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_steps (
  instance_id TEXT NOT NULL, step_id TEXT NOT NULL, activation INTEGER NOT NULL,
  primitive TEXT NOT NULL, state TEXT NOT NULL, kanban_task_id TEXT UNIQUE,
  input_revision_hash TEXT, output_revision INTEGER, finding_count INTEGER, blocked_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(instance_id, step_id, activation),
  FOREIGN KEY(instance_id) REFERENCES recipe_instances(id));
CREATE TABLE IF NOT EXISTS advance_events (
  key TEXT PRIMARY KEY, instance_id TEXT, source TEXT NOT NULL, payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, applied_at TEXT);
CREATE INDEX IF NOT EXISTS idx_advance_events_pending ON advance_events(state, created_at);
CREATE TABLE IF NOT EXISTS budget_charges (
  key TEXT PRIMARY KEY, board TEXT NOT NULL, utc_day TEXT NOT NULL, instance_id TEXT NOT NULL,
  step_id TEXT NOT NULL, activation INTEGER NOT NULL, tokens INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_budget_charges_day ON budget_charges(board, utc_day);
CREATE TABLE IF NOT EXISTS outbox (
  key TEXT PRIMARY KEY, target TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, delivered_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS triage_selections (
  id TEXT PRIMARY KEY, source_task_id TEXT NOT NULL UNIQUE, board TEXT NOT NULL, lease_until TEXT,
  ranked_json TEXT NOT NULL, chosen_recipe TEXT, parameters_json TEXT, skip_steps_json TEXT,
  outcome TEXT, root_collector_task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


_A0_MIGRATION_STATEMENTS = (
    "ALTER TABLE advance_events ADD COLUMN lease_owner TEXT",
    "ALTER TABLE advance_events ADD COLUMN lease_until TEXT",
    "ALTER TABLE advance_events ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE advance_events ADD COLUMN expected_activation INTEGER",
    "ALTER TABLE advance_events ADD COLUMN expected_state TEXT",
    "ALTER TABLE advance_events ADD COLUMN outcome TEXT",
    "ALTER TABLE advance_events ADD COLUMN last_error TEXT",
    "ALTER TABLE outbox ADD COLUMN lease_owner TEXT",
    "ALTER TABLE outbox ADD COLUMN lease_until TEXT",
    """CREATE TABLE action_intents (
    key             TEXT PRIMARY KEY,
    logical_key     TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    instance_id     TEXT,
    step_id         TEXT,
    activation      INTEGER,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    state           TEXT NOT NULL,
    lease_owner     TEXT,
    lease_until     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    result_json     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(logical_key, attempt)
)""",
    """CREATE INDEX idx_action_intents_ready
ON action_intents(state, lease_until, created_at)""",
    """CREATE TABLE resource_leases (
    key               TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    units             INTEGER NOT NULL,
    instance_id       TEXT,
    step_id           TEXT,
    activation        INTEGER,
    state             TEXT NOT NULL,
    lease_until       TEXT,
    metadata_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    released_at       TEXT
)""",
)
_A0_MIGRATION_TEXT = ";\n".join(_A0_MIGRATION_STATEMENTS) + ";\n"
_A1_MIGRATION_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN board TEXT",
    "ALTER TABLE runs ADD COLUMN workspace_path TEXT",
    "ALTER TABLE runs ADD COLUMN log_path TEXT",
    "ALTER TABLE runs ADD COLUMN prompt_path TEXT",
    "ALTER TABLE runs ADD COLUMN provider TEXT",
    "ALTER TABLE runs ADD COLUMN resolved_model TEXT",
    "ALTER TABLE runs ADD COLUMN executor_version TEXT",
    "ALTER TABLE runs ADD COLUMN process_start_token TEXT",
    "ALTER TABLE monitors ADD COLUMN state TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE monitors ADD COLUMN last_outcome TEXT",
    "ALTER TABLE monitors ADD COLUMN last_error TEXT",
    "ALTER TABLE monitors ADD COLUMN last_checked_at TEXT",
    "CREATE INDEX idx_resource_leases_active ON resource_leases(kind,state,lease_until)",
)
_A1_MIGRATION_TEXT = ";\n".join(_A1_MIGRATION_STATEMENTS) + ";\n"
_A1_FENCING_MIGRATION_STATEMENTS = (
    "ALTER TABLE runs ADD COLUMN task_attempt_id INTEGER",
)
_A1_FENCING_MIGRATION_TEXT = ";\n".join(_A1_FENCING_MIGRATION_STATEMENTS) + ";\n"
_ARTIFACT_MIGRATION_STATEMENTS = (
    """CREATE TABLE artifacts (
    id                    TEXT PRIMARY KEY,
    instance_id           TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    activation            INTEGER NOT NULL,
    run_id                INTEGER,
    kind                  TEXT NOT NULL,
    schema_version        INTEGER NOT NULL,
    state                 TEXT NOT NULL,
    candidate_path        TEXT,
    sealed_path           TEXT,
    sha256                TEXT,
    size_bytes            INTEGER,
    producer              TEXT NOT NULL,
    trust_domain          TEXT,
    base_sha              TEXT NOT NULL,
    head_sha              TEXT,
    repo_tree_sha         TEXT NOT NULL,
    validation_error      TEXT,
    created_at            TEXT NOT NULL,
    sealed_at             TEXT,
    UNIQUE(instance_id, step_id, activation, kind)
)""",
    """CREATE TABLE artifact_edges (
    parent_artifact_id  TEXT NOT NULL,
    child_artifact_id   TEXT NOT NULL,
    relation            TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
)""",
    "ALTER TABLE recipe_steps ADD COLUMN input_artifact_set_hash TEXT",
    "ALTER TABLE recipe_steps ADD COLUMN output_artifact_set_hash TEXT",
)
_ARTIFACT_MIGRATION_TEXT = ";\n".join(_ARTIFACT_MIGRATION_STATEMENTS) + ";\n"
_INSTANCE_BASE_MIGRATION_STATEMENTS = (
    "ALTER TABLE recipe_instances ADD COLUMN base_sha TEXT",
    "ALTER TABLE recipe_instances ADD COLUMN updated_base_at TEXT",
    """UPDATE recipe_instances
SET base_sha=(
        SELECT a.base_sha FROM artifacts a
        WHERE a.instance_id=recipe_instances.id AND a.state='sealed'
        ORDER BY a.sealed_at DESC,a.created_at DESC LIMIT 1
    ),
    updated_base_at=(
        SELECT COALESCE(a.sealed_at,a.created_at) FROM artifacts a
        WHERE a.instance_id=recipe_instances.id AND a.state='sealed'
        ORDER BY a.sealed_at DESC,a.created_at DESC LIMIT 1
    )""",
)
_INSTANCE_BASE_MIGRATION_TEXT = ";\n".join(_INSTANCE_BASE_MIGRATION_STATEMENTS) + ";\n"
_PLANNING_BUDGET_MIGRATION_STATEMENTS = (
    "ALTER TABLE budget_charges ADD COLUMN token_pool TEXT",
    "CREATE INDEX idx_budget_charges_pool ON budget_charges(instance_id,token_pool)",
)
_PLANNING_BUDGET_MIGRATION_TEXT = ";\n".join(
    _PLANNING_BUDGET_MIGRATION_STATEMENTS
) + ";\n"
_MIGRATIONS = (
    (1, "a0_single_writer_recoverable_actions", _A0_MIGRATION_TEXT),
    (2, "a1_durable_runs_resource_governor", _A1_MIGRATION_TEXT),
    (3, "a1_worker_transition_attempt_fencing", _A1_FENCING_MIGRATION_TEXT),
    (4, "sf5_artifact_revision_identity", _ARTIFACT_MIGRATION_TEXT),
    (5, "sf5_instance_base_identity", _INSTANCE_BASE_MIGRATION_TEXT),
    (6, "sf6_named_token_pool_charges", _PLANNING_BUDGET_MIGRATION_TEXT),
)
_MIGRATION_STATEMENTS = {
    1: _A0_MIGRATION_STATEMENTS,
    2: _A1_MIGRATION_STATEMENTS,
    3: _A1_FENCING_MIGRATION_STATEMENTS,
    4: _ARTIFACT_MIGRATION_STATEMENTS,
    5: _INSTANCE_BASE_MIGRATION_STATEMENTS,
    6: _PLANNING_BUDGET_MIGRATION_STATEMENTS,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "shipfactory" / "shipfactory.db"


class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose ``with`` block commits AND closes.

    Finding #27 (2026-07-14): stock ``with sqlite3.connect(...)`` is a
    TRANSACTION scope — it commits/rolls back on exit but never closes the
    handle. Every ``with _connect()`` in this package therefore leaked one
    fd per call; the daemon leaked ~13/hour against macOS's default 256
    soft limit, and EMFILE surfaces as SQLite "disk I/O error" + index
    corruption (finding #21 was this leak's downstream symptom).
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")  # #16-V2: wait out concurrent writers.
    conn.execute("PRAGMA journal_mode = WAL")  # #16-V2: readers do not block writers.
    return conn


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def init_db() -> None:
    """Create the base schema and transactionally apply verified migrations."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript commits implicitly, so execute the bootstrap DDL one
            # statement at a time inside our explicit transaction.
            for statement in _BASE_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            # Normalize the two pre-migration legacy schemas that shipped
            # before schema_migrations existed. These are bootstrap upgrades,
            # not A0 migrations; all subsequent changes are numbered below.
            monitor_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(monitors)")
            }
            if "interval_seconds" not in monitor_columns:
                conn.execute(
                    "ALTER TABLE monitors ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 300"
                )
            step_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(recipe_steps)")
            }
            if "finding_count" not in step_columns:
                conn.execute("ALTER TABLE recipe_steps ADD COLUMN finding_count INTEGER")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )""")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        for version, name, migration in _MIGRATIONS:
            checksum = hashlib.sha256(migration.encode("utf-8")).hexdigest()
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                existing = next((row for row in rows if row["version"] == version), None)
                if existing is not None:
                    if existing["name"] != name or existing["checksum"] != checksum:
                        raise RuntimeError(f"schema migration {version} checksum mismatch")
                    conn.commit()
                    continue
                if any(int(row["version"]) > version for row in rows):
                    raise RuntimeError(f"schema migration {version} is partially applied")
                prior = max((int(row["version"]) for row in rows), default=0)
                if prior != version - 1:
                    raise RuntimeError(
                        f"schema migration {version} requires prior version {version - 1}, found {prior}"
                    )
                existing_tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if version == 1:
                    event_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(advance_events)"
                    )}
                    outbox_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(outbox)"
                    )}
                    migration_artifacts = (
                        "lease_owner" in event_columns
                        or "lease_owner" in outbox_columns
                        or bool({"action_intents", "resource_leases"} & existing_tables)
                    )
                elif version == 2:
                    run_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(runs)"
                    )}
                    monitor_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(monitors)"
                    )}
                    indexes = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )}
                    migration_artifacts = bool(
                        {"board", "workspace_path", "process_start_token"} & run_columns
                        or {"state", "last_outcome"} & monitor_columns
                        or "idx_resource_leases_active" in indexes
                    )
                elif version == 3:
                    run_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(runs)"
                    )}
                    migration_artifacts = "task_attempt_id" in run_columns
                elif version == 4:
                    step_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_steps)"
                    )}
                    migration_artifacts = bool(
                        {"artifacts", "artifact_edges"} & existing_tables
                        or {"input_artifact_set_hash", "output_artifact_set_hash"}
                        & step_columns
                    )
                elif version == 5:
                    instance_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(recipe_instances)"
                    )}
                    migration_artifacts = bool(
                        {"base_sha", "updated_base_at"} & instance_columns
                    )
                else:
                    charge_columns = {row["name"] for row in conn.execute(
                        "PRAGMA table_info(budget_charges)"
                    )}
                    indexes = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )}
                    migration_artifacts = bool(
                        "token_pool" in charge_columns
                        or "idx_budget_charges_pool" in indexes
                    )
                if migration_artifacts:
                    raise RuntimeError(f"schema migration {version} is partially applied")
                for statement in _MIGRATION_STATEMENTS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                    (version, name, checksum, _now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def record_run_start(task_id, seat, executor, model, pid=None, *, board=None,
                     workspace_path=None, log_path=None, prompt_path=None,
                     provider=None, resolved_model=None, executor_version=None,
                     process_start_token=None, task_attempt_id=None) -> int:
    """Insert a running harness execution and return its run id."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(task_id,seat,executor,model,pid,started_at,tokens_in,tokens_out,"
            "tokens_total,board,workspace_path,log_path,prompt_path,provider,resolved_model,"
            "executor_version,process_start_token,task_attempt_id) "
            "VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?)",
            (task_id, seat, executor, model or "", pid, _now(), board,
             str(workspace_path) if workspace_path is not None else None,
             str(log_path) if log_path is not None else None,
             str(prompt_path) if prompt_path is not None else None,
             provider, resolved_model, executor_version, process_start_token,
             int(task_attempt_id) if task_attempt_id is not None else None),
        )
        return int(cur.lastrowid)


def record_run_spawned(run_id: int, pid: int, process_start_token: str | None) -> None:
    """Attach the OS identity only after a pre-spawn run row is durable."""
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE runs SET pid=?,process_start_token=? WHERE id=? AND ended_at IS NULL",
            (int(pid), process_start_token, int(run_id)),
        ).rowcount
        if changed != 1:
            raise ValueError(f"unknown or terminal run {run_id}")


def nonterminal_runs() -> list[dict[str, Any]]:
    """Return durable worker runs which still need process reconciliation."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL AND task_id<>? ORDER BY id",
            (DAEMON_RUN_TASK_ID,),
        ))


def run_row(run_id: int) -> dict[str, Any] | None:
    """Return one durable run row."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (int(run_id),)).fetchone()
    return dict(row) if row else None


def record_run_end(run_id, exit_code, tokens_in, tokens_out, duration_s, result) -> None:
    """Finalize a harness execution with usage and outcome."""
    init_db()
    tokens_in = int(tokens_in) if tokens_in is not None else None
    tokens_out = int(tokens_out) if tokens_out is not None else None
    tokens_total = (
        tokens_in + tokens_out
        if tokens_in is not None and tokens_out is not None else None
    )
    with _connect() as conn:
        conn.execute("UPDATE runs SET ended_at=?,exit_code=?,tokens_in=?,tokens_out=?,tokens_total=?,duration_s=?,result=? WHERE id=?",
                     (_now(), exit_code, tokens_in, tokens_out, tokens_total, duration_s, result, run_id))


def record_run_crashed(run_id: int, reason: str = "process identity unavailable") -> None:
    """Durably terminate a run whose recorded OS identity cannot be adopted."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET ended_at=?,exit_code=-1,result=? WHERE id=? AND ended_at IS NULL",
            (_now(), f"crashed: {reason}"[:500], int(run_id)),
        )


def _daemon_payload(
    boards: list[str],
    last_tick_at: dict[str, str | None],
    *,
    tick_interval: float,
) -> dict[str, Any]:
    """Build the one-release-compatible daemon liveness payload."""
    return {
        "kind": "shipfactory_daemon",
        "board": boards[0],
        "board_deprecation": "board is retained for one release; use boards",
        "boards": boards,
        "last_tick_at": last_tick_at,
        "tick_interval_seconds": tick_interval,
    }


def record_daemon_start(
    board: str,
    pid: int,
    *,
    boards: list[str] | None = None,
    tick_interval: float = 5.0,
) -> int:
    """Insert a durable Factory-daemon run record for all served boards."""
    names = list(dict.fromkeys(boards or [board]))
    run_id = record_run_start(DAEMON_RUN_TASK_ID, names[0], "shipfactory-daemon", "", pid)
    payload = _daemon_payload(
        names,
        {name: None for name in names},
        tick_interval=float(tick_interval),
    )
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET result=? WHERE id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), run_id),
        )
    return run_id


def record_daemon_tick(run_id: int, board: str) -> str:
    """Persist one board's latest completed tick on its daemon run record."""
    ticked_at = _now()
    with _connect() as conn:
        row = conn.execute("SELECT seat,result FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown daemon run {run_id}")
        try:
            payload = json.loads(row["result"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        names = payload.get("boards")
        if not isinstance(names, list) or not names:
            names = [str(payload.get("board") or row["seat"])]
        if board not in names:
            names.append(board)
        ticks = payload.get("last_tick_at")
        if not isinstance(ticks, dict):
            ticks = {names[0]: ticks}
        ticks = {name: ticks.get(name) for name in names}
        ticks[board] = ticked_at
        result = json.dumps(
            _daemon_payload(
                names,
                ticks,
                tick_interval=float(payload.get("tick_interval_seconds") or 5.0),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute("UPDATE runs SET result=? WHERE id=?", (result, run_id))
    return ticked_at


def record_daemon_end(run_id: int) -> None:
    """Mark a daemon run cleanly stopped without changing its last tick."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET ended_at=?,exit_code=0 WHERE id=? AND ended_at IS NULL",
            (_now(), run_id),
        )


def latest_daemon_run(board: str | None = None) -> dict[str, Any] | None:
    """Return the latest durable daemon record, optionally serving ``board``."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY id DESC",
            (DAEMON_RUN_TASK_ID,),
        ).fetchall()
    row = None
    payload: dict[str, Any] = {}
    for candidate in rows:
        try:
            candidate_payload = json.loads(candidate["result"] or "{}")
        except (TypeError, json.JSONDecodeError):
            candidate_payload = {}
        names = candidate_payload.get("boards")
        if not isinstance(names, list) or not names:
            names = [str(candidate_payload.get("board") or candidate["seat"])]
        if board is None or board in names:
            row = candidate
            payload = candidate_payload
            break
    if row is None:
        return None
    value = dict(row)
    names = payload.get("boards")
    if not isinstance(names, list) or not names:
        names = [str(payload.get("board") or value["seat"])]
    ticks = payload.get("last_tick_at")
    if not isinstance(ticks, dict):
        ticks = {names[0]: ticks}
    value["board"] = payload.get("board") or names[0]
    value["boards"] = names
    value["last_tick_at"] = {name: ticks.get(name) for name in names}
    value["tick_interval_seconds"] = float(payload.get("tick_interval_seconds") or 5.0)
    value["board_deprecation"] = payload.get("board_deprecation")
    return value


def get_policy(task_id) -> dict | None:
    """Return a task's execution policy, if present."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT policy_json FROM policies WHERE task_id=?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def set_policy(task_id, policy: dict) -> None:
    """Create or replace a task execution policy."""
    init_db()
    value = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    with _connect() as conn:
        conn.execute("INSERT INTO policies VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET policy_json=excluded.policy_json", (task_id, value))


def record_decision(task_id, stage_id, stage_type, seat, outcome, body) -> None:
    """Append an immutable policy-stage decision."""
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO decisions(task_id,stage_id,stage_type,seat,outcome,body,at) VALUES(?,?,?,?,?,?,?)",
                     (task_id, stage_id, stage_type, seat, outcome, body, _now()))


def decisions_for(task_id) -> list[dict]:
    """Return decisions for a task in insertion order."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute("SELECT task_id,stage_id,stage_type,seat,outcome,body,at FROM decisions WHERE task_id=? ORDER BY id", (task_id,)))


def add_monitor(
    task_id,
    next_check_at,
    timeout_at,
    max_attempts,
    recovery_policy,
    notes,
    scheduled_by,
    interval_seconds=300,
) -> None:
    """Create or replace a task monitor, resetting its attempts."""
    init_db()
    interval_seconds = int(interval_seconds)
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    with _connect() as conn:
        conn.execute("""INSERT INTO monitors(
          task_id,next_check_at,timeout_at,max_attempts,attempt_count,recovery_policy,notes,scheduled_by,interval_seconds
        ) VALUES(?,?,?,?,0,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
          next_check_at=excluded.next_check_at,timeout_at=excluded.timeout_at,max_attempts=excluded.max_attempts,
          attempt_count=0,recovery_policy=excluded.recovery_policy,notes=excluded.notes,
          scheduled_by=excluded.scheduled_by,interval_seconds=excluded.interval_seconds,
          state='active',last_outcome=NULL,last_error=NULL,last_checked_at=NULL""",
                     (task_id, next_check_at, timeout_at, max_attempts, recovery_policy, notes,
                      scheduled_by, interval_seconds))


def due_monitors(now_iso) -> list[dict]:
    """Return monitors whose next check or terminal timeout has arrived."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            """SELECT * FROM monitors
               WHERE state='active' AND (next_check_at<=? OR (timeout_at IS NOT NULL AND timeout_at<=?))
               ORDER BY next_check_at,task_id""",
            (now_iso, now_iso),
        ))


def advance_monitor(task_id, now_iso, *, close=False) -> bool:
    """Atomically advance one recovery attempt and reschedule or close it."""

    init_db()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT interval_seconds FROM monitors WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE monitors SET attempt_count=attempt_count+1 WHERE task_id=?", (task_id,)
        )
        if close:
            conn.execute("UPDATE monitors SET state='closed' WHERE task_id=?", (task_id,))
        else:
            now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            next_check_at = (now.astimezone(timezone.utc) + timedelta(
                seconds=int(row["interval_seconds"])
            )).isoformat()
            conn.execute(
                "UPDATE monitors SET next_check_at=? WHERE task_id=?",
                (next_check_at, task_id),
            )
        return True


def record_monitor_outcome(task_id: str, outcome: str, error: str | None = None) -> None:
    """Persist the latest bounded watchdog attempt outcome for operators."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE monitors SET last_outcome=?,last_error=?,last_checked_at=? WHERE task_id=?",
            (outcome, error, _now(), task_id),
        )


def clear_monitor(task_id) -> None:
    """Delete a task monitor."""
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM monitors WHERE task_id=?", (task_id,))


def add_watchdog(root_task_id, agent, instructions) -> None:
    """Create or update a subtree watchdog without losing its fingerprint."""
    init_db()
    with _connect() as conn:
        conn.execute("""INSERT INTO watchdogs(root_task_id,agent,instructions) VALUES(?,?,?)
          ON CONFLICT(root_task_id) DO UPDATE SET agent=excluded.agent,instructions=excluded.instructions""",
                     (root_task_id, agent, instructions))


def watchdogs() -> list[dict]:
    """Return all subtree watchdog definitions."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute("SELECT * FROM watchdogs ORDER BY root_task_id"))


def set_watchdog_fingerprint(root_task_id, fp) -> None:
    """Persist the last reviewed subtree fingerprint."""
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE watchdogs SET last_fingerprint=? WHERE root_task_id=?", (fp, root_task_id))


def seat_paused(seat) -> bool:
    """Return whether spawning is paused for a seat."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT paused FROM seat_state WHERE seat=?", (seat,)).fetchone()
    return bool(row[0]) if row else False


def set_seat_paused(seat, paused: bool) -> None:
    """Set a seat's durable spawning pause flag."""
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO seat_state VALUES(?,?) ON CONFLICT(seat) DO UPDATE SET paused=excluded.paused", (seat, int(bool(paused))))


def costs_rollup(by: str, since_days: int) -> list[dict]:
    """Aggregate completed run counts and token usage by seat, executor, or task."""
    columns = {"seat": "seat", "executor": "executor", "task": "task_id"}
    if by not in columns:
        raise ValueError("by must be seat, executor, or task")
    if int(since_days) < 0:
        raise ValueError("since_days must be non-negative")
    init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=int(since_days))).isoformat()
    column = columns[by]
    with _connect() as conn:
        return _rows(conn.execute(f"""SELECT {column} AS {by}, COUNT(*) AS runs,
          COALESCE(SUM(tokens_in),0) AS tokens_in, COALESCE(SUM(tokens_out),0) AS tokens_out,
          COALESCE(SUM(tokens_total),0) AS tokens_total,
          SUM(CASE WHEN tokens_total IS NULL THEN 1 ELSE 0 END) AS usage_unknown_runs,
          SUM(CASE WHEN tokens_total IS NULL THEN 0 ELSE 1 END) AS usage_known_runs,
          COALESCE(SUM(duration_s),0) AS duration_s
          FROM runs WHERE started_at>=? AND task_id<>?
          GROUP BY {column} ORDER BY {column}""", (since, DAEMON_RUN_TASK_ID)))


def reap_resource_leases(now: str | None = None) -> int:
    """Expire elapsed resource leases without deleting their audit rows."""
    init_db()
    now = now or _now()
    with _connect() as conn:
        return conn.execute(
            "UPDATE resource_leases SET state='expired',released_at=? "
            "WHERE state='active' AND lease_until IS NOT NULL AND lease_until<=?",
            (now, now),
        ).rowcount


def active_resource_units(kind: str, *, now: str | None = None) -> int:
    reap_resource_leases(now)
    with _connect() as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(units),0) FROM resource_leases WHERE kind=? AND state='active'",
            (kind,),
        ).fetchone()[0])


def available_resource_units(kind: str, capacity: int) -> int:
    """Return operator-configured capacity remaining after active leases."""
    return max(0, int(capacity) - active_resource_units(kind))


def acquire_resource_lease(kind: str, capacity: int, *, units: int = 1,
                           lease_seconds: int = 300, key: str | None = None,
                           instance_id: str | None = None, step_id: str | None = None,
                           activation: int | None = None,
                           metadata: dict[str, Any] | None = None) -> str | None:
    """Atomically acquire bounded capacity, or return ``None`` without spawning."""
    init_db()
    units, capacity = int(units), int(capacity)
    if units < 1 or capacity < 1:
        return None
    key = key or f"{kind}:{uuid.uuid4().hex}"
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=int(lease_seconds))).isoformat()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE resource_leases SET state='expired',released_at=? "
            "WHERE state='active' AND lease_until IS NOT NULL AND lease_until<=?",
            (now, now),
        )
        used = int(conn.execute(
            "SELECT COALESCE(SUM(units),0) FROM resource_leases WHERE kind=? AND state='active'",
            (kind,),
        ).fetchone()[0])
        existing = conn.execute(
            "SELECT state,units FROM resource_leases WHERE key=?", (key,),
        ).fetchone()
        if existing and existing["state"] == "active":
            conn.execute(
                "UPDATE resource_leases SET lease_until=?,metadata_json=? WHERE key=?",
                (lease_until, json.dumps(metadata or {}, sort_keys=True), key),
            )
            return key
        if used + units > capacity:
            return None
        if existing:
            conn.execute(
                "UPDATE resource_leases SET kind=?,units=?,instance_id=?,step_id=?,activation=?,"
                "state='active',lease_until=?,metadata_json=?,released_at=NULL WHERE key=?",
                (kind, units, instance_id, step_id, activation, lease_until,
                 json.dumps(metadata or {}, sort_keys=True), key),
            )
            return key
        conn.execute(
            "INSERT INTO resource_leases(key,kind,units,instance_id,step_id,activation,state,"
            "lease_until,metadata_json,created_at,released_at) VALUES(?,?,?,?,?,?,'active',?,?,?,NULL)",
            (key, kind, units, instance_id, step_id, activation, lease_until,
             json.dumps(metadata or {}, sort_keys=True), now),
        )
    return key


def renew_resource_lease(key: str, *, lease_seconds: int = 300) -> bool:
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=int(lease_seconds))).isoformat()
    with _connect() as conn:
        return conn.execute(
            "UPDATE resource_leases SET lease_until=? WHERE key=? AND state='active'",
            (lease_until, key),
        ).rowcount == 1


def release_resource_lease(key: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "UPDATE resource_leases SET state='released',released_at=?,lease_until=NULL "
            "WHERE key=? AND state='active'",
            (_now(), key),
        ).rowcount == 1


def admit_budget_charge(db: sqlite3.Connection, *, key: str, board: str, utc_day: str, instance_id: str,
                        step_id: str, activation: int, tokens: int,
                        ceiling: int, token_pool: str | None = None) -> bool:
    """Enforce the one configured board-day ceiling in the caller's transaction."""
    tokens, ceiling = int(tokens), int(ceiling)
    existing = db.execute(
        "SELECT 1 FROM budget_charges WHERE key=?", (key,)
    ).fetchone()
    if existing:
        return True
    daily = int(db.execute(
        "SELECT COALESCE(SUM(tokens),0) FROM budget_charges WHERE board=? AND utc_day=?",
        (board, utc_day),
    ).fetchone()[0])
    if daily + tokens > ceiling:
        return False
    db.execute(
        "INSERT INTO budget_charges(key,board,utc_day,instance_id,step_id,activation,tokens,created_at,token_pool) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            key, board, utc_day, instance_id, step_id, int(activation), tokens,
            _now(), token_pool,
        ),
    )
    return True


def sync_get(gh_number) -> dict | None:
    """Return the synchronization mapping for a GitHub issue."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sync WHERE gh_number=?", (gh_number,)).fetchone()
    return dict(row) if row else None


def sync_upsert(gh_number, task_id, gh_updated, k_updated) -> None:
    """Create or update a GitHub issue to kanban task mapping."""
    init_db()
    with _connect() as conn:
        conn.execute("""INSERT INTO sync VALUES(?,?,?,?,?) ON CONFLICT(gh_number) DO UPDATE SET
          task_id=excluded.task_id,gh_updated=excluded.gh_updated,k_updated=excluded.k_updated,last_synced_at=excluded.last_synced_at""",
                     (gh_number, task_id, gh_updated, k_updated, _now()))


__all__ = ["init_db", "record_run_start", "record_run_spawned", "record_run_end", "record_run_crashed", "nonterminal_runs", "run_row", "record_daemon_start", "record_daemon_tick", "record_daemon_end", "latest_daemon_run", "get_policy", "set_policy", "record_decision", "decisions_for", "add_monitor", "due_monitors", "advance_monitor", "record_monitor_outcome", "clear_monitor", "add_watchdog", "watchdogs", "set_watchdog_fingerprint", "seat_paused", "set_seat_paused", "costs_rollup", "reap_resource_leases", "active_resource_units", "available_resource_units", "acquire_resource_lease", "renew_resource_lease", "release_resource_lease", "admit_budget_charge", "sync_get", "sync_upsert"]
