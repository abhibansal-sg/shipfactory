"""Recipe instance construction: flat kanban tasks plus inert collectors."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from shipfactory import store
from .loader import Recipe, _canonical, bind_parameters


class RecipePolicyError(RuntimeError):
    """A persisted recipe policy cannot be trusted for its instance."""


def task_key(instance_id: str, recipe_hash: str, step_id: str, activation: int) -> str:
    return f"recipe/{instance_id}/{recipe_hash}/{step_id}/{activation}"


def _render(value: Any, parameters: dict[str, Any]) -> Any:
    if not isinstance(value, str): return value
    for key, item in parameters.items():
        token = "${" + key + "}"
        if value == token: return item
        value = value.replace(token, "" if item is None else str(item))
    return value


def current_base_sha(workspace: str | Path | None = None) -> str:
    """Resolve the trusted Git base used for a new or rerouted instance."""
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace or Path.cwd(), text=True,
            stderr=subprocess.PIPE, timeout=10,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("recipe instance base_sha requires a Git workspace") from exc
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("recipe instance base_sha is not a commit hash")
    return value


def _base_sha(value: str | None) -> str:
    resolved = (value or current_base_sha()).lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resolved):
        raise ValueError("recipe instance base_sha is not a commit hash")
    return resolved


def instantiate(conn: Any, *, board: str, recipe: Recipe, parameters: dict[str, Any], skip_steps: list[str] | None = None, parent_tasks: list[str] | None = None, instance_id: str | None = None, base_sha: str | None = None) -> dict[str, Any]:
    """Persist one pinned instance and its collector; the advancer creates work.

    No task is made ready by this function except the inert collector, which is
    atomically sticky-blocked.  This keeps all flow mutation in the advancer.
    """
    from hermes_cli import kanban_db
    bound = bind_parameters(recipe, parameters, skip_steps)
    base_sha = _base_sha(base_sha)
    instance_id = instance_id or str(uuid.uuid4())
    collector_key = f"recipe/{instance_id}/{recipe.hash}/collector"
    collector = kanban_db.create_blocked_task(conn, title=f"Recipe collector {recipe.key}", body="Inert Factory completion collector.", parents=parent_tasks or (), idempotency_key=collector_key, board=board, block_kind="needs_input", reason="recipe_collector")
    now = store._now(); skips = set(skip_steps or [])
    with store._connect() as db:
        db.execute("INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,base_sha,updated_base_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (instance_id, board, collector, recipe.document["id"], recipe.document["version"], recipe.hash, "running", json.dumps(bound, sort_keys=True), base_sha, now, now, now))
        for step in recipe.document["steps"]:
            state = "skipped" if step["id"] in skips else "pending"
            db.execute("INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (instance_id, step["id"], 1, step["primitive"], state, now, now))
    return {"instance_id": instance_id, "collector_task_id": collector, "recipe": recipe.key, "parameters": bound, "base_sha": base_sha}


def replace_unactivated(*, instance_id: str, recipe: Recipe, parameters: dict[str, Any], skip_steps: list[str] | None = None, base_sha: str | None = None) -> dict[str, Any]:
    """Replace an instance's pinned graph while retaining its collector and id.

    Reroute is only an in-place operation before the first external activation.
    Once a task exists, callers must cancel and instantiate a distinct instance
    so the old graph and its artifacts remain immutable audit history.
    """
    bound = bind_parameters(recipe, parameters, skip_steps)
    base_sha = _base_sha(base_sha)
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
            "parameters_json=?,base_sha=?,updated_base_at=?,activation_count=0,tokens_charged=0,"
            "blocked_reason=NULL,updated_at=? WHERE id=?",
            (
                recipe.document["id"], recipe.document["version"], recipe.hash,
                json.dumps(bound, sort_keys=True), base_sha, now, now, instance_id,
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
        "base_sha": base_sha,
    }


def recipe_for_instance(instance: dict[str, Any], db: Any | None = None) -> Recipe:
    """Load and verify the immutable policy bound to an instance.

    ``db`` lets callers validate against their current transaction snapshot;
    callers outside a transaction get a short-lived read connection.
    """
    def load(connection: Any) -> Recipe:
        row = connection.execute(
            "SELECT normalized_yaml,hash FROM recipe_versions WHERE id=? AND version=?",
            (instance["recipe_id"], instance["recipe_version"]),
        ).fetchone()
        if row is None:
            raise RecipePolicyError("pinned recipe version missing")
        normalized_yaml = row["normalized_yaml"]
        try:
            document = json.loads(normalized_yaml)
        except Exception as exc:
            raise RecipePolicyError(
                f"pinned recipe policy bytes are malformed: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise RecipePolicyError("pinned recipe policy bytes are not a document")
        canonical = _canonical(document).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        version_hash = str(row["hash"] or "")
        instance_hash = str(instance["recipe_hash"] or "")
        if digest != version_hash:
            raise RecipePolicyError(
                "pinned recipe policy bytes hash mismatch: "
                f"normalized_yaml sha256 {digest} != recipe_versions.hash {version_hash}"
            )
        if digest != instance_hash:
            raise RecipePolicyError(
                "pinned recipe policy bytes hash mismatch: "
                f"normalized_yaml sha256 {digest} != recipe_instances.recipe_hash {instance_hash}"
            )
        if document.get("id") != instance["recipe_id"]:
            raise RecipePolicyError(
                "pinned recipe policy identity mismatch: "
                f"document id {document.get('id')!r} != recipe_instances.recipe_id "
                f"{instance['recipe_id']!r}"
            )
        try:
            document_version = int(document.get("version"))
            instance_version = int(instance["recipe_version"])
        except (TypeError, ValueError, KeyError) as exc:
            raise RecipePolicyError(
                "pinned recipe policy identity has an invalid version"
            ) from exc
        if document_version != instance_version:
            raise RecipePolicyError(
                "pinned recipe policy identity mismatch: "
                f"document version {document_version} != recipe_instances.recipe_version "
                f"{instance_version}"
            )
        return Recipe(document, digest)

    if db is not None:
        return load(db)
    with store._connect() as connection:
        return load(connection)


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
    if recipe.get("schema") == "shipfactory.recipe/v2":
        # V2 gates bind authoritative bytes, not ordinal model-task revision
        # counters.  Reopen every declared artifact/evidence input so a stale,
        # replaced, or mismatched sealed object cannot retain the same gate
        # revision.  Review activations are included because they carry no
        # output artifact of their own but are still required approvals in the
        # exact sequential chain.
        from shipfactory.artifacts import input_artifacts

        definition = definitions[step["step_id"]]
        bound_inputs = input_artifacts(db, instance_id, definition)
        artifact_values = sorted([
            {
                "id": str(item["id"]), "kind": str(item["kind"]),
                "sha256": str(item["sha256"]), "base_sha": str(item["base_sha"]),
                "head_sha": str(item.get("head_sha") or ""),
                "tree_sha": str(item.get("repo_tree_sha") or ""),
            }
            for item in bound_inputs
        ], key=lambda item: (item["kind"], item["id"]))
        review_values = []
        for review_id in sorted(
            node for node in ancestors
            if definitions[node]["primitive"] == "review_gate"
        ):
            row = db.execute(
                "SELECT activation,state,kanban_task_id FROM recipe_steps "
                "WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
                (instance_id, review_id),
            ).fetchone()
            if row is not None:
                review_values.append({
                    "step_id": review_id, "activation": int(row["activation"]),
                    "state": str(row["state"]),
                    "kanban_task_id": str(row["kanban_task_id"] or ""),
                })
        instance = db.execute(
            "SELECT base_sha,recipe_hash FROM recipe_instances WHERE id=?",
            (instance_id,),
        ).fetchone()
        payload = {
            "schema": "shipfactory.revision-vector/v2",
            "instance_id": instance_id, "step_id": step["step_id"],
            "base_sha": str(instance["base_sha"] or "") if instance else "",
            "artifacts": artifact_values, "reviews": review_values,
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()
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
