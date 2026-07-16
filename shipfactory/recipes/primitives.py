"""Recipe primitive activation rules and strict review verdict parsing."""
from __future__ import annotations

import base64
import json
import re
import hashlib
import subprocess
from pathlib import Path
from typing import Any

from shipfactory import store
from shipfactory.policy import citation_ok
from .instantiate import task_key

_VERDICT = re.compile(r"^SHIPFACTORY_VERDICT:\s*(\{.*\})\s*$")


def _upstream_ids(recipe: dict[str, Any], step_id: str) -> list[str]:
    defs = {item["id"]: item for item in recipe["steps"]}
    found: set[str] = set()

    def visit(node: str) -> None:
        for parent in defs[node].get("needs", []):
            if parent not in found:
                found.add(parent)
                visit(parent)

    visit(step_id)
    return [item["id"] for item in recipe["steps"] if item["id"] in found]


def build_review_input_context(
    db: Any, instance: dict[str, Any], recipe: dict[str, Any], step_def: dict[str, Any],
) -> tuple[str, str]:
    """Open and reverify every transitive sealed input for a review task."""
    from shipfactory.artifacts import artifact_document
    from shipfactory.verification import (
        _canonical, _evidence_root, assert_commit_binding, verify_evidence_bundle,
    )

    upstream = _upstream_ids(recipe, step_def["id"])
    snapshot: dict[str, Any] = {
        "schema": "shipfactory.review-input/v1",
        "instance_id": instance["id"], "review_step_id": step_def["id"],
        "artifacts": [], "evidence_bundles": [], "exact_diff": None,
    }
    artifacts_by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("task-spec", "plan"):
        if not upstream:
            continue
        placeholders = ",".join("?" for _ in upstream)
        row = db.execute(
            f"SELECT * FROM artifacts WHERE instance_id=? AND step_id IN ({placeholders}) "
            "AND kind=? AND state='sealed' ORDER BY activation DESC,sealed_at DESC LIMIT 1",
            (instance["id"], *upstream, kind),
        ).fetchone()
        if row is None:
            continue
        artifact = dict(row)
        document = artifact_document(artifact)
        sealed_bytes = Path(artifact["sealed_path"]).read_bytes()
        snapshot["artifacts"].append({
            "id": artifact["id"], "kind": kind, "sha256": artifact["sha256"],
            "activation": int(artifact["activation"]), "document": document,
            "sealed_bytes_b64": base64.b64encode(sealed_bytes).decode("ascii"),
            "sealed_size_bytes": len(sealed_bytes),
        })
        artifacts_by_kind[kind] = artifact
    if {"task-spec", "plan"} <= set(artifacts_by_kind):
        plan_document = artifact_document(artifacts_by_kind["plan"])
        if plan_document.get("task_spec_sha256") != artifacts_by_kind["task-spec"]["sha256"]:
            raise ValueError("review plan is not bound to the selected sealed task-spec")
    for producer_id in upstream:
        definition = next(item for item in recipe["steps"] if item["id"] == producer_id)
        if definition["primitive"] != "verification":
            continue
        rows = db.execute(
            "SELECT * FROM evidence_bundles WHERE instance_id=? AND step_id=? "
            "AND sealed_at IS NOT NULL ORDER BY activation,sealed_at",
            (instance["id"], producer_id),
        ).fetchall()
        if not rows:
            raise ValueError(f"review evidence is missing for {producer_id}")
        for row in rows:
            bundle = verify_evidence_bundle(row["id"], db=db)
            bundle_path = _evidence_root(
                bundle["instance_id"], bundle["step_id"], int(bundle["activation"]),
            ) / "bundle.json"
            sealed_bytes = bundle_path.read_bytes()
            snapshot["evidence_bundles"].append({
                "id": bundle["id"], "activation": int(bundle["activation"]),
                "sha256": bundle["bundle_sha256"],
                "sealed_bytes_b64": base64.b64encode(sealed_bytes).decode("ascii"),
                "sealed_size_bytes": len(sealed_bytes),
                "document": json.loads(sealed_bytes),
            })
    change_inputs = [
        item for node in [step_def["id"], *upstream]
        for item in next(
            definition for definition in recipe["steps"] if definition["id"] == node
        ).get("inputs", [])
        if item.get("kind") == "change-set" and item.get("from") in upstream
    ]
    if change_inputs:
        producer_id = change_inputs[0]["from"]
        producer_step = db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1",
            (instance["id"], producer_id),
        ).fetchone()
        if (producer_step is None or not producer_step["kanban_task_id"]
                or producer_step["producer_run_id"] is None):
            raise ValueError("review change-set exact producer task/run identity is missing")
        activation = int(producer_step["activation"])
        run_id = int(producer_step["producer_run_id"])
        run_row = db.execute(
            "SELECT * FROM runs WHERE id=? AND task_id=? AND recipe_activation=?",
            (run_id, str(producer_step["kanban_task_id"]), activation),
        ).fetchone()
        run = dict(run_row) if run_row is not None else None
        if run is None or not run.get("workspace_path"):
            raise ValueError("review exact producer run/workspace is missing")
        if not snapshot["evidence_bundles"]:
            raise ValueError("review change-set has no transitive verification identity")
        verified = snapshot["evidence_bundles"][-1]["document"]
        workspace = Path(run["workspace_path"])
        assert_commit_binding(workspace, verified["head_sha"], verified["tree_sha"])
        diff_bytes = subprocess.check_output(
            ["git", "diff", "--binary", verified["base_sha"], verified["head_sha"]],
            cwd=workspace, stderr=subprocess.PIPE, timeout=30,
        )
        snapshot["exact_diff"] = {
            "producer_step_id": producer_id, "producer_task_id": producer_step["kanban_task_id"],
            "producer_activation": activation, "producer_run_id": run_id,
            "workspace_path": str(workspace.resolve()),
            "base_sha": verified["base_sha"], "head_sha": verified["head_sha"],
            "tree_sha": verified["tree_sha"],
            "sha256": hashlib.sha256(diff_bytes).hexdigest(), "size_bytes": len(diff_bytes),
            "bytes_b64": base64.b64encode(diff_bytes).decode("ascii"),
        }
    raw = _canonical(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    body = (
        "\n\n## Factory-sealed review inputs\n"
        "The JSON below was opened and integrity-checked by Factory immediately before task creation.\n"
        f"SHIPFACTORY_REVIEW_INPUT_SHA256: {digest}\n"
        "```json\n" + raw.decode("utf-8") + "\n```"
    )
    return body, digest

def parse_verdict(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    match = _VERDICT.fullmatch(lines[-1]) if lines else None
    if not match: raise ValueError("review final line must be SHIPFACTORY_VERDICT JSON")
    try: verdict = json.loads(match.group(1))
    except json.JSONDecodeError as exc: raise ValueError("invalid SHIPFACTORY_VERDICT JSON") from exc
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
    if primitive == "review_gate" and db is not None and recipe.get("schema") == "shipfactory.recipe/v2":
        review_context, _digest = build_review_input_context(db, instance, recipe, step_def)
        body += review_context
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
            from .advancer import _plan_action
            _plan_action(
                db, logical_key=key, kind="notification_delivery",
                payload={"outbox_key": key, "board": instance.get("board")},
                instance_id=instance["id"], step_id=step["step_id"],
                activation=int(step["activation"]),
            )
        else:
            with store._connect() as fresh:
                fresh.execute("INSERT OR IGNORE INTO outbox(key,target,message,state,attempts,next_attempt_at) VALUES(?,?,?,'pending',0,?)", (key, render(params["target"]), body, store._now()))
                from .advancer import _plan_action
                _plan_action(
                    fresh, logical_key=key, kind="notification_delivery",
                    payload={"outbox_key": key, "board": instance.get("board")},
                    instance_id=instance["id"], step_id=step["step_id"],
                    activation=int(step["activation"]),
                )
        return None
    if primitive == "verification":
        # The advancer journals this non-model action directly; it has no
        # kanban task, seat, executor, or model activation.
        return None
    raise RuntimeError(f"unknown primitive {primitive}")


__all__ = ["parse_verdict", "activate", "build_review_input_context"]
