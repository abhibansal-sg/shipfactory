"""Recipe instance construction: flat kanban tasks plus inert collectors."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from headframe import store
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
    collector = kanban_db.create_blocked_task(conn, title=f"Recipe collector {recipe.key}", body="Inert Factory completion collector.", parents=parent_tasks or (), idempotency_key=collector_key, board=board, block_kind="needs_input", reason="recipe_collector")
    now = store._now(); skips = set(skip_steps or [])
    with store._connect() as db:
        db.execute("INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (instance_id, board, collector, recipe.document["id"], recipe.document["version"], recipe.hash, "running", json.dumps(bound, sort_keys=True), now, now))
        for step in recipe.document["steps"]:
            state = "skipped" if step["id"] in skips else "pending"
            db.execute("INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (instance_id, step["id"], 1, step["primitive"], state, now, now))
    return {"instance_id": instance_id, "collector_task_id": collector, "recipe": recipe.key, "parameters": bound}


def replace_unactivated(*, instance_id: str, recipe: Recipe, parameters: dict[str, Any], skip_steps: list[str] | None = None) -> dict[str, Any]:
    """Replace an instance's pinned graph while retaining its collector and id.

    Reroute is only an in-place operation before the first external activation.
    Once a task exists, callers must cancel and instantiate a distinct instance
    so the old graph and its artifacts remain immutable audit history.
    """
    bound = bind_parameters(recipe, parameters, skip_steps)
    now = store._now(); skips = set(skip_steps or [])
    with store._connect() as db:
        instance = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        if instance is None:
            raise ValueError("unknown recipe instance")
        activated = db.execute(
            "SELECT 1 FROM recipe_steps WHERE instance_id=? "
            "AND (kanban_task_id IS NOT NULL OR state NOT IN ('pending','skipped')) LIMIT 1",
            (instance_id,),
        ).fetchone()
        if activated or int(instance["activation_count"]):
            raise ValueError("cannot replace an activated recipe instance in place")
        db.execute("DELETE FROM recipe_steps WHERE instance_id=?", (instance_id,))
        db.execute(
            "UPDATE recipe_instances SET recipe_id=?,recipe_version=?,recipe_hash=?,status='running',"
            "parameters_json=?,activation_count=0,tokens_charged=0,blocked_reason=NULL,updated_at=? WHERE id=?",
            (
                recipe.document["id"], recipe.document["version"], recipe.hash,
                json.dumps(bound, sort_keys=True), now, instance_id,
            ),
        )
        for step in recipe.document["steps"]:
            state = "skipped" if step["id"] in skips else "pending"
            db.execute(
                "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (instance_id, step["id"], 1, step["primitive"], state, now, now),
            )
    return {
        "instance_id": instance_id,
        "collector_task_id": instance["collector_task_id"],
        "recipe": recipe.key,
        "parameters": bound,
    }


def recipe_for_instance(instance: dict[str, Any]) -> Recipe:
    row = None
    with store._connect() as db:
        row = db.execute("SELECT normalized_yaml,hash FROM recipe_versions WHERE id=? AND version=?", (instance["recipe_id"], instance["recipe_version"])).fetchone()
    if row is None: raise RuntimeError("pinned recipe version missing")
    return Recipe(json.loads(row["normalized_yaml"]), row["hash"])


def revision_vector(db: Any, instance_id: str, step: dict[str, Any], recipe: dict[str, Any]) -> str:
    """Hash the latest completed upstream producer revisions for a gate."""
    definitions = {item["id"]: item for item in recipe["steps"]}
    ancestors: set[str] = set()

    def collect(step_id: str) -> None:
        for parent in definitions[step_id]["needs"]:
            if parent not in ancestors:
                ancestors.add(parent)
                collect(parent)

    collect(step["step_id"])
    producers = sorted(
        step_id for step_id in ancestors
        if definitions[step_id]["primitive"] == "agent_task"
    )
    values = []
    for step_id in producers:
        row = db.execute(
            "SELECT activation,output_revision FROM recipe_steps "
            "WHERE instance_id=? AND step_id=? AND state='done' "
            "ORDER BY activation DESC LIMIT 1",
            (instance_id, step_id),
        ).fetchone()
        if row is not None:
            values.append((step_id, int(row["activation"]), row["output_revision"]))
    text = json.dumps(values, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
