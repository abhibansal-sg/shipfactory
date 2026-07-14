"""SQLite persistence for Hermes Factory state."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DAEMON_RUN_TASK_ID = "__headframe_daemon__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "headframe" / "headframe.db"


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
    """Create all Factory tables idempotently."""
    with _connect() as conn:
        conn.executescript("""
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
        """)
        monitor_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(monitors)")
        }
        if "interval_seconds" not in monitor_columns:
            conn.execute(
                "ALTER TABLE monitors ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 300"
            )
        recipe_step_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(recipe_steps)")
        }
        if "finding_count" not in recipe_step_columns:
            conn.execute("ALTER TABLE recipe_steps ADD COLUMN finding_count INTEGER")


def record_run_start(task_id, seat, executor, model, pid) -> int:
    """Insert a running harness execution and return its run id."""
    init_db()
    with _connect() as conn:
        cur = conn.execute("INSERT INTO runs(task_id,seat,executor,model,pid,started_at) VALUES(?,?,?,?,?,?)",
                           (task_id, seat, executor, model or "", pid, _now()))
        return int(cur.lastrowid)


def record_run_end(run_id, exit_code, tokens_in, tokens_out, duration_s, result) -> None:
    """Finalize a harness execution with usage and outcome."""
    init_db()
    tokens_in, tokens_out = int(tokens_in or 0), int(tokens_out or 0)
    with _connect() as conn:
        conn.execute("UPDATE runs SET ended_at=?,exit_code=?,tokens_in=?,tokens_out=?,tokens_total=?,duration_s=?,result=? WHERE id=?",
                     (_now(), exit_code, tokens_in, tokens_out, tokens_in + tokens_out, duration_s, result, run_id))


def _daemon_payload(
    boards: list[str],
    last_tick_at: dict[str, str | None],
    *,
    tick_interval: float,
) -> dict[str, Any]:
    """Build the one-release-compatible daemon liveness payload."""
    return {
        "kind": "headframe_daemon",
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
    run_id = record_run_start(DAEMON_RUN_TASK_ID, names[0], "headframe-daemon", "", pid)
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
          scheduled_by=excluded.scheduled_by,interval_seconds=excluded.interval_seconds""",
                     (task_id, next_check_at, timeout_at, max_attempts, recovery_policy, notes,
                      scheduled_by, interval_seconds))


def due_monitors(now_iso) -> list[dict]:
    """Return monitors whose next check or terminal timeout has arrived."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute(
            """SELECT * FROM monitors
               WHERE next_check_at<=? OR (timeout_at IS NOT NULL AND timeout_at<=?)
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
            conn.execute("DELETE FROM monitors WHERE task_id=?", (task_id,))
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
          COALESCE(SUM(tokens_total),0) AS tokens_total, COALESCE(SUM(duration_s),0) AS duration_s
          FROM runs WHERE started_at>=? AND task_id<>?
          GROUP BY {column} ORDER BY {column}""", (since, DAEMON_RUN_TASK_ID)))


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


__all__ = ["init_db", "record_run_start", "record_run_end", "record_daemon_start", "record_daemon_tick", "record_daemon_end", "latest_daemon_run", "get_policy", "set_policy", "record_decision", "decisions_for", "add_monitor", "due_monitors", "advance_monitor", "clear_monitor", "add_watchdog", "watchdogs", "set_watchdog_fingerprint", "seat_paused", "set_seat_paused", "costs_rollup", "sync_get", "sync_upsert"]
