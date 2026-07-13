"""Artifact-discipline recipe regressions."""
from __future__ import annotations

import json
from pathlib import Path

from factory import store
from factory.recipes.advancer import apply_events, reconcile, release_review_stall
from factory.recipes.instantiate import instantiate
from factory.recipes.loader import load_library


PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}


def _review_loop_recipe(tmp_path: Path):
    library = tmp_path / "review-loop-library"
    library.mkdir()
    (library / "review-loop@1.yaml").write_text(
        """schema: factory.recipe/v1
id: review-loop
version: 1
status: active
description: bounded review-stall loop
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 3, max_tokens: 500000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: verify
    primitive: review_gate
    title: Verify
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: verify, execution_profile: standard, workspace: worktree}
""",
        encoding="utf-8",
    )
    return load_library(library).get("review-loop@1")


def _step(instance_id: str, step_id: str, activation: int | None = None) -> dict | None:
    with store._connect() as db:
        if activation is None:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
                (instance_id, step_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND activation=?",
                (instance_id, step_id, activation),
            ).fetchone()
    return dict(row) if row else None


def _instance(instance_id: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
    assert row
    return dict(row)


def _reject_round(kanban_conn, instance_id: str, activation: int, count: int | None) -> None:
    from hermes_cli import kanban_db

    build = _step(instance_id, "build", activation)
    assert build and build["state"] == "running"
    assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result=f"build {activation}")
    reconcile(kanban_conn, instance_id, profiles=PROFILES)
    review = _step(instance_id, "verify", activation)
    assert review and review["state"] == "running"
    if count is None:
        body = "factory/recipes/advancer.py:1 requires changes"
    else:
        body = f"finding_count: {count}\nfactory/recipes/advancer.py:1 requires changes"
    verdict = {
        "outcome": "request_changes",
        "target_step": "build",
        "body": body,
    }
    result = "FACTORY_VERDICT: " + json.dumps(verdict, separators=(",", ":"))
    assert kanban_db.complete_task(kanban_conn, review["kanban_task_id"], result=result)
    reconcile(kanban_conn, instance_id, profiles=PROFILES)


def test_shrinking_rejection_counts_continue_until_activation_cap(tmp_path, kanban_conn):
    recipe = _review_loop_recipe(tmp_path)
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="shrinking")
    reconcile(kanban_conn, "shrinking", profiles=PROFILES)

    for activation, count in enumerate((3, 2, 1), start=1):
        _reject_round(kanban_conn, "shrinking", activation, count)
        assert _step("shrinking", "verify", activation)["finding_count"] == count

    assert _step("shrinking", "build", 4)["blocked_reason"] == "activation_fuse"
    assert _step("shrinking", "verify", 4)["state"] == "pending"
    assert _instance("shrinking")["activation_count"] == 6


def test_flat_rejection_counts_park_before_third_activation_and_release_is_audited(tmp_path, kanban_conn):
    recipe = _review_loop_recipe(tmp_path)
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="flat")
    reconcile(kanban_conn, "flat", profiles=PROFILES)
    _reject_round(kanban_conn, "flat", 1, 2)
    _reject_round(kanban_conn, "flat", 2, 2)

    parked = _instance("flat")
    assert parked["status"] == "blocked" and parked["blocked_reason"] == "review_stall"
    assert _step("flat", "verify", 2)["blocked_reason"] == "review_stall"
    assert _step("flat", "build", 3) is None
    assert parked["activation_count"] == 4
    charged = parked["tokens_charged"]
    reconcile(kanban_conn, "flat", profiles=PROFILES)
    assert _instance("flat")["tokens_charged"] == charged

    key = release_review_stall("flat", "verify", "operator accepts another bounded revision")
    apply_events(kanban_conn, profiles=PROFILES)
    assert _step("flat", "build", 3)["state"] == "running"
    with store._connect() as db:
        audit = db.execute("SELECT source,payload_json,state FROM advance_events WHERE key=?", (key,)).fetchone()
    assert audit["source"] == "operator_release" and audit["state"] == "applied"
    assert json.loads(audit["payload_json"])["reason"] == "operator accepts another bounded revision"


def test_unparseable_rejection_counts_never_stall_and_existing_cap_wins(tmp_path, kanban_conn):
    recipe = _review_loop_recipe(tmp_path)
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="unknown-count")
    reconcile(kanban_conn, "unknown-count", profiles=PROFILES)

    for activation in (1, 2, 3):
        _reject_round(kanban_conn, "unknown-count", activation, None)
        assert _step("unknown-count", "verify", activation)["finding_count"] == -1
        assert _step("unknown-count", "verify", activation)["blocked_reason"] == "changes_requested"

    assert _step("unknown-count", "build", 4)["blocked_reason"] == "activation_fuse"
    assert _instance("unknown-count")["blocked_reason"] == "activation_fuse"
