"""SQLite persistence for Hermes Factory state."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "factory" / "factory.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
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
          recovery_policy TEXT NOT NULL, notes TEXT, scheduled_by TEXT);
        CREATE TABLE IF NOT EXISTS watchdogs (
          root_task_id TEXT PRIMARY KEY, agent TEXT NOT NULL, instructions TEXT NOT NULL, last_fingerprint TEXT);
        CREATE TABLE IF NOT EXISTS seat_state (seat TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sync (
          gh_number INTEGER PRIMARY KEY, task_id TEXT NOT NULL, gh_updated TEXT, k_updated TEXT, last_synced_at TEXT NOT NULL);
        """)


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


def add_monitor(task_id, next_check_at, timeout_at, max_attempts, recovery_policy, notes, scheduled_by) -> None:
    """Create or replace a task monitor, resetting its attempts."""
    init_db()
    with _connect() as conn:
        conn.execute("""INSERT INTO monitors VALUES(?,?,?,?,0,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
          next_check_at=excluded.next_check_at,timeout_at=excluded.timeout_at,max_attempts=excluded.max_attempts,
          attempt_count=0,recovery_policy=excluded.recovery_policy,notes=excluded.notes,scheduled_by=excluded.scheduled_by""",
                     (task_id, next_check_at, timeout_at, max_attempts, recovery_policy, notes, scheduled_by))


def due_monitors(now_iso) -> list[dict]:
    """Return monitors whose next check is at or before the UTC timestamp."""
    init_db()
    with _connect() as conn:
        return _rows(conn.execute("SELECT * FROM monitors WHERE next_check_at<=? ORDER BY next_check_at,task_id", (now_iso,)))


def bump_monitor(task_id) -> None:
    """Increment a monitor's recovery attempt count."""
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE monitors SET attempt_count=attempt_count+1 WHERE task_id=?", (task_id,))


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
          FROM runs WHERE started_at>=? GROUP BY {column} ORDER BY {column}""", (since,)))


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


__all__ = ["init_db", "record_run_start", "record_run_end", "get_policy", "set_policy", "record_decision", "decisions_for", "add_monitor", "due_monitors", "bump_monitor", "clear_monitor", "add_watchdog", "watchdogs", "set_watchdog_fingerprint", "seat_paused", "set_seat_paused", "costs_rollup", "sync_get", "sync_upsert"]
