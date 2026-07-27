"""Factory dashboard API, mounted by Hermes below ``/api/plugins/shipfactory``."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

# Dashboard APIs are imported directly from ``dashboard/plugin_api.py`` by
# Hermes, unlike the normal plugin entry point.  Make the repository root
# importable so ``shipfactory`` resolves when this is an installed user plugin.
_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from shipfactory import store
from shipfactory.recipes import advancer
from shipfactory.recipes.loader import RecipeError


router = APIRouter()


class GateDecision(BaseModel):
    instance: str = Field(min_length=1)
    step: str = Field(min_length=1)
    activation: int = Field(ge=1)
    revision_hash: str = Field(min_length=1)
    evidence_bundle_hash: str | None
    nonce: str = Field(min_length=1)
    actor_kind: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    reason: str = ""


class PhoneTokenRequest(BaseModel):
    instance: str = Field(min_length=1)
    step: str = Field(min_length=1)
    activation: int = Field(ge=1)
    revision_hash: str = Field(min_length=1)
    evidence_bundle_hash: str | None
    decision: str = Field(pattern="^(approve|reject)$")
    nonce: str | None = None
    ttl_seconds: int = Field(default=600, ge=1, le=600)


class PhoneTokenDecision(BaseModel):
    token: str = Field(min_length=1)
    actor_kind: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    channel: str = Field(default="telegram", min_length=1)
    reason: str = ""


class InstantiateRecipe(BaseModel):
    recipe: str = Field(min_length=1)
    version: int = Field(ge=1)
    board: str = Field(min_length=1)
    parameters: dict[str, object] = Field(default_factory=dict)
    skip_steps: list[str] = Field(default_factory=list)


class TriageTask(BaseModel):
    title: str = Field(min_length=1)
    body: str = ""
    board: str = Field(min_length=1)


class RerouteRecipe(BaseModel):
    recipe: str = Field(min_length=1)
    version: int = Field(ge=1)
    parameters: dict[str, object] = Field(default_factory=dict)


class ProjectRecipePolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_recipe_keys: list[str]
    default_recipe_key: str | None = None


class ProjectFlightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe: str = Field(min_length=1)
    version: int = Field(ge=1)
    parameters: dict[str, object] = Field(default_factory=dict)
    skip_steps: list[str] = Field(default_factory=list)
    linear_issue_id: str | None = None
    idempotency_key: str = Field(min_length=1)


class SeatWrite(BaseModel):
    name: str | None = None
    profile: str | None = None
    executor: str | None = None
    model: str | None = None
    reasoning: str | None = None
    role: str | None = None
    max_concurrent: int | None = Field(default=None, ge=1)
    provider_config: dict[str, object] | None = None
    config: dict[str, object] | None = None
    skills: list[str] | None = None


def _latest_steps(db: Any, instance_id: str | None = None) -> list[dict[str, Any]]:
    scope = "WHERE instance_id=?" if instance_id else ""
    where = "WHERE s.instance_id=?" if instance_id else ""
    params: tuple[Any, ...] = (instance_id, instance_id) if instance_id else ()
    rows = db.execute(
        f"""
        SELECT s.* FROM recipe_steps AS s
        JOIN (
          SELECT instance_id, step_id, MAX(activation) AS activation
          FROM recipe_steps {scope} GROUP BY instance_id, step_id
        ) AS latest
          ON latest.instance_id=s.instance_id AND latest.step_id=s.step_id
         AND latest.activation=s.activation
        {where} ORDER BY s.step_id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _recipe_budgets_for(db: Any, instance: dict[str, Any]) -> dict[str, int | None]:
    row = db.execute(
        "SELECT normalized_yaml FROM recipe_versions WHERE id=? AND version=?",
        (instance["recipe_id"], instance["recipe_version"]),
    ).fetchone()
    budgets: dict[str, int | None] = {
        "max_activations": None,
        "max_step_activations": None,
        "max_tokens": None,
    }
    if row:
        try:
            raw = (yaml.safe_load(row["normalized_yaml"]) or {}).get("budgets", {})
            for key in budgets:
                value = raw.get(key)
                budgets[key] = int(value) if value is not None else None
        except (TypeError, ValueError, yaml.YAMLError):
            pass
    return budgets


def _recipe_step_order_for(db: Any, recipe_id: str, recipe_version: int) -> list[str]:
    row = db.execute(
        "SELECT normalized_yaml FROM recipe_versions WHERE id=? AND version=?",
        (recipe_id, recipe_version),
    ).fetchone()
    if not row:
        return []
    try:
        document = yaml.safe_load(row["normalized_yaml"]) or {}
        return [
            str(step["id"])
            for step in document.get("steps", [])
            if isinstance(step, dict) and step.get("id")
        ]
    except (TypeError, yaml.YAMLError):
        return []


def _budget_for(db: Any, instance: dict[str, Any]) -> dict[str, Any]:
    budget = _recipe_budgets_for(db, instance)["max_tokens"]
    charged = instance["tokens_charged"]
    return {"charged": charged, "budget": budget, "remaining": max(budget - charged, 0) if budget is not None else None}


def _instance_summary(
    db: Any, instance: dict[str, Any],
    order_cache: dict[tuple[str, int], list[str]] | None = None,
) -> dict[str, Any]:
    latest = _latest_steps(db, instance["id"])
    order_key = (instance["recipe_id"], instance["recipe_version"])
    if order_cache is not None and order_key in order_cache:
        recipe_order = order_cache[order_key]
    else:
        recipe_order = _recipe_step_order_for(db, *order_key)
        if order_cache is not None:
            order_cache[order_key] = recipe_order
    positions = {step_id: index + 1 for index, step_id in enumerate(recipe_order)}
    fallback_position = len(positions) + 1
    # Recipe order when the pinned version is readable; the pre-existing
    # alphabetical order remains the fallback for missing/unparsable rows.
    latest.sort(
        key=lambda step: (
            positions.get(step["step_id"], fallback_position), step["step_id"],
        )
    )
    states: dict[str, int] = defaultdict(int)
    for step in latest:
        step["step_position"] = positions.get(step["step_id"])
        states[step["state"]] += 1
    return {
        **instance,
        "recipe": f"{instance['recipe_id']}@{instance['recipe_version']}",
        "latest_steps": latest,
        "step_states": dict(states),
        "tokens": _budget_for(db, instance),
        "budgets": _recipe_budgets_for(db, instance),
    }


def _gate_or_400(instance_id: str, step_id: str) -> None:
    with store._connect() as db:
        instance = db.execute("SELECT 1 FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        step = db.execute(
            "SELECT primitive,state FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
            (instance_id, step_id),
        ).fetchone()
    if not instance or not step or step["primitive"] != "approval_gate" or step["state"] != "waiting":
        raise HTTPException(status_code=400, detail="approval gate is not waiting")


def _recipe_config() -> tuple[Any, dict[str, Any]]:
    from shipfactory.config import load_seats

    config = load_seats()
    recipes = config.recipes or {}
    path = recipes.get("library_path")
    if not path:
        raise ValueError("recipes.library_path is not configured")
    return config, recipes


def _library(*, persist: bool = True) -> Any:
    from shipfactory.recipes.loader import load_library

    config, recipes = _recipe_config()
    return load_library(
        recipes["library_path"],
        seats=set(config.seats),
        profiles=set((recipes.get("execution_profiles") or {}).keys()),
        persist=persist,
    )


def _instance_board(instance_id: str) -> str:
    store.init_db()
    with store._connect() as db:
        row = db.execute(
            "SELECT board FROM recipe_instances WHERE id=?", (instance_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown recipe instance")
    return str(row["board"])


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _request_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


class _ProjectAPIError(Exception):
    def __init__(self, status_code: int, error: str, message: str, field: str | None = None):
        self.status_code = status_code
        self.error = error
        self.message = message
        self.field = field


def _project_error(
    status_code: int, error: str, message: str, field: str | None = None,
) -> None:
    raise _ProjectAPIError(status_code, error, message, field)


def _project_error_response(exc: _ProjectAPIError) -> JSONResponse:
    payload = {"error": exc.error, "message": exc.message}
    if exc.field is not None:
        payload["field"] = exc.field
    return JSONResponse(status_code=exc.status_code, content=payload)


def _fresh_projects_runtime_config() -> tuple[Any, dict[str, Any]]:
    """Load the single operator config source at every Projects request."""
    from shipfactory.config import load_seats, projects_visual_recipes_config

    config = load_seats()
    return config, projects_visual_recipes_config(config.recipes)


def _project_value(project: Any, name: str, default: Any = None) -> Any:
    if isinstance(project, dict):
        return project.get(name, default)
    return getattr(project, name, default)


def _project_registry(project_id: str | None = None) -> tuple[Any, list[Any]]:
    """Read Hermes projects and their explicit ``board_slug`` live."""
    from hermes_cli import projects_db

    with projects_db.connect_closing() as conn:
        projects = list(projects_db.list_projects(conn))
        if project_id is None:
            return None, projects
        project = projects_db.get_project(conn, project_id)
    return project, projects


def _project_binding(project: Any, projects: list[Any]) -> str:
    board_slug = _project_value(project, "board_slug")
    if not isinstance(board_slug, str) or not board_slug.strip():
        return "unbound"
    matches = [item for item in projects if _project_value(item, "board_slug") == board_slug]
    return "bound" if len(matches) == 1 and _project_value(matches[0], "id") == _project_value(project, "id") else "ambiguous"


def resolve_hermes_project(project_id: str) -> dict[str, Any]:
    """Return a live Hermes project projection without persisting a mapping."""
    project, projects = _project_registry(project_id)
    if project is None:
        _project_error(404, "project_not_found", "unknown Hermes project")
    return {
        "project": project,
        "projects": projects,
        "binding": _project_binding(project, projects),
        "board_slug": _project_value(project, "board_slug"),
    }


def _project_summary(project: Any, binding: str, policy: dict[str, Any] | None,
                     rollup: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _project_value(project, "id"),
        "slug": _project_value(project, "slug"),
        "name": _project_value(project, "name"),
        "binding": binding,
        "recipes": {
            "allowed": list(policy["allowed_recipe_keys"] if policy else []),
            "default": policy["default_recipe_key"] if policy else None,
        },
        "rollup": rollup,
    }


def _rollup_rows(rows: list[dict[str, Any]], recent_limit: int) -> dict[str, Any]:
    waiting_states = {"waiting_gate", "waiting_event"}
    terminal_states = {"done", "failed", "cancelled", *waiting_states}
    counts = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1
    recent = sorted(
        rows,
        key=lambda row: (str(row.get("updated_at") or ""), str(row.get("id") or "")),
        reverse=True,
    )[:recent_limit]
    return {
        "active": sum(count for state, count in counts.items() if state not in terminal_states),
        "waiting": sum(count for state, count in counts.items() if state in waiting_states),
        "recent": [
            {
                "instance_id": row["id"],
                "recipe": f"{row['recipe_id']}@{row['recipe_version']}",
                "status": row["status"],
                "updated_at": row["updated_at"],
                "linear_issue_id": row.get("linear_issue_id"),
            }
            for row in recent
        ],
    }


def _project_rollups(db: Any, projects: list[Any], recent_limit: int) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    rows = [dict(row) for row in db.execute(
        "SELECT id,board,project_id,recipe_id,recipe_version,status,updated_at,linear_issue_id "
        "FROM recipe_instances"
    ).fetchall()]
    by_board: dict[str, list[Any]] = defaultdict(list)
    for project in projects:
        board_slug = _project_value(project, "board_slug")
        if isinstance(board_slug, str) and board_slug.strip():
            by_board[board_slug].append(project)
    buckets: dict[str, list[dict[str, Any]]] = {str(_project_value(p, "id")): [] for p in projects}
    unclassified: list[dict[str, Any]] = []
    for row in rows:
        candidates = by_board.get(row.get("board"), [])
        owner = candidates[0] if len(candidates) == 1 else None
        if owner is not None and row.get("project_id") not in {None, _project_value(owner, "id")}:
            owner = None
        if owner is None:
            unclassified.append(row)
        else:
            buckets[str(_project_value(owner, "id"))].append(row)
    return (
        {project_id: _rollup_rows(items, recent_limit) for project_id, items in buckets.items()},
        _rollup_rows(unclassified, recent_limit),
    )


def _policy_for(db: Any, project_id: str) -> dict[str, Any] | None:
    try:
        return store.load_project_recipe_policy(db, project_id)
    except (TypeError, ValueError) as exc:
        _project_error(400, "invalid_policy", str(exc))


def _library_for_config(config: Any, *, persist: bool) -> Any:
    from shipfactory.recipes.loader import load_library

    recipes = config.recipes or {}
    path = recipes.get("library_path")
    if not path:
        _project_error(400, "invalid_recipe_library", "recipes.library_path is not configured")
    try:
        return load_library(
            path,
            seats=set(config.seats),
            profiles=set((recipes.get("execution_profiles") or {}).keys()),
            persist=persist,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        _project_error(400, "invalid_recipe_library", str(exc))


def _require_project_flag(runtime: dict[str, Any], flag: str) -> None:
    if not runtime.get("enabled") or not runtime.get(flag):
        _project_error(403, "feature_disabled", f"Projects feature {flag} is disabled")


def _recipe_summary(recipe: Any, default_key: str | None) -> dict[str, Any]:
    document = recipe.document
    budgets = document.get("budgets") or {}
    caps = budgets.get("step_activation_caps")
    steps = []
    for step in document.get("steps", []):
        params = step.get("params") or {}
        steps.append({
            "id": step["id"],
            "title": step["title"],
            "primitive": step["primitive"],
            "needs": step["needs"],
            "optional": step["optional"],
            "seat": params.get("seat"),
            "execution_profile": params.get("execution_profile"),
            "access_mode": params.get("access_mode"),
            "environment": params.get("environment"),
            "instructions": params.get("instructions"),
            "inputs": step.get("inputs", []),
            "outputs": step.get("outputs", []),
            "activation_cap": caps.get(step["id"]) if isinstance(caps, dict) else None,
        })
    return {
        "key": recipe.key,
        "id": document["id"],
        "version": document["version"],
        "status": document["status"],
        "recipe_hash": recipe.hash,
        "description": document["description"],
        "parameters": document["parameters"],
        "budgets": {
            "max_activations": budgets.get("max_activations"),
            "step_activation_caps": caps,
        },
        "steps": steps,
        "optional_steps": [
            {"id": step["id"], "title": step["title"]}
            for step in document.get("steps", []) if step["optional"]
        ],
        "default": recipe.key == default_key,
    }


def _validate_project_policy(policy: dict[str, Any] | None, library: Any) -> None:
    if policy is None:
        return
    allowed = policy["allowed_recipe_keys"]
    default = policy["default_recipe_key"]
    if allowed and default is None:
        _project_error(
            400, "invalid_policy",
            "a non-empty recipe policy requires a default recipe",
            "default_recipe_key",
        )
    if default is not None and default not in allowed:
        _project_error(
            400, "invalid_policy", "default_recipe_key must be allowed",
            "default_recipe_key",
        )
    for key in allowed:
        try:
            recipe = library.get(key)
        except RecipeError as exc:
            _project_error(400, "invalid_policy", str(exc), "allowed_recipe_keys")
        if recipe.document.get("status") != "active":
            _project_error(
                400, "invalid_policy", f"recipe {key!r} is not active",
                "allowed_recipe_keys",
            )


def _row_skip_steps(db: Any, instance_id: str) -> list[str]:
    return sorted(
        str(row[0]) for row in db.execute(
            "SELECT step_id FROM recipe_steps WHERE instance_id=? AND state='skipped'",
            (instance_id,),
        ).fetchall()
    )


def _flight_fingerprint(project_id: str, recipe: Any, parameters: dict[str, Any],
                        skip_steps: list[str], linear_issue_id: str | None,
                        idempotency_key: str) -> str:
    payload = {
        "project_id": project_id,
        "recipe": recipe.key,
        "recipe_hash": recipe.hash,
        "parameters": parameters,
        "skip_steps": sorted(skip_steps),
        "linear_issue_id": linear_issue_id,
        "idempotency_key": idempotency_key,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _flight_matches(db: Any, row: dict[str, Any], project_id: str, recipe: Any,
                    parameters: dict[str, Any], skip_steps: list[str],
                    linear_issue_id: str | None, idempotency_key: str) -> bool:
    try:
        stored_parameters = json.loads(row["parameters_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    try:
        version_matches = int(row.get("recipe_version")) == int(recipe.document["version"])
    except (TypeError, ValueError):
        version_matches = False
    return (
        row.get("project_id") == project_id
        and row.get("launch_idempotency_key") == idempotency_key
        and row.get("linear_issue_id") == linear_issue_id
        and row.get("recipe_id") == recipe.document["id"]
        and version_matches
        and row.get("recipe_hash") == recipe.hash
        and stored_parameters == parameters
        and _row_skip_steps(db, row["id"]) == sorted(skip_steps)
    )


def _flight_identity_rows(
    db: Any, project_id: str, linear_issue_id: str | None,
    idempotency_key: str,
) -> list[dict[str, Any]]:
    """Probe both durable identity fences, preserving one row per identity."""
    rows: list[dict[str, Any]] = []
    for row in (
        store.project_flight_by_idempotency_key(db, project_id, idempotency_key),
        store.project_flight_by_linear_issue_id(db, linear_issue_id)
        if linear_issue_id is not None else None,
    ):
        if row is not None and all(row["id"] != existing["id"] for existing in rows):
            rows.append(row)
    return rows


def _replay_or_conflict(
    db: Any, project_id: str, recipe: Any, parameters: dict[str, Any],
    skip_steps: list[str], linear_issue_id: str | None, idempotency_key: str,
) -> tuple[dict[str, Any], int] | None:
    rows = _flight_identity_rows(db, project_id, linear_issue_id, idempotency_key)
    if not rows:
        return None
    if len(rows) != 1 or not _flight_matches(
        db, rows[0], project_id, recipe, parameters, skip_steps,
        linear_issue_id, idempotency_key,
    ):
        _project_error(
            409, "idempotency_conflict", "launch identity is already used",
            "idempotency_key",
        )
    return _flight_response(rows[0], skip_steps), 200


def _flight_response(row: dict[str, Any], skip_steps: list[str] | None = None) -> dict[str, Any]:
    try:
        parameters = json.loads(row["parameters_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        _project_error(409, "invalid_flight", "stored flight parameters are invalid")
    if skip_steps is None:
        skip_steps = []
    return {
        "instance_id": row["id"],
        "project_id": row["project_id"],
        "recipe": f"{row['recipe_id']}@{row['recipe_version']}",
        "recipe_hash": row["recipe_hash"],
        "parameters": parameters,
        "skip_steps": sorted(skip_steps),
        "linear_issue_id": row.get("linear_issue_id"),
        "idempotency_key": row["launch_idempotency_key"],
        "linear_backlink": {
            "status": "unavailable",
            "issue_id": row.get("linear_issue_id"),
            "reason": "in-product backlink writer deferred",
        },
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _launch_project_flight(project_id: str, request: ProjectFlightRequest) -> tuple[dict[str, Any], int]:
    config, runtime = _fresh_projects_runtime_config()
    _require_project_flag(runtime, "launch_enabled")
    issue_id = request.linear_issue_id
    if not request.idempotency_key.strip():
        _project_error(400, "invalid_request", "idempotency_key must be non-empty", "idempotency_key")
    if issue_id is not None and not issue_id.strip():
        _project_error(400, "invalid_request", "linear_issue_id must be non-empty", "linear_issue_id")
    library = _library_for_config(config, persist=True)
    recipe_key = f"{request.recipe}@{request.version}"
    from shipfactory.recipes.loader import RecipeError, bind_parameters

    try:
        recipe = library.get(recipe_key)
    except RecipeError as exc:
        _project_error(404, "recipe_not_found", str(exc))
    if recipe.document.get("status") != "active":
        _project_error(400, "invalid_policy", "only active recipes may be launched")
    try:
        bound = bind_parameters(recipe, dict(request.parameters), list(request.skip_steps))
    except (TypeError, ValueError) as exc:
        _project_error(400, "invalid_parameters", str(exc))
    skips = sorted(set(request.skip_steps))

    # Probe both durable identity fences before reading the current project
    # binding or policy.  A replay is an old flight and must retain its captured
    # board/recipe/hash even after an operator changes Hermes or Factory policy.
    store.init_db()
    with store._connect() as db:
        replay = _replay_or_conflict(
            db, project_id, recipe, bound, skips, issue_id, request.idempotency_key,
        )
        if replay is not None:
            return replay

    projection = resolve_hermes_project(project_id)
    if projection["binding"] != "bound":
        _project_error(409, "project_binding_unavailable", "project has no unique active board binding")
    board = projection["board_slug"]
    if not isinstance(board, str) or not board.strip():
        _project_error(409, "project_binding_unavailable", "project has no board binding")
    with store._connect() as db:
        policy = _policy_for(db, project_id)
        allowed = policy["allowed_recipe_keys"] if policy else []
        if recipe_key not in allowed:
            _project_error(400, "recipe_not_allowed", "recipe is not attached to this project")
    instance_id = "flight-" + _flight_fingerprint(
        project_id, recipe, bound, skips, issue_id, request.idempotency_key,
    )
    from hermes_cli import kanban_db
    from shipfactory.recipes.instantiate import instantiate

    conn = kanban_db.connect(board=board)
    try:
        instantiate(
            conn,
            board=board,
            recipe=recipe,
            parameters=dict(request.parameters),
            skip_steps=skips,
            instance_id=instance_id,
            project_id=project_id,
            linear_issue_id=issue_id,
            launch_idempotency_key=request.idempotency_key,
        )
    except sqlite3.IntegrityError:
        with store._connect() as db:
            replay = _replay_or_conflict(
                db, project_id, recipe, bound, skips, issue_id,
                request.idempotency_key,
            )
            if replay is not None:
                return replay
        _project_error(
            409, "idempotency_conflict", "launch identity is already used",
            "idempotency_key",
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        _project_error(400, "launch_failed", str(exc))
    finally:
        conn.close()
    with store._connect() as db:
        row = store.project_flight(db, instance_id)
        if row is None:
            _project_error(409, "launch_incomplete", "flight identity was not persisted")
        return _flight_response(row, skips), 201


def launch_project_flight(project_id: str, request: ProjectFlightRequest) -> dict[str, Any]:
    return _launch_project_flight(project_id, request)[0]


def _cancel_preview(instance_id: str, board: str) -> dict[str, Any]:
    from shipfactory.spawn import _RUNNING
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        report = advancer.cancel(conn, instance_id, dry_run=True)
        suppressed = set(report.get("suppressed") or [])
        workers_by_task = {
            record["task_id"]: {
                "task_id": record["task_id"],
                "pid": record["proc"].pid,
                "executor": record.get("executor"),
            }
            for record in _RUNNING.values()
            if record.get("task_id") in suppressed
        }
        if suppressed:
            placeholders = ",".join("?" for _ in suppressed)
            rows = conn.execute(
                f"SELECT id,worker_pid,assignee FROM tasks "
                f"WHERE id IN ({placeholders}) AND worker_pid IS NOT NULL",
                tuple(suppressed),
            ).fetchall()
            for row in rows:
                workers_by_task.setdefault(
                    row["id"],
                    {
                        "task_id": row["id"],
                        "pid": row["worker_pid"],
                        "executor": row["assignee"],
                    },
                )
        report["workers"] = list(workers_by_task.values())
        return report
    finally:
        conn.close()


@router.get("/projects", response_model=None)
def list_projects() -> dict[str, Any] | JSONResponse:
    try:
        config, runtime = _fresh_projects_runtime_config()
        _require_project_flag(runtime, "enabled")
        project, projects = _project_registry()
        del project
        store.init_db()
        with store._connect() as db:
            rollups, unclassified_rollup = _project_rollups(
                db, projects, int(runtime["recent_flight_limit"]),
            )
            summaries = []
            for item in projects:
                project_id = str(_project_value(item, "id"))
                policy = _policy_for(db, project_id)
                summaries.append(_project_summary(
                    item, _project_binding(item, projects), policy, rollups[project_id],
                ))
        return {
            "projects": summaries,
            "runtime_config": runtime,
            "unclassified": {
                "id": "unclassified",
                "label": "Unclassified",
                "binding": "unclassified",
                "rollup": unclassified_rollup,
            },
        }
    except _ProjectAPIError as exc:
        return _project_error_response(exc)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        return _project_error_response(_ProjectAPIError(400, "projects_unavailable", str(exc)))


@router.get("/projects/{project_id}/recipes", response_model=None)
def project_recipes(project_id: str) -> dict[str, Any] | JSONResponse:
    try:
        config, runtime = _fresh_projects_runtime_config()
        _require_project_flag(runtime, "enabled")
        projection = resolve_hermes_project(project_id)
        if projection["binding"] != "bound":
            _project_error(409, "project_binding_unavailable", "project has no unique active board binding")
        library = _library_for_config(config, persist=False)
        store.init_db()
        with store._connect() as db:
            policy = _policy_for(db, project_id)
        allowed = policy["allowed_recipe_keys"] if policy else []
        _validate_project_policy(policy, library)
        recipes = [
            _recipe_summary(library.get(key), policy["default_recipe_key"] if policy else None)
            for key in allowed
        ]
        return {
            "project_id": project_id,
            "recipes": recipes,
            "default_recipe": policy["default_recipe_key"] if policy else None,
        }
    except _ProjectAPIError as exc:
        return _project_error_response(exc)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        return _project_error_response(_ProjectAPIError(400, "projects_unavailable", str(exc)))


@router.put("/projects/{project_id}/recipe-policy", response_model=None)
def update_project_recipe_policy(
    project_id: str, request: ProjectRecipePolicyWrite,
) -> dict[str, Any] | JSONResponse:
    try:
        config, runtime = _fresh_projects_runtime_config()
        _require_project_flag(runtime, "policy_editing_enabled")
        project, _ = _project_registry(project_id)
        if project is None:
            _project_error(404, "project_not_found", "unknown Hermes project")
        library = _library_for_config(config, persist=False)
        requested_policy = {
            "allowed_recipe_keys": request.allowed_recipe_keys,
            "default_recipe_key": request.default_recipe_key,
        }
        _validate_project_policy(requested_policy, library)
        store.init_db()
        with store._connect() as db:
            try:
                return store.save_project_recipe_policy(
                    db, project_id, request.allowed_recipe_keys, request.default_recipe_key,
                )
            except (TypeError, ValueError) as exc:
                _project_error(400, "invalid_policy", str(exc))
    except _ProjectAPIError as exc:
        return _project_error_response(exc)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        return _project_error_response(_ProjectAPIError(400, "projects_unavailable", str(exc)))


@router.post("/projects/{project_id}/flights", status_code=201, response_model=None)
def create_project_flight(
    project_id: str, request: ProjectFlightRequest,
) -> dict[str, Any] | JSONResponse:
    try:
        response, status_code = _launch_project_flight(project_id, request)
        if status_code == 201:
            return response
        return JSONResponse(status_code=status_code, content=response)
    except _ProjectAPIError as exc:
        return _project_error_response(exc)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        return _project_error_response(_ProjectAPIError(400, "launch_failed", str(exc)))


@router.get("/recipes")
def list_recipes() -> list[dict[str, Any]]:
    """Describe the configured recipe library without creating API-owned state."""
    try:
        library = _library(persist=False)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise _request_error(exc) from exc
    items = []
    for recipe in library.recipes.values():
        document = recipe.document
        budgets = document["budgets"]
        steps = []
        for step in document["steps"]:
            params = step.get("params", {})
            item = {
                "id": step["id"],
                "title": step["title"],
                "primitive": step["primitive"],
                "seat": params.get("seat"),
                "needs": step["needs"],
                "optional": step["optional"],
                "instructions": params.get("instructions"),
            }
            if "execution_profile" in params:
                item["execution_profile"] = params["execution_profile"]
            steps.append(item)
        items.append({
            "id": document["id"],
            "version": document["version"],
            "status": document["status"],
            "description": document["description"],
            "parameters": document["parameters"],
            "budgets": {
                "max_activations": budgets.get("max_activations"),
                "step_activation_caps": budgets.get("step_activation_caps"),
            },
            "steps": steps,
            "optional_steps": [
                {"id": step["id"], "title": step["title"]}
                for step in document["steps"] if step["optional"]
            ],
        })
    return sorted(items, key=lambda item: (item["id"], item["version"]))


@router.post("/instances")
def create_instance(request: InstantiateRecipe) -> dict[str, Any]:
    from shipfactory.recipes.instantiate import instantiate
    from hermes_cli import kanban_db

    conn = None
    try:
        recipe = _library().get(f"{request.recipe}@{request.version}")
        conn = kanban_db.connect(board=request.board)
        return instantiate(
            conn,
            board=request.board,
            recipe=recipe,
            parameters=request.parameters,
            skip_steps=request.skip_steps,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise _request_error(exc) from exc
    finally:
        if conn is not None:
            conn.close()


@router.post("/triage")
def create_triage_task(request: TriageTask) -> dict[str, Any]:
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=request.board)
    try:
        task_id = kanban_db.create_task(
            conn, title=request.title, body=request.body, triage=True,
            board=request.board,
        )
        task = kanban_db.get_task(conn, task_id)
        return {"task_id": task_id, "status": task.status, "board": request.board}
    except ValueError as exc:
        raise _request_error(exc) from exc
    finally:
        conn.close()


@router.post("/instances/{instance_id}/reroute")
def reroute_instance(instance_id: str, request: RerouteRecipe) -> dict[str, Any]:
    from shipfactory.cli import _reroute
    from hermes_cli import kanban_db

    board = _instance_board(instance_id)
    conn = kanban_db.connect(board=board)
    try:
        _, recipes = _recipe_config()
        args = argparse.Namespace(
            instance=instance_id,
            recipe=f"{request.recipe}@{request.version}",
            parameters=json.dumps(request.parameters),
            library=recipes["library_path"],
            board=board,
        )
        return _reroute(conn, args)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise _request_error(exc) from exc
    finally:
        conn.close()


@router.get("/instances/{instance_id}/cancel")
def cancel_instance_preview(instance_id: str) -> dict[str, Any]:
    board = _instance_board(instance_id)
    try:
        return _cancel_preview(instance_id, board)
    except ValueError as exc:
        raise _request_error(exc) from exc


@router.post("/instances/{instance_id}/cancel")
def cancel_instance(instance_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db

    board = _instance_board(instance_id)
    conn = kanban_db.connect(board=board)
    try:
        return advancer.cancel(conn, instance_id)
    except ValueError as exc:
        raise _request_error(exc) from exc
    finally:
        conn.close()


@router.get("/status")
def shipfactory_status() -> dict[str, Any]:
    try:
        _, recipes = _recipe_config()
    except (FileNotFoundError, OSError, ValueError):
        recipes = {}
    record = store.latest_daemon_run()
    running = bool(
        record and record.get("ended_at") is None and _pid_alive(record.get("pid"))
    )
    names = list(record.get("boards") or []) if record else []
    ticks = record.get("last_tick_at") if record else {}
    if not isinstance(ticks, dict):
        ticks = {names[0]: ticks} if names else {}
    interval = float(record.get("tick_interval_seconds") or 5.0) if record else 5.0
    now = datetime.now(timezone.utc)
    board_status = []
    for name in names:
        ticked_at = ticks.get(name)
        age = None
        if ticked_at:
            try:
                stamp = datetime.fromisoformat(str(ticked_at).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age = max(0.0, (now - stamp).total_seconds())
            except (TypeError, ValueError):
                age = None
        board_status.append({
            "board": name,
            "last_tick_at": ticked_at,
            "last_tick_age_seconds": age,
            "stale": age is None or age > 3 * interval,
        })
    first_board = names[0] if names else None
    return {
        "running": running,
        "pid": record.get("pid") if running and record else None,
        # One-release compatibility for readers expecting a single board/tick.
        "board": first_board,
        "last_tick_at": ticks.get(first_board) if first_board else None,
        "boards": board_status,
        "tick_interval_seconds": interval,
        "config": {
            "recipes_enabled": bool(recipes.get("enabled")),
            "library_path": recipes.get("library_path"),
            "bare_task_recipe": recipes.get("bare_task_recipe"),
        },
    }


@router.get("/instances")
def list_instances() -> list[dict[str, Any]]:
    store.init_db()
    with store._connect() as db:
        rows = db.execute("SELECT * FROM recipe_instances ORDER BY updated_at DESC, id DESC").fetchall()
        # Memoized per (recipe_id, recipe_version) within this request only.
        order_cache: dict[tuple[str, int], list[str]] = {}
        return [_instance_summary(db, dict(row), order_cache) for row in rows]


@router.get("/instances/{instance_id}")
def get_instance(instance_id: str) -> dict[str, Any]:
    store.init_db()
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="unknown recipe instance")
        instance = _instance_summary(db, dict(row))
        steps = [dict(item) for item in db.execute("SELECT * FROM recipe_steps WHERE instance_id=? ORDER BY step_id,activation", (instance_id,)).fetchall()]
        recipe_order = _recipe_step_order_for(
            db, instance["recipe_id"], instance["recipe_version"]
        )
        positions = {step_id: index + 1 for index, step_id in enumerate(recipe_order)}
        fallback_position = len(positions) + 1
        steps.sort(
            key=lambda step: (
                positions.get(step["step_id"], fallback_position),
                step["step_id"],
                step["activation"],
            )
        )
        for step in steps:
            step["step_position"] = positions.get(step["step_id"])
        activations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        task_ids: list[str] = []
        for step in steps:
            activations[step["step_id"]].append(step)
            if step["kanban_task_id"]:
                task_ids.append(step["kanban_task_id"])
        decisions: list[dict[str, Any]] = []
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            decisions = [dict(item) for item in db.execute(f"SELECT * FROM decisions WHERE task_id IN ({placeholders}) ORDER BY at DESC,id DESC", task_ids).fetchall()]
        bound_decisions = [dict(item) for item in db.execute(
            "SELECT * FROM gate_decisions WHERE instance_id=? ORDER BY created_at DESC,id DESC",
            (instance_id,),
        ).fetchall()]
        story = None
        waiting_approval = db.execute(
            "SELECT step_id FROM recipe_steps WHERE instance_id=? "
            "AND primitive='approval_gate' AND state='waiting' "
            "ORDER BY activation DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
        story_hash = None
        if waiting_approval is not None:
            try:
                from shipfactory.decisions import current_binding
                story_hash = current_binding(
                    db, instance_id, waiting_approval["step_id"],
                ).get("review_story_sha256")
            except Exception:
                # A stale or unverifiable approval binding must not fall back
                # to an unrelated instance-wide "latest" story.
                story_hash = None
        story_row = db.execute(
            "SELECT * FROM artifacts WHERE instance_id=? AND kind='review-story' "
            "AND state='sealed' AND sha256=?",
            (instance_id, story_hash),
        ).fetchone() if story_hash else None
        if story_row is not None:
            from shipfactory.artifacts import artifact_document, dashboard_safe_review_story
            story = dashboard_safe_review_story(artifact_document(dict(story_row)))
        instance.update({
            "steps": steps,
            "activations": dict(activations),
            "blocked_reasons": [{"step": step["step_id"], "activation": step["activation"], "reason": step["blocked_reason"]} for step in steps if step["blocked_reason"]],
            "decisions": decisions,
            "gate_decisions": bound_decisions,
            "review_story": story,
        })
        return instance


_RECEIPT_FILE_CAP_BYTES = 256 * 1024


@router.get("/instances/{instance_id}/receipts")
def instance_receipts(instance_id: str) -> list[dict[str, Any]]:
    """Per-attempt harness execution receipts for one instance (Amendment C).

    Raw filesystem paths never enter the payload; callers fetch content by
    ``run_id`` through the log/prompt endpoints below.
    """
    store.init_db()
    with store._connect() as db:
        if not db.execute(
            "SELECT 1 FROM recipe_instances WHERE id=?", (instance_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="unknown recipe instance")
        rows = db.execute(
            """
            SELECT s.step_id, s.activation, s.kanban_task_id, r.id AS run_id,
                   r.seat, r.executor, r.provider, r.resolved_model, r.model,
                   r.started_at, r.ended_at, r.exit_code, r.result,
                   r.tokens_in, r.tokens_out, r.tokens_total, r.duration_s,
                   r.log_path, r.prompt_path, r.workspace_path,
                   r.access_enforcement_level
            FROM recipe_steps s
            JOIN runs r ON r.task_id = s.kanban_task_id
             AND (r.recipe_activation IS NULL OR r.recipe_activation = s.activation)
            WHERE s.instance_id = ?
            ORDER BY s.step_id, s.activation, r.id
            """,
            (instance_id,),
        ).fetchall()
    receipts = []
    for row in rows:
        receipt = dict(row)
        receipt["has_log"] = receipt.pop("log_path") is not None
        receipt["has_prompt"] = receipt.pop("prompt_path") is not None
        receipt.pop("workspace_path")
        receipts.append(receipt)
    return receipts


def _run_file(run_id: int, kind: str) -> dict[str, Any]:
    """Serve one run's log/prompt tail from the path recorded in the runs row.

    The path comes exclusively from shipfactory.db (never the client), and only
    runs belonging to a recipe step activation are served.
    """
    store.init_db()
    row = store.run_row(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown run")
    with store._connect() as db:
        step = db.execute(
            "SELECT 1 FROM recipe_steps WHERE kanban_task_id=?", (row["task_id"],)
        ).fetchone()
    if step is None:
        raise HTTPException(status_code=404, detail="run is not a recipe step execution")
    path = row.get("log_path" if kind == "log" else "prompt_path")
    if not path:
        raise HTTPException(status_code=404, detail=f"run has no recorded {kind}")
    try:
        size = Path(path).stat().st_size
        with Path(path).open("rb") as handle:
            if size > _RECEIPT_FILE_CAP_BYTES:
                handle.seek(size - _RECEIPT_FILE_CAP_BYTES)
            data = handle.read(_RECEIPT_FILE_CAP_BYTES)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"run {kind} file is unavailable") from exc
    return {
        "run_id": int(run_id),
        "kind": kind,
        "content": data.decode("utf-8", errors="replace"),
        "truncated": size > _RECEIPT_FILE_CAP_BYTES,
    }


@router.get("/runs/{run_id}/log")
def run_log(run_id: int) -> dict[str, Any]:
    return _run_file(run_id, "log")


@router.get("/runs/{run_id}/prompt")
def run_prompt(run_id: int) -> dict[str, Any]:
    return _run_file(run_id, "prompt")


@router.get("/waiting")
def waiting_gates() -> list[dict[str, Any]]:
    """Return every current approval gate backed by a kanban needs-input task."""
    store.init_db()
    with store._connect() as db:
        rows = db.execute(
            """
            SELECT s.*,i.board,i.recipe_id,i.recipe_version,i.status AS instance_status,
                   i.blocked_reason AS instance_blocked_reason,i.updated_at AS instance_updated_at
            FROM recipe_steps AS s JOIN recipe_instances AS i ON i.id=s.instance_id
            JOIN (SELECT instance_id,step_id,MAX(activation) AS activation FROM recipe_steps GROUP BY instance_id,step_id) AS latest
              ON latest.instance_id=s.instance_id AND latest.step_id=s.step_id AND latest.activation=s.activation
            WHERE s.primitive='approval_gate' AND s.state='waiting'
            ORDER BY i.updated_at DESC,s.step_id
            """
        ).fetchall()
        gates = [dict(row) for row in rows]
        positions: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for gate in gates:
            instance_id = gate["instance_id"]
            if instance_id not in positions:
                ordered_ids = _recipe_step_order_for(
                    db, gate["recipe_id"], gate["recipe_version"]
                )
                if not ordered_ids:
                    ordered_ids = [
                        step["step_id"] for step in _latest_steps(db, instance_id)
                    ]
                positions[instance_id] = {
                    step_id: index + 1 for index, step_id in enumerate(ordered_ids)
                }
                totals[instance_id] = len(ordered_ids)
            gate["step_position"] = positions[instance_id].get(gate["step_id"])
            gate["step_total"] = totals[instance_id]
            try:
                from shipfactory.decisions import current_binding
                gate.update(current_binding(db, instance_id, gate["step_id"]))
                story_hash = gate.get("review_story_sha256")
                gate["review_story"] = None
                if story_hash:
                    story_row = db.execute(
                        "SELECT * FROM artifacts WHERE instance_id=? AND kind='review-story' "
                        "AND state='sealed' AND sha256=?",
                        (instance_id, story_hash),
                    ).fetchone()
                    if story_row is not None:
                        from shipfactory.artifacts import (
                            artifact_document, dashboard_safe_review_story,
                        )
                        gate["review_story"] = dashboard_safe_review_story(
                            artifact_document(dict(story_row))
                        )
            except Exception as exc:
                gate["binding_error"] = str(exc)
        return gates


@router.get("/seats")
def seats() -> list[dict[str, Any]]:
    store.init_db()
    try:
        from shipfactory.seats_admin import seat_details
        return [seat | {"paused": store.seat_paused(seat["name"])} for seat in seat_details()]
    except (FileNotFoundError, OSError, ValueError):
        return []


@router.get("/profiles")
def profiles() -> list[str]:
    from shipfactory.seats_admin import list_profiles
    return list_profiles()


@router.post("/seats", status_code=201)
def create_seat(seat: SeatWrite) -> dict[str, Any]:
    """Create through the same writer used by ``hermes shipfactory seat-create``."""
    # A profile is required only for a hermes seat; a non-hermes seat's name is
    # a dispatch label decoupled from the profiles directory.
    required = (seat.name, seat.executor, seat.model, seat.role)
    if None in required or (seat.executor == "hermes" and seat.profile is None):
        raise HTTPException(status_code=422, detail="name, executor, model, and role are required (profile required for hermes)")
    try:
        from shipfactory.seats_admin import create_seat as create
        return create(seat.name, seat.profile, seat.executor, seat.model, seat.reasoning or "", seat.role,
                      seat.max_concurrent or 1, seat.provider_config,
                      config=seat.config, skills=seat.skills)
    except (ValueError, TypeError) as exc:
        raise _request_error(exc) from exc


@router.put("/seats/{name}")
def update_seat(name: str, seat: SeatWrite) -> dict[str, Any]:
    try:
        from shipfactory.seats_admin import update_seat as update
        return update(name, seat.profile, seat.executor, seat.model, seat.reasoning, seat.role,
                      seat.max_concurrent, seat.provider_config,
                      config=seat.config, skills=seat.skills)
    except (ValueError, TypeError) as exc:
        raise _request_error(exc) from exc


@router.get("/costs")
def costs(
    by: str = Query("seat", pattern="^(seat|executor|task|day|instance)$"),
    since_days: int = Query(1, ge=0, le=3650),
) -> list[dict[str, Any]]:
    if by in {"seat", "executor", "task"}:
        return store.costs_rollup(by, since_days)

    store.init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    with store._connect() as db:
        if by == "day":
            rows = db.execute(
                """SELECT utc_day AS day, COUNT(*) AS charges,
                          COALESCE(SUM(tokens), 0) AS tokens_total
                   FROM budget_charges WHERE created_at>=?
                   GROUP BY utc_day ORDER BY utc_day DESC""",
                (since,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT c.instance_id AS instance, i.board,
                          i.recipe_id || '@' || i.recipe_version AS recipe,
                          COUNT(*) AS charges,
                          COALESCE(SUM(c.tokens), 0) AS tokens_total
                   FROM budget_charges AS c
                   LEFT JOIN recipe_instances AS i ON i.id=c.instance_id
                   WHERE c.created_at>=?
                   GROUP BY c.instance_id, i.board, i.recipe_id, i.recipe_version
                   ORDER BY tokens_total DESC, c.instance_id""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]


@router.post("/approve")
def approve(decision: GateDecision) -> dict[str, str]:
    from shipfactory.decisions import DecisionConflict, record_decision
    try:
        row = record_decision(
            instance_id=decision.instance, step_id=decision.step,
            activation=decision.activation, revision_hash=decision.revision_hash,
            evidence_bundle_hash=decision.evidence_bundle_hash, nonce=decision.nonce,
            decision="approve", actor_kind=decision.actor_kind,
            actor_id=decision.actor_id, channel=decision.channel,
            reason=decision.reason,
        )
    except DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"key": str(row["advance_event_key"]), "decision_id": str(row["id"])}


@router.post("/reject")
def reject(decision: GateDecision) -> dict[str, str]:
    from shipfactory.decisions import DecisionConflict, record_decision
    try:
        row = record_decision(
            instance_id=decision.instance, step_id=decision.step,
            activation=decision.activation, revision_hash=decision.revision_hash,
            evidence_bundle_hash=decision.evidence_bundle_hash, nonce=decision.nonce,
            decision="reject", actor_kind=decision.actor_kind,
            actor_id=decision.actor_id, channel=decision.channel,
            reason=decision.reason,
        )
    except DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"key": str(row["advance_event_key"]), "decision_id": str(row["id"])}


@router.post("/phone-token")
def phone_token(request: PhoneTokenRequest) -> dict[str, str]:
    """Create an expiring action token; this does not record a decision."""
    from shipfactory.decisions import DecisionConflict, issue_phone_token
    try:
        token = issue_phone_token(
            instance_id=request.instance, step_id=request.step,
            activation=request.activation, revision_hash=request.revision_hash,
            evidence_bundle_hash=request.evidence_bundle_hash,
            decision=request.decision, nonce=request.nonce,
            ttl_seconds=request.ttl_seconds,
        )
    except DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"token": token}


@router.post("/phone-decision")
def phone_decision(request: PhoneTokenDecision) -> dict[str, Any]:
    from shipfactory.decisions import (
        DecisionConflict, DecisionTokenError, consume_phone_token,
    )
    try:
        row = consume_phone_token(
            request.token, actor_kind=request.actor_kind, actor_id=request.actor_id,
            channel=request.channel, reason=request.reason,
        )
    except DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DecisionTokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "key": row["advance_event_key"], "decision_id": row["id"],
        "replayed": bool(row.get("replayed")),
    }
