"""Adversarial regressions from the three live recipe-engine shakedowns."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipfactory import daemon, store
from shipfactory.recipes.advancer import apply_events, cancel, reconcile
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import RecipeError, load_library


PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}


def _write_recipe(path: Path, *, recipe_id: str = "finding", steps: str,
                  parameters: str = "{}", max_tokens: int = 500_000):
    path.mkdir()
    (path / f"{recipe_id}@1.yaml").write_text(
        f"""schema: shipfactory.recipe/v1
id: {recipe_id}
version: 1
status: active
description: shakedown regression
intent_tags: [test]
supersedes: null
parameters: {parameters}
budgets: {{max_activations: 10, max_step_activations: 3, max_tokens: {max_tokens}}}
steps:
{steps}
""",
        encoding="utf-8",
    )
    return load_library(path).get(f"{recipe_id}@1")


def _worker_recipe(path: Path, *, recipe_id: str = "finding"):
    return _write_recipe(
        path,
        recipe_id=recipe_id,
        steps="""  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build it, execution_profile: standard, workspace: worktree}
""",
    )


def _step(instance_id: str, step_id: str = "build") -> dict:
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1",
            (instance_id, step_id),
        ).fetchone()
    assert row is not None
    return dict(row)


def _instance(instance_id: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
    assert row is not None
    return dict(row)


def _pending_events() -> int:
    with store._connect() as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM advance_events WHERE state='pending'"
        ).fetchone()[0])


def test_finding_14_tick_reconciles_without_events(tmp_path, kanban_conn, monkeypatch):
    """A completed task advances on the daemon tick even when no hook was queued."""
    from shipfactory import config as factory_config
    from shipfactory import watchdog
    from shipfactory.recipes import advancer
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _worker_recipe(tmp_path / "library", recipe_id="tick-liveness")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="tick-liveness"
    )
    reconcile(kanban_conn, "tick-liveness", profiles=PROFILES)
    task_id = _step("tick-liveness")["kanban_task_id"]
    assert kanban_db.complete_task(kanban_conn, task_id, summary="finished without hook")
    assert _pending_events() == 0

    cfg = SimpleNamespace(
        company="test",
        recipes={
            "enabled": True,
            "dispatcher_max_in_progress": 4,
            "execution_profiles": PROFILES,
            "selector": {"enabled": False},
        },
    )
    monkeypatch.setattr(factory_config, "load_seats", lambda: cfg)
    monkeypatch.setattr(advancer, "startup_guard", lambda config: None)
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda *args, **kwargs: 0)
    monkeypatch.setattr("shipfactory.spawn.reap_finished", lambda: [])
    monkeypatch.setattr(watchdog, "tick", lambda *args, **kwargs: None)

    daemon.tick(kanban_conn, board="test")

    assert _step("tick-liveness")["state"] == "done"
    assert _instance("tick-liveness")["status"] == "done"


def test_finding_2_blocked_step_reobserves_terminal_task_without_event(tmp_path, kanban_conn):
    """A recovered worker task can finish after Factory recorded worker_blocked."""
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _worker_recipe(tmp_path / "library", recipe_id="blocked-liveness")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="blocked-liveness"
    )
    reconcile(kanban_conn, "blocked-liveness", profiles=PROFILES)
    task_id = _step("blocked-liveness")["kanban_task_id"]
    assert kanban_db.block_task(
        kanban_conn, task_id, kind="transient", reason="pre-daemon spawn failure"
    )
    reconcile(kanban_conn, "blocked-liveness", profiles=PROFILES)
    assert _step("blocked-liveness")["blocked_reason"] == "worker_blocked"

    assert kanban_db.unblock_task(kanban_conn, task_id)
    assert kanban_db.complete_task(kanban_conn, task_id, summary="recovered and finished")
    assert _pending_events() == 0
    apply_events(kanban_conn, profiles=PROFILES)

    assert _step("blocked-liveness")["state"] == "done"
    assert _instance("blocked-liveness")["status"] == "done"


def test_finding_5_missing_task_opens_fresh_activation(tmp_path, kanban_conn):
    """A Factory row pointing at an uncommitted board task heals with activation+1."""
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _worker_recipe(tmp_path / "library", recipe_id="ghost-healing")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="ghost-healing"
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='running',kanban_task_id='t_missing_ghost' "
            "WHERE instance_id='ghost-healing' AND step_id='build' AND activation=1"
        )

    reconcile(kanban_conn, "ghost-healing", profiles=PROFILES)

    healed = _step("ghost-healing")
    assert healed["activation"] == 2
    assert healed["state"] == "running"
    assert healed["kanban_task_id"] != "t_missing_ghost"
    assert kanban_db.get_task(kanban_conn, healed["kanban_task_id"]) is not None


def test_finding_6_reset_pending_ghost_uses_a_new_advance_key(tmp_path, kanban_conn):
    """Resetting a ghost row to pending cannot reuse activation one's consumed keys."""
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _worker_recipe(tmp_path / "library", recipe_id="reset-ghost")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="reset-ghost"
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='pending',kanban_task_id='t_reset_ghost' "
            "WHERE instance_id='reset-ghost' AND step_id='build' AND activation=1"
        )

    reconcile(kanban_conn, "reset-ghost", profiles=PROFILES)

    healed = _step("reset-ghost")
    assert healed["activation"] == 2
    assert healed["kanban_task_id"] != "t_reset_ghost"
    assert kanban_db.get_task(kanban_conn, healed["kanban_task_id"]) is not None
    with store._connect() as db:
        keys = db.execute(
            "SELECT key FROM advance_events WHERE instance_id='reset-ghost' AND state='applied'"
        ).fetchall()
    assert len({row["key"] for row in keys}) == len(keys)


def test_finding_6_reconciliation_sweeps_only_the_instances_board(
    tmp_path, hermetic_hermes_home
):
    """A board-A tick must not diagnose board B's perfectly real task as a ghost."""
    from hermes_cli import kanban_db

    kanban_db.create_board("board-a")
    kanban_db.create_board("board-b")
    conn_a = kanban_db.connect(board="board-a")
    conn_b = kanban_db.connect(board="board-b")
    try:
        store.init_db()
        recipe = _worker_recipe(tmp_path / "library", recipe_id="board-sweep")
        instantiate(
            conn_b, board="board-b", recipe=recipe, parameters={}, instance_id="board-b-instance"
        )
        reconcile(conn_b, "board-b-instance", profiles=PROFILES)
        before = _step("board-b-instance")

        apply_events(conn_a, profiles=PROFILES, board="board-a")

        after = _step("board-b-instance")
        assert after["activation"] == before["activation"] == 1
        assert after["kanban_task_id"] == before["kanban_task_id"]
        assert kanban_db.get_task(conn_b, after["kanban_task_id"]) is not None
    finally:
        conn_a.close()
        conn_b.close()


def test_finding_8_reactivation_drops_poisoned_workspace_fields(
    tmp_path, hermetic_hermes_home
):
    """An archived/mismatched task cannot donate a foreign workspace to its repair."""
    from hermes_cli import kanban_db

    board_a = tmp_path / "board-a-workdir"
    board_b = tmp_path / "board-b-workdir"
    board_a.mkdir()
    board_b.mkdir()
    kanban_db.create_board("board-a", default_workdir=str(board_a))
    kanban_db.create_board("board-b", default_workdir=str(board_b))
    kanban_db.set_current_board("board-a")
    conn = kanban_db.connect(board="board-b")
    try:
        store.init_db()
        recipe = _worker_recipe(tmp_path / "library", recipe_id="workspace-healing")
        instantiate(
            conn, board="board-b", recipe=recipe, parameters={}, instance_id="workspace-healing"
        )
        reconcile(conn, "workspace-healing", profiles=PROFILES)
        first = _step("workspace-healing")
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='archived',workspace_path=? WHERE id=?",
                (str(board_a), first["kanban_task_id"]),
            )

        reconcile(conn, "workspace-healing", profiles=PROFILES)

        healed = _step("workspace-healing")
        task = kanban_db.get_task(conn, healed["kanban_task_id"])
        assert healed["activation"] == 2
        assert task is not None
        assert task.workspace_path == str(board_b)
        assert task.workspace_path != str(board_a)
    finally:
        conn.close()


def test_finding_15_approval_gate_parks_with_upstream_case_file(tmp_path, kanban_conn):
    """The Waiting card is refreshed from task_runs, not stale tasks.result."""
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _write_recipe(
        tmp_path / "library",
        recipe_id="approval-evidence",
        max_tokens=300_000,
        steps="""  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build it, execution_profile: standard, workspace: worktree}
  - id: verify
    primitive: review_gate
    title: Verify
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: verify it, execution_profile: standard, workspace: worktree}
  - id: approval
    primitive: approval_gate
    title: Approve
    needs: [verify]
    optional: false
    params: {approvers: [operator], instructions: approve the evidence}
""",
    )
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="approval-evidence"
    )
    reconcile(kanban_conn, "approval-evidence", profiles=PROFILES)
    build = _step("approval-evidence", "build")
    commit = "a1b2c3d4e5f678901234567890abcdef12345678"
    assert kanban_db.complete_task(
        kanban_conn,
        build["kanban_task_id"],
        summary=f"Built the auth seam at commit {commit}; 12 passed, 1 skipped.",
        metadata={"commit_hash": commit, "tests": {"passed": 12, "skipped": 1}},
    )
    reconcile(kanban_conn, "approval-evidence", profiles=PROFILES)
    verify = _step("approval-evidence", "verify")
    verdict = "SHIPFACTORY_VERDICT: " + json.dumps(
        {"outcome": "approve", "body": "APPROVE clean pass"}, separators=(",", ":")
    )
    assert kanban_db.complete_task(
        kanban_conn,
        verify["kanban_task_id"],
        result=verdict,
        summary="Verifier approved the revision; 12 passed.",
        metadata={"verdict": "APPROVE clean pass", "tests_passed": 12},
    )
    reconcile(kanban_conn, "approval-evidence", profiles=PROFILES)

    gate = _step("approval-evidence", "approval")
    task = kanban_db.get_task(kanban_conn, gate["kanban_task_id"])
    comments = kanban_db.list_comments(kanban_conn, gate["kanban_task_id"])
    case_file = "\n".join([task.body or "", *(comment.body for comment in comments)])
    assert "Built the auth seam" in case_file
    assert commit in case_file
    assert "Verifier approved the revision" in case_file
    assert "APPROVE clean pass" in case_file
    assert "12 passed" in case_file and "1 skipped" in case_file
    assert "100000 / 300000" in case_file
    assert "build (done)" in case_file
    assert "verify (done)" in case_file
    assert "approval (waiting)" in case_file
    assert "No completed upstream summary was available" not in case_file


def test_finding_3_refused_cancel_does_not_write_cancelling_fence(tmp_path, kanban_conn):
    """Missing board ids are refused while the instance remains in its prior state."""
    store.init_db()
    recipe = _worker_recipe(tmp_path / "library", recipe_id="cancel-atomicity")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="cancel-atomicity"
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='running',kanban_task_id='t_missing_cancel' "
            "WHERE instance_id='cancel-atomicity' AND step_id='build' AND activation=1"
        )

    result = cancel(kanban_conn, "cancel-atomicity")

    assert result["refused"] == "unknown task id(s): t_missing_cancel"
    assert result["status"] == "running"
    assert _instance("cancel-atomicity")["status"] == "running"


def test_finding_7_templated_seat_is_validated_after_binding(tmp_path, kanban_conn):
    """Publication accepts roster templates; instantiation rejects a bad bound seat."""
    store.init_db()
    library_path = tmp_path / "library"
    library_path.mkdir()
    (library_path / "templated-seat@1.yaml").write_text(
        """schema: shipfactory.recipe/v1
id: templated-seat
version: 1
status: active
description: template validation regression
intent_tags: [test]
supersedes: null
parameters:
  assignee_seat: {type: string, required: true, default: null}
  runtime_profile: {type: string, required: true, default: null}
budgets: {max_activations: 2, max_step_activations: 2, max_tokens: 100000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params:
      seat: "${assignee_seat}"
      instructions: build it
      execution_profile: "${runtime_profile}"
      workspace: worktree
""",
        encoding="utf-8",
    )
    recipe = load_library(
        library_path, seats={"dev-backend"}, profiles={"standard"}
    ).get("templated-seat@1")

    with pytest.raises(RecipeError, match="unknown seat 'intruder'"):
        instantiate(
            kanban_conn,
            board="test",
            recipe=recipe,
            parameters={"assignee_seat": "intruder", "runtime_profile": "standard"},
            instance_id="bad-template",
        )
    with pytest.raises(RecipeError, match="unknown profile 'oversized'"):
        instantiate(
            kanban_conn,
            board="test",
            recipe=recipe,
            parameters={"assignee_seat": "dev-backend", "runtime_profile": "oversized"},
            instance_id="bad-profile-template",
        )

    instantiate(
        kanban_conn,
        board="test",
        recipe=recipe,
        parameters={"assignee_seat": "dev-backend", "runtime_profile": "standard"},
        instance_id="good-template",
    )
    reconcile(kanban_conn, "good-template", profiles=PROFILES)
    from hermes_cli import kanban_db
    good = _step("good-template")
    task = kanban_db.get_task(kanban_conn, good["kanban_task_id"])
    assert good["state"] == "running"
    assert task is not None and task.assignee == "dev-backend"


def test_finding_10_database_health_failure_is_loud_and_best_effort(
    monkeypatch, caplog
):
    """Checkpoint/quick-check failures emit telemetry but never abort dispatch."""
    from shipfactory import watchdog
    from shipfactory import telemetry
    from hermes_cli import kanban_db

    class BrokenHealthConnection:
        def execute(self, statement):
            if statement.startswith("PRAGMA"):
                raise RuntimeError("simulated external-volume I/O failure")
            raise AssertionError(statement)

    records = []
    monkeypatch.setattr(daemon, "_DB_HEALTH_EVERY_TICKS", 1)
    monkeypatch.setattr(daemon, "_db_health_tick", 0)
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda *args, **kwargs: "dispatched")
    monkeypatch.setattr("shipfactory.spawn.reap_finished", lambda: [])
    monkeypatch.setattr(watchdog, "tick", lambda *args, **kwargs: None)
    monkeypatch.setattr(telemetry, "append_jsonl", records.append)
    caplog.set_level(logging.ERROR)

    result = daemon.tick(BrokenHealthConnection(), board="external")

    assert result["dispatch"] == "dispatched"
    assert records and records[0]["event"] == "database_health_failure"
    assert records[0]["board"] == "external"
    assert "simulated external-volume I/O failure" in caplog.text


def test_finding_13_executor_discipline_completes_handoff_to_review():
    template = (
        Path(__file__).resolve().parents[1] / "recipes/templates/executor-discipline.md"
    ).read_text(encoding="utf-8")
    assert (
        "Completing your kanban task IS the handoff to review — do NOT block "
        "your task for review yourself."
    ) in template


def test_finding_11_instantiation_anchors_tasks_to_explicit_board(
    tmp_path, hermetic_hermes_home
):
    """Global board A cannot stamp its workdir onto an instance created on board B."""
    from hermes_cli import kanban_db

    board_a = tmp_path / "foreign-workdir"
    board_b = tmp_path / "instance-workdir"
    board_a.mkdir()
    board_b.mkdir()
    kanban_db.create_board("board-a", default_workdir=str(board_a))
    kanban_db.create_board("board-b", default_workdir=str(board_b))
    kanban_db.set_current_board("board-a")
    conn = kanban_db.connect(board="board-b")
    try:
        store.init_db()
        recipe = _worker_recipe(tmp_path / "library", recipe_id="multi-board")
        instantiate(
            conn, board="board-b", recipe=recipe, parameters={}, instance_id="multi-board"
        )
        reconcile(conn, "multi-board", profiles=PROFILES)
        task = kanban_db.get_task(conn, _step("multi-board")["kanban_task_id"])
        assert task is not None
        assert task.workspace_path == str(board_b)
        assert task.workspace_path != str(board_a)
    finally:
        conn.close()
