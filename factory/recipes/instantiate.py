"""Recipe instance construction: flat kanban tasks plus inert collectors."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from factory import store
from .loader import Recipe, bind_parameters


def task_key(instance_id: str, recipe_hash: str, step_id: str, activation: int) -> str:
    return f"recipe/{instance_id}/{recipe_hash}/{step_id}/{activation}"


def _render(value: Any, parameters: dict[str, Any]) -> Any:
    if not isinstance(value, str): return value
    for key, item in parameters.items():
        token = "${" + key + "}"
        if value == token: return item
        value = value.replace(token, "" if item is None else str(item))
    return value


def instantiate(conn: Any, *, board: str, recipe: Recipe, parameters: dict[str, Any], skip_steps: list[str] | None = None, parent_tasks: list[str] | None = None, instance_id: str | None = None) -> dict[str, Any]:
    """Persist one pinned instance and its collector; the advancer creates work.

    No task is made ready by this function except the inert collector, which is
    atomically sticky-blocked.  This keeps all flow mutation in the advancer.
    """
    from hermes_cli import kanban_db
    bound = bind_parameters(recipe, parameters, skip_steps)
    instance_id = instance_id or str(uuid.uuid4())
    collector_key = f"recipe/{instance_id}/{recipe.hash}/collector"
    collector = kanban_db.create_blocked_task(conn, title=f"Recipe collector {recipe.key}", body="Inert Factory completion collector.", parents=parent_tasks or (), idempotency_key=collector_key, block_kind="needs_input", reason="recipe_collector")
    now = store._now(); skips = set(skip_steps or [])
    with store._connect() as db:
        db.execute("INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (instance_id, board, collector, recipe.document["id"], recipe.document["version"], recipe.hash, "running", json.dumps(bound, sort_keys=True), now, now))
        for step in recipe.document["steps"]:
            state = "skipped" if step["id"] in skips else "pending"
            db.execute("INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (instance_id, step["id"], 1, step["primitive"], state, now, now))
    return {"instance_id": instance_id, "collector_task_id": collector, "recipe": recipe.key, "parameters": bound}


def recipe_for_instance(instance: dict[str, Any]) -> Recipe:
    row = None
    with store._connect() as db:
        row = db.execute("SELECT normalized_yaml,hash FROM recipe_versions WHERE id=? AND version=?", (instance["recipe_id"], instance["recipe_version"])).fetchone()
    if row is None: raise RuntimeError("pinned recipe version missing")
    return Recipe(json.loads(row["normalized_yaml"]), row["hash"])


def revision_vector(db: Any, instance_id: str, step: dict[str, Any]) -> str:
    rows = db.execute("SELECT step_id,output_revision FROM recipe_steps WHERE instance_id=? AND state='done' ORDER BY step_id,activation", (instance_id,)).fetchall()
    text = json.dumps([(r["step_id"], r["output_revision"]) for r in rows], separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
