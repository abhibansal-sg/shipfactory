"""Artifact-discipline recipe regressions."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from factory import store
from factory.recipes.advancer import (
    apply_events,
    event,
    gate_decision,
    reconcile,
    release_review_stall,
)
from factory.recipes.instantiate import instantiate
from factory.recipes.loader import load_library


PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}

ROOT = Path(__file__).resolve().parents[1]
DEV_PIPELINE_V1_SHA256 = "a5911fd612ca25a50d3d0af066fc6d3b6d11f991dd9c423f55ed27d843a180c1"


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


def test_dev_pipeline_v2_loads_in_order_and_v1_bytes_are_immutable():
    v1 = ROOT / "recipes" / "dev-pipeline@1.yaml"
    assert hashlib.sha256(v1.read_bytes()).hexdigest() == DEV_PIPELINE_V1_SHA256

    recipe = load_library(ROOT / "recipes", persist=False).get("dev-pipeline@2").document
    assert recipe["budgets"] == {
        "max_activations": 12,
        "max_step_activations": 3,
        "max_tokens": 300_000,
    }
    assert [step["id"] for step in recipe["steps"]] == [
        "plan-check", "build", "verify", "approval", "notify",
    ]
    assert [step["needs"] for step in recipe["steps"]] == [
        [], ["plan-check"], ["build"], ["verify"], ["approval"],
    ]


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


def _human_gate_recipe(tmp_path: Path, primitive: str):
    recipe_id = "approval-note" if primitive == "approval_gate" else "event-note"
    gate_params = (
        "{approvers: [operator], instructions: Approve the verified artifact.}"
        if primitive == "approval_gate"
        else "{event: artifact_ready}"
    )
    library = tmp_path / f"{recipe_id}-library"
    library.mkdir()
    (library / f"{recipe_id}@1.yaml").write_text(
        f"""schema: factory.recipe/v1
id: {recipe_id}
version: 1
status: active
description: resume-note gate test
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 1, max_tokens: 100000}}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {{seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}}
  - id: gate
    primitive: {primitive}
    title: Human gate
    needs: [build]
    optional: false
    params: {gate_params}
""",
        encoding="utf-8",
    )
    return load_library(library).get(f"{recipe_id}@1")


def _park_human_gate(tmp_path: Path, kanban_conn, primitive: str, instance_id: str) -> dict:
    from hermes_cli import kanban_db

    recipe = _human_gate_recipe(tmp_path, primitive)
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id=instance_id)
    reconcile(kanban_conn, instance_id, profiles=PROFILES)
    build = _step(instance_id, "build")
    assert kanban_db.complete_task(
        kanban_conn,
        build["kanban_task_id"],
        result="JWT auth with refresh rotation using jose",
    )
    reconcile(kanban_conn, instance_id, profiles=PROFILES)
    gate = _step(instance_id, "gate")
    assert gate and gate["state"] == "waiting"
    comments = kanban_db.list_comments(kanban_conn, gate["kanban_task_id"])
    assert len(comments) == 1
    note = comments[0].body
    assert "CONTINUE-HERE" in note
    assert f"Instance: {instance_id}" in note and "Step: gate" in note
    assert "build: JWT auth with refresh rotation using jose" in note
    assert "## Done" in note and "## Left" in note and "## Decisions and Why" in note
    assert "## Blockers" in note and "## Next Action" in note
    return gate


def test_approval_gate_resume_note_is_written_and_consumed(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    gate = _park_human_gate(tmp_path, kanban_conn, "approval_gate", "approval-resume")
    comments = kanban_db.list_comments(kanban_conn, gate["kanban_task_id"])
    assert "Approval required: Approve the verified artifact." in comments[0].body

    gate_decision("approval-resume", "gate", "approve")
    apply_events(kanban_conn, profiles=PROFILES)
    comments = kanban_db.list_comments(kanban_conn, gate["kanban_task_id"])
    assert len([comment for comment in comments if comment.body.startswith("RESUMED ")]) == 1


def test_wait_for_event_resume_note_is_written_and_consumed(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    gate = _park_human_gate(tmp_path, kanban_conn, "wait_for_event", "event-resume")
    comments = kanban_db.list_comments(kanban_conn, gate["kanban_task_id"])
    assert "Event required: artifact_ready." in comments[0].body

    event("event-resume", "gate", {"id": "artifact-1", "type": "artifact_ready"})
    apply_events(kanban_conn, profiles=PROFILES)
    comments = kanban_db.list_comments(kanban_conn, gate["kanban_task_id"])
    assert len([comment for comment in comments if comment.body.startswith("RESUMED ")]) == 1
