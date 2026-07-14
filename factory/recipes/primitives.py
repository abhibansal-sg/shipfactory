"""The five v1 primitive activation rules and strict review verdict parsing."""
from __future__ import annotations

import json
import re
from typing import Any

from factory import store
from factory.policy import citation_ok
from .instantiate import task_key

_VERDICT = re.compile(r"^FACTORY_VERDICT:\s*(\{.*\})\s*$")

def parse_verdict(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    match = _VERDICT.fullmatch(lines[-1]) if lines else None
    if not match: raise ValueError("review final line must be FACTORY_VERDICT JSON")
    try: verdict = json.loads(match.group(1))
    except json.JSONDecodeError as exc: raise ValueError("invalid FACTORY_VERDICT JSON") from exc
    if not isinstance(verdict, dict) or verdict.get("outcome") not in {"approve", "request_changes"} or not isinstance(verdict.get("body"), str) or not citation_ok(verdict["body"]): raise ValueError("invalid review verdict")
    if verdict["outcome"] == "approve" and set(verdict) != {"outcome", "body"}: raise ValueError("approve verdict has unknown fields")
    if verdict["outcome"] == "request_changes" and (set(verdict) != {"outcome", "target_step", "body"} or not isinstance(verdict.get("target_step"), str)): raise ValueError("invalid request_changes verdict")
    return verdict

def activate(conn: Any, instance: dict[str, Any], recipe: dict[str, Any], step_def: dict[str, Any], step: dict[str, Any], parameters: dict[str, Any], parents: list[str], db: Any = None) -> str | None:
    """Perform one idempotent primitive mutation and return its task id if any."""
    from hermes_cli import kanban_db
    primitive, params = step_def["primitive"], step_def["params"]
    def render(value: Any) -> Any:
        if not isinstance(value, str): return value
        for name, val in parameters.items(): value = value.replace("${" + name + "}", "" if val is None else str(val))
        return value
    key = task_key(instance["id"], instance["recipe_hash"], step["step_id"], step["activation"])
    title, body = render(step_def["title"]), render(params.get("instructions", params.get("message", "")))
    if primitive in {"agent_task", "review_gate"}:
        # board= must be explicit: create_task's default_workdir inheritance
        # falls back to get_current_board() (the GLOBAL current board), which
        # poisons workspace_path with another board's workdir when the factory
        # board isn't current (shakedown finding #11).
        return kanban_db.create_task(conn, title=title, body=body, assignee=render(params["seat"]), workspace_kind=render(params["workspace"]), board=instance.get("board"), parents=parents, idempotency_key=key, max_runtime_seconds=int(params.get("max_runtime_seconds", 1800)), max_retries=int(params.get("max_retries", 2)))
    if primitive == "approval_gate":
        return kanban_db.create_blocked_task(conn, title=title, body=body, parents=parents, idempotency_key=key, board=instance.get("board"), block_kind="needs_input", reason="approval_required")
    if primitive == "wait_for_event":
        return kanban_db.create_blocked_task(conn, title=title, body=f"Waiting for event: {render(params['event'])}", parents=parents, idempotency_key=key, board=instance.get("board"), block_kind="needs_input", reason="event_wait")
    if primitive == "notify":
        # Reuse the caller's open factory-db handle when provided — opening a
        # second connection here deadlocks against reconcile()'s held write
        # txn on the same file (shakedown finding #17: 'database is locked').
        if db is not None:
            db.execute("INSERT OR IGNORE INTO outbox(key,target,message,state,attempts,next_attempt_at) VALUES(?,?,?,'pending',0,?)", (key, render(params["target"]), body, store._now()))
        else:
            with store._connect() as fresh:
                fresh.execute("INSERT OR IGNORE INTO outbox(key,target,message,state,attempts,next_attempt_at) VALUES(?,?,?,'pending',0,?)", (key, render(params["target"]), body, store._now()))
        return None
    raise RuntimeError(f"unknown primitive {primitive}")
