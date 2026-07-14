"""Recipe v2 regression coverage against the real Hermes kanban database."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import pytest

from factory import store
from factory.cli import _recipe_gate, _reroute
from factory.config import FactoryConfig
from factory.recipes.advancer import (
    advance_key,
    cancel,
    event,
    reconcile,
    reconcile_root_collectors,
    startup_guard,
)
from factory.recipes.instantiate import instantiate
from factory.recipes.loader import RecipeError, load_library


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}


def _recipe(tmp_path: Path, text: str, key: str):
    library_path = tmp_path / ("library-" + key.replace("@", "-"))
    library_path.mkdir()
    (library_path / f"{key}.yaml").write_text(text, encoding="utf-8")
    return load_library(library_path).get(key)


def _step(instance_id: str, step_id: str, activation: int | None = None) -> dict:
    with store._connect() as db:
        if activation is None:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
                "ORDER BY activation DESC LIMIT 1",
                (instance_id, step_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND activation=?",
                (instance_id, step_id, activation),
            ).fetchone()
    assert row is not None
    return dict(row)


def _instance(instance_id: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
    assert row is not None
    return dict(row)


def _complete_review(kanban_db, conn, task_id: str, *, outcome: str, target: str | None = None) -> None:
    verdict = {"outcome": outcome}
    if target is not None:
        verdict["target_step"] = target
        verdict["body"] = "factory/recipes/advancer.py:84 requires changes"
    else:
        verdict["body"] = "APPROVE clean pass"
    result = "FACTORY_VERDICT: " + json.dumps(verdict, separators=(",", ":"))
    assert kanban_db.complete_task(conn, task_id, result=result, summary="reviewed")


def _one_step_recipe(tmp_path: Path, key: str = "single@1"):
    recipe_id, version = key.split("@")
    return _recipe(
        tmp_path,
        f"""schema: factory.recipe/v1
id: {recipe_id}
version: {version}
status: active
description: one real worker task
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 1, max_tokens: 50000}}
steps:
  - id: work
    primitive: agent_task
    title: Do work
    needs: []
    optional: false
    params: {{seat: dev-backend, instructions: do it, execution_profile: standard, workspace: worktree}}
""",
        key,
    )


def _selection_with_siblings(conn, tmp_path: Path):
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _one_step_recipe(tmp_path)
    root = kanban_db.create_blocked_task(
        conn,
        title="Triage root collector",
        body="wait for all siblings",
        block_kind="needs_input",
        reason="recipe_root_collector",
    )
    left = instantiate(conn, board="test", recipe=recipe, parameters={}, instance_id="left")
    right = instantiate(conn, board="test", recipe=recipe, parameters={}, instance_id="right")
    kanban_db.link_tasks(conn, left["collector_task_id"], root)
    kanban_db.link_tasks(conn, right["collector_task_id"], root)
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO triage_selections(id,source_task_id,board,ranked_json,outcome,"
            "root_collector_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("selection", root, "test", "[]", "selected", root, now, now),
        )
    reconcile(conn, "left", profiles=PROFILES)
    reconcile(conn, "right", profiles=PROFILES)
    return root, left, right


def test_loader_persists_immutable_normalized_recipe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = tmp_path / "r.yaml"
    source.write_text("""schema: factory.recipe/v1
id: test
version: 1
status: active
description: test
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 1, max_step_activations: 1, max_tokens: 1}
steps:
 - id: work
   primitive: notify
   title: n
   needs: []
   optional: false
   params: {target: x, message: y}
""")
    library = load_library(tmp_path)
    assert library.get("test@1").hash
    source.write_text(source.read_text().replace("description: test", "description: changed"))
    with pytest.raises(RecipeError, match="immutable"):
        load_library(tmp_path)


def test_restart_reconciliation_activates_review_once_after_swallowed_hook(tmp_path, kanban_conn):
    """§17.7: reconciliation reproduces the missing transition after restart."""
    from hermes_cli import kanban_db

    library = load_library(ROOT / "recipes")
    created = instantiate(
        kanban_conn,
        board="test",
        recipe=library.get("dev-pipeline@1"),
        parameters={"request": "harden restart handling"},
        instance_id="restart",
    )
    reconcile(kanban_conn, "restart", profiles=PROFILES)
    build = _step("restart", "build")
    assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result="built")

    # No completion event/hook is delivered: the daemon's restart pass alone advances.
    reconcile(kanban_conn, "restart", profiles=PROFILES)
    review = _step("restart", "review")
    assert review["state"] == "running"
    assert review["kanban_task_id"]
    key = advance_key("restart", library.get("dev-pipeline@1").hash, "review", 1, "running", "activate")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE key=?", (key,)).fetchone()[0] == 1

    reconcile(kanban_conn, "restart", profiles=PROFILES)
    assert _step("restart", "review")["kanban_task_id"] == review["kanban_task_id"]
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE ?",
        (f"recipe/restart/{library.get('dev-pipeline@1').hash}/review/%",),
    ).fetchone()[0] == 1
    assert kanban_db.get_task(kanban_conn, created["collector_task_id"]).status == "blocked"


def test_duplicate_event_is_one_durable_key_and_one_task_transition(tmp_path, kanban_conn):
    """§17.7: duplicate external and task-level advance keys are no-ops."""
    recipe = _one_step_recipe(tmp_path)
    created = instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="duplicate")
    first_key = event("duplicate", "work", {"id": "webhook-1", "type": "arrived"})
    assert first_key == event("duplicate", "work", {"id": "webhook-1", "type": "arrived"})

    reconcile(kanban_conn, "duplicate", profiles=PROFILES)
    task_id = _step("duplicate", "work")["kanban_task_id"]
    reconcile(kanban_conn, "duplicate", profiles=PROFILES)
    activation_key = advance_key("duplicate", recipe.hash, "work", 1, "running", "activate")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE key=?", (first_key,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE key=?", (activation_key,)).fetchone()[0] == 1
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key=?",
        (f"recipe/duplicate/{recipe.hash}/work/1",),
    ).fetchone()[0] == 1
    assert _step("duplicate", "work")["kanban_task_id"] == task_id
    assert created["collector_task_id"]


def test_gate_rejection_invalidates_cone_and_rebinds_approvals(tmp_path, kanban_conn):
    """§17.4: old review approvals cannot satisfy a new producer revision."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: factory.recipe/v1
id: revision-cone
version: 1
status: active
description: revision-vector test
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
  - id: qa
    primitive: review_gate
    title: QA
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: qa, execution_profile: standard, workspace: worktree}
  - id: review
    primitive: review_gate
    title: Final review
    needs: [qa]
    optional: false
    params: {seat: verifier, instructions: review, execution_profile: standard, workspace: worktree}
""",
        "revision-cone@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="revision")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    build1 = _step("revision", "build", 1)
    assert kanban_db.complete_task(kanban_conn, build1["kanban_task_id"], result="revision one")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    qa1 = _step("revision", "qa", 1)
    _complete_review(kanban_db, kanban_conn, qa1["kanban_task_id"], outcome="approve")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    review1 = _step("revision", "review", 1)
    _complete_review(kanban_db, kanban_conn, review1["kanban_task_id"], outcome="request_changes", target="build")
    reconcile(kanban_conn, "revision", profiles=PROFILES)

    build2 = _step("revision", "build", 2)
    assert build2["state"] == "running"
    assert build2["kanban_task_id"] != build1["kanban_task_id"]
    assert _step("revision", "qa", 1)["state"] == "done"
    assert _step("revision", "qa", 2)["state"] == "pending"
    assert _step("revision", "review", 2)["kanban_task_id"] is None

    assert kanban_db.complete_task(kanban_conn, build2["kanban_task_id"], result="revision two")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    qa2 = _step("revision", "qa", 2)
    assert qa2["state"] == "running"
    assert qa2["input_revision_hash"] != qa1["input_revision_hash"]
    assert _step("revision", "review", 2)["kanban_task_id"] is None
    _complete_review(kanban_db, kanban_conn, qa2["kanban_task_id"], outcome="approve")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    review2 = _step("revision", "review", 2)
    assert review2["state"] == "running"
    assert review2["input_revision_hash"] != review1["input_revision_hash"]
    assert _step("revision", "build", 1)["output_revision"] != _step("revision", "build", 2)["output_revision"]


def test_budget_fuse_charges_worked_example_without_refunds(tmp_path, kanban_conn):
    """§17.8 round-3 example: six admissions consume 300k; the seventh refuses."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: factory.recipe/v1
id: budget-loop
version: 1
status: active
description: exact budget-fuse example
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 3, max_tokens: 300000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: review
    primitive: review_gate
    title: Review
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: review, execution_profile: standard, workspace: worktree}
  - id: ship
    primitive: agent_task
    title: Ship
    needs: [review]
    optional: false
    params: {seat: release, instructions: ship, execution_profile: standard, workspace: worktree}
""",
        "budget-loop@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="budget")
    reconcile(kanban_conn, "budget", profiles=PROFILES)

    for activation in (1, 2, 3):
        build = _step("budget", "build", activation)
        assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result=f"build {activation}")
        reconcile(kanban_conn, "budget", profiles=PROFILES)
        review = _step("budget", "review", activation)
        outcome = "approve" if activation == 3 else "request_changes"
        _complete_review(
            kanban_db,
            kanban_conn,
            review["kanban_task_id"],
            outcome=outcome,
            target=None if outcome == "approve" else "build",
        )
        reconcile(kanban_conn, "budget", profiles=PROFILES)

    blocked = _step("budget", "ship", 1)
    instance = _instance("budget")
    assert blocked["state"] == "blocked"
    assert blocked["blocked_reason"] == "instance_budget"
    assert blocked["kanban_task_id"] is None
    assert instance["status"] == "blocked"
    assert instance["activation_count"] == 6
    assert instance["tokens_charged"] == 300_000
    with store._connect() as db:
        charges = db.execute(
            "SELECT COUNT(*),SUM(tokens) FROM budget_charges WHERE instance_id='budget'"
        ).fetchone()
    assert tuple(charges) == (6, 300_000)
    reconcile(kanban_conn, "budget", profiles=PROFILES)
    assert _instance("budget")["tokens_charged"] == 300_000


def test_three_day_human_gate_is_never_claimed_reclaimed_or_duplicated(tmp_path, kanban_conn, monkeypatch):
    """§17.4/§17.9: human gates remain sticky blocked across TTL maintenance."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: factory.recipe/v1
id: human-gate
version: 1
status: active
description: human wait
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 1, max_step_activations: 1, max_tokens: 1}
steps:
  - id: approve
    primitive: approval_gate
    title: Human approval
    needs: []
    optional: false
    params: {approvers: [architect], instructions: approve this revision}
""",
        "human-gate@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="human")
    reconcile(kanban_conn, "human", profiles=PROFILES)
    gate = _step("human", "approve")
    task = kanban_db.get_task(kanban_conn, gate["kanban_task_id"])
    assert (task.status, task.block_kind, task.assignee) == ("blocked", "needs_input", None)
    assert task.claim_lock is None and task.claim_expires is None and task.worker_pid is None

    original_time = time.time()
    monkeypatch.setattr(kanban_db.time, "time", lambda: original_time + 3 * 24 * 60 * 60)
    assert kanban_db.release_stale_claims(kanban_conn) == 0
    reconcile(kanban_conn, "human", profiles=PROFILES)
    assert kanban_db.release_stale_claims(kanban_conn) == 0
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key=?",
        (f"recipe/human/{recipe.hash}/approve/1",),
    ).fetchone()[0] == 1
    task = kanban_db.get_task(kanban_conn, gate["kanban_task_id"])
    assert task.status == "blocked" and task.claim_lock is None and task.worker_pid is None

    result = _recipe_gate(kanban_conn, "human", "approve", "approve", "")
    assert result["status"] == "done"
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"


def test_selector_race_guard_refuses_auto_decompose_and_accepts_disabled(monkeypatch):
    """§17.1: recipe routing and Hermes auto-decompose are mutually exclusive."""
    from hermes_cli import config as hermes_config

    config = FactoryConfig("test", {}, {}, {"enabled": True})
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"kanban": {"auto_decompose": True}})
    with pytest.raises(RuntimeError, match="auto_decompose=true"):
        startup_guard(config)
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"kanban": {"auto_decompose": False}})
    startup_guard(config)


def test_reroute_replaces_before_activation_and_cancels_after_activation(tmp_path, kanban_conn):
    """§17.10: pre-activation replacement is in-place; post-activation preserves history."""
    from hermes_cli import kanban_db

    library_path = tmp_path / "reroute-library"
    library_path.mkdir()
    for recipe_id, step_id in (("route-a", "old-work"), ("route-b", "new-work")):
        (library_path / f"{recipe_id}.yaml").write_text(
            f"""schema: factory.recipe/v1
id: {recipe_id}
version: 1
status: active
description: reroute fixture
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 1, max_tokens: 50000}}
steps:
  - id: {step_id}
    primitive: agent_task
    title: {step_id}
    needs: []
    optional: false
    params: {{seat: dev-backend, instructions: {step_id}, execution_profile: standard, workspace: worktree}}
""",
            encoding="utf-8",
        )
    library = load_library(library_path)
    pre = instantiate(kanban_conn, board="test", recipe=library.get("route-a@1"), parameters={}, instance_id="pre")
    result = _reroute(
        kanban_conn,
        argparse.Namespace(instance="pre", recipe="route-b@1", parameters="{}", library=str(library_path)),
    )
    assert result["activated"] is False
    assert result["replacement"]["instance_id"] == "pre"
    assert result["replacement"]["collector_task_id"] == pre["collector_task_id"]
    assert _instance("pre")["recipe_id"] == "route-b"
    assert _step("pre", "new-work")["activation"] == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM recipe_steps WHERE instance_id='pre'").fetchone()[0] == 1

    post = instantiate(kanban_conn, board="test", recipe=library.get("route-a@1"), parameters={}, instance_id="post")
    reconcile(kanban_conn, "post", profiles=PROFILES)
    old_task = _step("post", "old-work")["kanban_task_id"]
    kanban_db.add_comment(kanban_conn, old_task, "worker", "artifact: retained-review.txt")
    result = _reroute(
        kanban_conn,
        argparse.Namespace(instance="post", recipe="route-b@1", parameters="{}", library=str(library_path)),
    )
    replacement = result["replacement"]
    assert result["activated"] is True
    assert replacement["instance_id"] != "post"
    assert _instance("post")["status"] == "cancelled"
    assert kanban_db.get_task(kanban_conn, post["collector_task_id"]).status == "blocked"
    assert kanban_db.get_task(kanban_conn, post["collector_task_id"]).block_kind == "needs_input"
    assert kanban_db.get_task(kanban_conn, old_task).status == "archived"
    assert _step("post", "old-work")["kanban_task_id"] == old_task
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body=?",
        (old_task, "artifact: retained-review.txt"),
    ).fetchone()[0] == 1


def test_cancellation_parks_collector_without_releasing_sibling_graph(tmp_path, kanban_conn, monkeypatch):
    """§17.11/§17.14.2: atomic subtree cancel never satisfies outer dependencies."""
    from hermes_cli import kanban_db

    root, left, right = _selection_with_siblings(kanban_conn, tmp_path)
    left_task = _step("left", "work")["kanban_task_id"]
    right_task = _step("right", "work")["kanban_task_id"]
    calls = []
    real_cancel_subtree = kanban_db.cancel_subtree

    def recording_cancel_subtree(conn, task_ids, *, keep_blocked=None):
        calls.append((list(task_ids), list(keep_blocked or [])))
        return real_cancel_subtree(conn, task_ids, keep_blocked=keep_blocked)

    monkeypatch.setattr(kanban_db, "cancel_subtree", recording_cancel_subtree)
    before_right = kanban_db.get_task(kanban_conn, right_task).status
    before_root = kanban_db.get_task(kanban_conn, root).status
    result = cancel(kanban_conn, "left")
    assert result["status"] == "cancelled"
    assert calls == [([left_task, left["collector_task_id"]], [left["collector_task_id"]])]
    assert kanban_db.get_task(kanban_conn, left_task).status == "archived"
    collector = kanban_db.get_task(kanban_conn, left["collector_task_id"])
    assert (collector.status, collector.block_kind, collector.assignee) == ("blocked", "needs_input", None)
    assert kanban_db.list_events(kanban_conn, collector.id)[-1].payload["reason"] == "recipe_cancelled"
    assert kanban_db.get_task(kanban_conn, right_task).status == before_right
    assert kanban_db.get_task(kanban_conn, root).status == before_root == "blocked"

    assert kanban_db.complete_task(kanban_conn, right_task, result="right done")
    reconcile(kanban_conn, "right", profiles=PROFILES)
    assert kanban_db.get_task(kanban_conn, right["collector_task_id"]).status == "done"
    assert reconcile_root_collectors(kanban_conn) == 0
    assert kanban_db.get_task(kanban_conn, root).status == "blocked"


def test_root_collector_completes_once_after_two_sibling_successes(tmp_path, kanban_conn):
    """§17.14.1: the advancer explicitly and idempotently completes the root."""
    from hermes_cli import kanban_db

    root, left, right = _selection_with_siblings(kanban_conn, tmp_path)
    for instance_id in ("left", "right"):
        task_id = _step(instance_id, "work")["kanban_task_id"]
        assert kanban_db.complete_task(kanban_conn, task_id, result=f"{instance_id} done")
        reconcile(kanban_conn, instance_id, profiles=PROFILES)
    assert kanban_db.get_task(kanban_conn, left["collector_task_id"]).status == "done"
    assert kanban_db.get_task(kanban_conn, right["collector_task_id"]).status == "done"
    assert kanban_db.get_task(kanban_conn, root).status == "blocked"

    assert reconcile_root_collectors(kanban_conn) == 1
    assert reconcile_root_collectors(kanban_conn) == 0
    assert kanban_db.get_task(kanban_conn, root).status == "done"
    completed = [event for event in kanban_db.list_events(kanban_conn, root) if event.kind == "completed"]
    assert len(completed) == 1
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM advance_events WHERE source='root_collector' AND state='applied'"
        ).fetchone()[0] == 1


def test_advance_key_is_spec_formula():
    expected = hashlib.sha256(b"i|h|s|2|done|event-9").hexdigest()
    assert advance_key("i", "h", "s", 2, "done", "event-9") == expected


def test_gate_rejection_accepts_kanban_task_id_target(tmp_path, kanban_conn):
    """Finding #25b: review workers are prompted with kanban task ids, so
    verdicts routinely cite t_<id> of the build task instead of the recipe
    step id. The advancer must resolve it instead of fusing the gate."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: factory.recipe/v1
id: task-id-target
version: 1
status: active
description: verdict targets a kanban task id
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
  - id: qa
    primitive: review_gate
    title: QA
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: qa, execution_profile: standard, workspace: worktree}
""",
        "task-id-target@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="tid")
    reconcile(kanban_conn, "tid", profiles=PROFILES)
    build1 = _step("tid", "build", 1)
    assert kanban_db.complete_task(kanban_conn, build1["kanban_task_id"], result="rev one")
    reconcile(kanban_conn, "tid", profiles=PROFILES)
    qa1 = _step("tid", "qa", 1)
    # Target the build step by its KANBAN TASK id, exactly as t_10fdf585 did.
    _complete_review(kanban_db, kanban_conn, qa1["kanban_task_id"],
                     outcome="request_changes", target=build1["kanban_task_id"])
    reconcile(kanban_conn, "tid", profiles=PROFILES)
    build2 = _step("tid", "build", 2)
    assert build2["state"] == "running"
    assert _step("tid", "qa", 1)["blocked_reason"] == "changes_requested"
    assert _instance("tid")["status"] != "blocked"
