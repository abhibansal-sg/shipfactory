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


def _notify_recipe(path: Path, *, recipe_id: str, target: str = "${notify_target}"):
    return _write_recipe(
        path,
        recipe_id=recipe_id,
        parameters="{notify_target: {type: string, required: false, default: telegram:home}}",
        steps=f"""  - id: notify
    primitive: notify
    title: Notify
    needs: []
    optional: false
    params: {{target: "{target}", message: notify test}}
""",
    )


def _instantiation_side_effects(conn) -> tuple[int, int, int]:
    tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with store._connect() as db:
        instances = db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0]
        steps = db.execute("SELECT COUNT(*) FROM recipe_steps").fetchone()[0]
    return tasks, instances, steps


def test_finding_91_cross_wired_existing_boards_refuse_before_side_effects(
    tmp_path, hermetic_hermes_home
):
    """An existing requested board cannot use another existing board's connection."""
    from hermes_cli import kanban_db

    kanban_db.create_board("board-a")
    kanban_db.create_board("board-b")
    conn_a = kanban_db.connect(board="board-a")
    try:
        store.init_db()
        recipe = _worker_recipe(tmp_path / "library", recipe_id="cross-wired")
        with pytest.raises(ValueError, match=r"requested board 'board-b'.*connection board 'board-a'"):
            instantiate(conn_a, board="board-b", recipe=recipe, parameters={}, instance_id="cross-wired")
        assert _instantiation_side_effects(conn_a) == (0, 0, 0)
    finally:
        conn_a.close()


def test_finding_91_matching_and_absent_board_labels_remain_compatible(
    tmp_path, kanban_conn, hermetic_hermes_home
):
    """Matching stock paths work; absent board labels keep their connection authority."""
    from hermes_cli import kanban_db

    kanban_db.create_board("matching")
    matching = kanban_db.connect(board="matching")
    try:
        store.init_db()
        recipe = _worker_recipe(tmp_path / "matching-library", recipe_id="matching")
        result = instantiate(matching, board="matching", recipe=recipe, parameters={}, instance_id="matching")
        assert result["instance_id"] == "matching"
        assert _instantiation_side_effects(matching) == (1, 1, 1)
    finally:
        matching.close()

    recipe = _worker_recipe(tmp_path / "absent-library", recipe_id="absent-label")
    result = instantiate(kanban_conn, board="not-created", recipe=recipe, parameters={}, instance_id="absent-label")
    assert result["instance_id"] == "absent-label"


def test_finding_92_notify_target_validation_is_fail_fast_and_structural(tmp_path, kanban_conn):
    store.init_db()
    invalid = _notify_recipe(tmp_path / "invalid-library", recipe_id="invalid-notify")
    with pytest.raises(ValueError, match=r"'telegram'.*'notify_target'"):
        instantiate(
            kanban_conn, board="test", recipe=invalid,
            parameters={"notify_target": "telegram"}, instance_id="invalid-notify",
        )
    assert _instantiation_side_effects(kanban_conn) == (0, 0, 0)

    valid = _notify_recipe(tmp_path / "valid-library", recipe_id="valid-notify")
    result = instantiate(
        kanban_conn, board="test", recipe=valid,
        parameters={"notify_target": "telegram:home"}, instance_id="valid-notify",
    )
    assert result["instance_id"] == "valid-notify"

    no_notify = _worker_recipe(tmp_path / "no-notify-library", recipe_id="no-notify")
    result = instantiate(kanban_conn, board="test", recipe=no_notify, parameters={}, instance_id="no-notify")
    assert result["instance_id"] == "no-notify"


def _v2_verify_recipe(path: Path, *, build_cap: int = 3):
    path.mkdir()
    (path / "verified@1.yaml").write_text(
        f"""schema: shipfactory.recipe/v2
id: verified
version: 1
status: active
description: finding 95 regression fixture
intent_tags: [test]
supersedes: null
verdict_contract: shipfactory.verdict/v2
parameters: {{}}
budgets:
  max_activations: 12
  step_activation_caps:
    build: {build_cap}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    inputs: []
    outputs:
      - {{kind: change-set, schema: shipfactory.change-set/v1, path: .shipfactory-output/change-set.json}}
    params: {{seat: dev-backend, instructions: build it, execution_profile: standard, workspace: worktree, access_mode: workspace_write, environment: source}}
  - id: verify
    primitive: verification
    title: Verify
    needs: [build]
    optional: false
    inputs:
      - {{from: build, kind: change-set, required: true}}
    outputs:
      - {{kind: evidence-bundle, schema: shipfactory.evidence/v1, path: .shipfactory-output/evidence-manifest.json}}
    params:
      manifest: .shipfactory/verification.yaml
      profile: browser-standard
      environment: app
""",
        encoding="utf-8",
    )
    return load_library(
        path, verification_profiles={"browser-standard"},
    ).get("verified@1")


def _simulate_failed_verification(instance_id: str, tmp_path: Path, *,
                                  reason: str = "test_failed") -> None:
    log = tmp_path / "pytest-tail.log"
    log.write_text(
        "FAILED tests/test_alpha.py::test_broken - AssertionError\n"
        "2 failed, 500 passed in 100.00s\n",
        encoding="utf-8",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='running_verification' "
            "WHERE instance_id=? AND step_id='verify' AND activation=1",
            (instance_id,),
        )
        db.execute(
            "INSERT INTO evidence_bundles(id, instance_id, step_id, activation, "
            "input_revision_hash, base_sha, head_sha, tree_sha, manifest_relpath, "
            "manifest_blob_sha, state, invalid_reason, redaction_state, created_at, "
            "environment_identity_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"bundle-{instance_id}", instance_id, "verify", 1,
             "rev", "a" * 40, "b" * 40, "c" * 40, ".shipfactory/verification.yaml",
             "d" * 40, "blocked", reason, "not_required", store._now(), "{}"),
        )
        db.execute(
            "INSERT INTO evidence_items(id, bundle_id, case_id, kind, path, sha256, "
            "size_bytes, producer, metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"item-{instance_id}", f"bundle-{instance_id}", "protected-pytest", "log",
             str(log), "0" * 64, log.stat().st_size, "runner", "{}"),
        )


def test_finding_95_test_failed_verification_reworks_build_with_feedback(
    tmp_path, kanban_conn, monkeypatch,
):
    """A deterministic verification failure routes a rework cone to the builder
    with the failing oracle lines in the new task body, instead of parking."""
    from hermes_cli import kanban_db
    from shipfactory import verification

    store.init_db()
    monkeypatch.setattr(verification, "verify_evidence_bundle", lambda *a, **k: None)
    recipe = _v2_verify_recipe(tmp_path / "library")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="f95",
    )
    reconcile(kanban_conn, "f95", profiles=PROFILES)
    build = _step("f95", "build")
    assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result="built")
    _simulate_failed_verification("f95", tmp_path)

    reconcile(kanban_conn, "f95", profiles=PROFILES)

    verify = _step("f95", "verify")
    rework = _step("f95", "build")
    assert rework["activation"] == 2
    assert rework["rejected_by_step_id"] == "verify"
    assert rework["rejected_by_activation"] == 1
    assert _instance("f95")["status"] == "running"
    with store._connect() as db:
        old_verify = db.execute(
            "SELECT state, blocked_reason FROM recipe_steps "
            "WHERE instance_id='f95' AND step_id='verify' AND activation=1",
        ).fetchone()
    assert (old_verify["state"], old_verify["blocked_reason"]) == ("blocked", "changes_requested")
    task = kanban_db.get_task(kanban_conn, rework["kanban_task_id"])
    assert "Verification failure feedback" in task.body
    assert "FAILED tests/test_alpha.py::test_broken" in task.body
    assert "2 failed, 500 passed" in task.body


def test_finding_95_exhausted_build_cap_parks_for_the_operator(
    tmp_path, kanban_conn, monkeypatch,
):
    """When the change-set producer's activation cap is spent, a test_failed
    verification parks exactly as before finding #95."""
    from hermes_cli import kanban_db
    from shipfactory import verification

    store.init_db()
    monkeypatch.setattr(verification, "verify_evidence_bundle", lambda *a, **k: None)
    recipe = _v2_verify_recipe(tmp_path / "library", build_cap=1)
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="f95cap",
    )
    reconcile(kanban_conn, "f95cap", profiles=PROFILES)
    build = _step("f95cap", "build")
    assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result="built")
    _simulate_failed_verification("f95cap", tmp_path)

    reconcile(kanban_conn, "f95cap", profiles=PROFILES)

    assert _step("f95cap", "build")["activation"] == 1
    verify = _step("f95cap", "verify")
    assert (verify["state"], verify["blocked_reason"]) == ("blocked", "test_failed")
    assert _instance("f95cap")["status"] == "blocked"


def test_finding_95_infrastructure_failures_keep_parking(
    tmp_path, kanban_conn, monkeypatch,
):
    """Only deterministic candidate defects rework; infra/baseline failures park."""
    from shipfactory import verification
    from hermes_cli import kanban_db

    store.init_db()
    monkeypatch.setattr(verification, "verify_evidence_bundle", lambda *a, **k: None)
    recipe = _v2_verify_recipe(tmp_path / "library")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="f95infra",
    )
    reconcile(kanban_conn, "f95infra", profiles=PROFILES)
    build = _step("f95infra", "build")
    assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result="built")
    _simulate_failed_verification(
        "f95infra", tmp_path, reason="protected_baseline_test_failed",
    )

    reconcile(kanban_conn, "f95infra", profiles=PROFILES)

    assert _step("f95infra", "build")["activation"] == 1
    verify = _step("f95infra", "verify")
    assert (verify["state"], verify["blocked_reason"]) == (
        "blocked", "protected_baseline_test_failed",
    )
    assert _instance("f95infra")["status"] == "blocked"


def test_finding_99_operator_retry_reuses_exact_sealed_producer_without_llm_rebuild(
    tmp_path, kanban_conn, monkeypatch,
):
    """An audited retry skips only the empty rework and re-verifies its exact prior candidate."""
    from shipfactory import verification
    from shipfactory.recipes import advancer

    store.init_db()
    monkeypatch.setattr(verification, "verify_evidence_bundle", lambda *a, **k: None)
    recipe = _v2_verify_recipe(tmp_path / "library")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="f99",
    )
    reconcile(kanban_conn, "f99", profiles=PROFILES)
    build1 = _step("f99", "build")
    run_id = store.record_run_start(
        build1["kanban_task_id"], "dev-backend", "codex", "gpt-test",
        workspace_path=str(tmp_path / "candidate"), recipe_activation=1,
    )
    kanban_conn.execute(
        "UPDATE tasks SET workspace_kind='worktree',workspace_path=? WHERE id=?",
        (str(tmp_path / "candidate"), build1["kanban_task_id"]),
    )
    store.record_run_end(run_id, 0, 1, 1, 0.1, "done")
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='done',producer_run_id=?,updated_at=? "
            "WHERE instance_id='f99' AND step_id='build' AND activation=1",
            (run_id, store._now()),
        )
        db.execute(
            "INSERT INTO artifacts(id,instance_id,step_id,activation,run_id,kind,"
            "schema_version,state,sha256,size_bytes,producer,base_sha,head_sha,repo_tree_sha,"
            "created_at,sealed_at) VALUES(?,?,?,?,?,'change-set',1,'sealed',?,?,?, ?,?,?,?,?)",
            ("artifact-f99", "f99", "build", 1, run_id, "a" * 64, 1, "factory",
             "a" * 40, "b" * 40, "c" * 40, store._now(), store._now()),
        )
    _simulate_failed_verification("f99", tmp_path)
    reconcile(kanban_conn, "f99", profiles=PROFILES)
    build2 = _step("f99", "build")
    verify2 = _step("f99", "verify")
    assert (build2["activation"], verify2["activation"]) == (2, 2)
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='blocked',blocked_reason='worker_blocked',updated_at=? "
            "WHERE instance_id='f99' AND step_id='build' AND activation=2",
            (store._now(),),
        )
        db.execute(
            "UPDATE recipe_instances SET status='blocked',blocked_reason='worker_blocked',updated_at=? "
            "WHERE id='f99'",
            (store._now(),),
        )

    key = advancer.retry_verification(
        "f99", "verify", producer_step="build", producer_activation=1,
        reason="pytest runtime was unavailable; rerun the sealed candidate",
    )
    row = advancer._claim_event(owner="test-f99", board="test")
    assert row and row["key"] == key
    advancer._apply_claimed_event(kanban_conn, row)

    assert (_step("f99", "build")["activation"], _step("f99", "build")["state"]) == (2, "skipped")
    assert (_step("f99", "verify")["activation"], _step("f99", "verify")["state"]) == (2, "pending")
    assert _instance("f99")["status"] == "running"
    with store._connect() as db:
        latest = {item["step_id"]: item for item in advancer._latest(db, "f99")}
        verify_definition = next(item for item in recipe.document["steps"] if item["id"] == "verify")
        workspace, owner = advancer._step_change_set_workspace(
            kanban_conn, db, "f99", latest, verify_definition,
        )
        event = db.execute("SELECT state,outcome,payload_json FROM advance_events WHERE key=?", (key,)).fetchone()
    assert (workspace, owner) == (str(tmp_path / "candidate"), build1["kanban_task_id"])
    # Never fall through to the later no-op activation if the sealed producer's
    # owner workspace disappears; that would verify different bytes.
    kanban_conn.execute(
        "UPDATE tasks SET workspace_path=NULL WHERE id=?", (build1["kanban_task_id"],),
    )
    build2 = _step("f99", "build")
    kanban_conn.execute(
        "UPDATE tasks SET workspace_kind='worktree',workspace_path=? WHERE id=?",
        (str(tmp_path / "wrong-candidate"), build2["kanban_task_id"]),
    )
    with store._connect() as db:
        latest = {item["step_id"]: item for item in advancer._latest(db, "f99")}
        assert advancer._step_change_set_workspace(
            kanban_conn, db, "f99", latest, verify_definition,
        ) == (None, None)
    assert (event["state"], event["outcome"]) == ("applied", "verification_retry_scheduled")
    payload = json.loads(event["payload_json"])
    assert payload["producer_run_id"] == run_id
    assert payload["artifact_id"] == "artifact-f99"

    # A failed candidate case followed by a passing protected case on the exact
    # same commit, command, cwd, oracle, and environment is nondeterministic
    # verifier evidence, not proof of a candidate defect. One separately capped
    # machine-only retry gets a fresh activation; the failed bundle stays sealed.
    kanban_conn.execute(
        "UPDATE tasks SET workspace_path=? WHERE id=?",
        (str(tmp_path / "candidate"), build1["kanban_task_id"]),
    )
    now = store._now()
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='blocked',blocked_reason='test_failed',updated_at=? "
            "WHERE instance_id='f99' AND step_id='verify' AND activation=2",
            (now,),
        )
        db.execute(
            "INSERT INTO evidence_bundles(id,instance_id,step_id,activation,input_revision_hash,"
            "base_sha,head_sha,tree_sha,environment_session_id,manifest_relpath,manifest_blob_sha,"
            "state,bundle_sha256,redaction_state,created_at,sealed_at,invalid_reason,phase_b_eligible) "
            "VALUES('bundle-f99-contradiction','f99','verify',2,'i',?,?,?,?,'.shipfactory/verification.yaml',?,"
            "'blocked',?,'clean',?,?,'test_failed',0)",
            ("b" * 40, "b" * 40, "c" * 40, "env-f99", "m" * 40,
             "d" * 64, now, now),
        )
        oracle = json.dumps({"type": "pytest_summary"}, sort_keys=True)
        requirements = json.dumps(["tests"], sort_keys=True)
        for case_id, status in (
            ("protected-pytest", "failed"),
            ("protected:protected-pytest", "passed"),
        ):
            db.execute(
                "INSERT INTO verification_cases(bundle_id,case_id,attempt,requirement_ids_json,"
                "oracle_type,oracle_json,status,evidence_item_ids_json,started_at,ended_at) "
                "VALUES('bundle-f99-contradiction',?,1,?,'pytest_summary',?,?,?, ?,?)",
                (case_id, requirements, oracle, status, "[]", now, now),
            )
            db.execute(
                "INSERT INTO evidence_items(id,bundle_id,case_id,kind,path,sha256,size_bytes,"
                "mime_type,producer,command_json,cwd_relpath,env_digest,exit_code,started_at,"
                "ended_at,metadata_json) VALUES(?,?,?,'log',?,?,1,'text/plain','runner',"
                "?,'.',?, ?,?,?, '{}')",
                (f"item-{case_id}", "bundle-f99-contradiction", case_id, f"{case_id}.log", "e" * 64,
                 json.dumps(["python", "-m", "pytest", "tests/"]), "env-digest",
                 1 if status == "failed" else 0, now, now),
            )
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_items SET env_digest='different-environment' "
            "WHERE id='item-protected:protected-pytest'",
        )
    with pytest.raises(ValueError, match="neither a blocked verification rework"):
        advancer.retry_verification(
            "f99", "verify", producer_step="build", producer_activation=1,
            reason="must not retry non-identical execution",
        )
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_items SET env_digest='env-digest' "
            "WHERE id='item-protected:protected-pytest'",
        )
    retry_key = advancer.retry_verification(
        "f99", "verify", producer_step="build", producer_activation=1,
        reason="identical candidate/protected execution contradicted itself",
    )
    retry_row = advancer._claim_event(owner="test-f99-contradiction", board="test")
    assert retry_row and retry_row["key"] == retry_key
    advancer._apply_claimed_event(kanban_conn, retry_row)
    assert (_step("f99", "verify")["activation"], _step("f99", "verify")["state"]) == (3, "pending")
    with store._connect() as db:
        contradiction_event = db.execute(
            "SELECT state,outcome,payload_json FROM advance_events WHERE key=?", (retry_key,),
        ).fetchone()
        preserved = db.execute(
            "SELECT state,invalid_reason FROM evidence_bundles WHERE id='bundle-f99-contradiction'",
        ).fetchone()
    assert (contradiction_event["state"], contradiction_event["outcome"]) == (
        "applied", "verification_retry_contradiction_scheduled",
    )
    assert json.loads(contradiction_event["payload_json"])["mode"] == "contradiction"
    assert (preserved["state"], preserved["invalid_reason"]) == ("blocked", "test_failed")


def test_finding_97_recipe_worktree_aligns_to_instance_base(tmp_path):
    """A recipe task's worktree cut from live HEAD is detached onto the
    instance's current base before the worker starts; non-recipe tasks and
    missing bases are untouched; an unreachable base fails the spawn loudly."""
    import os
    import subprocess as sp
    from shipfactory.spawn import _align_recipe_workspace_base

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(["git", "commit", "-qm", "base"], cwd=repo, env=env, check=True)
    base = sp.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "f.txt").write_text("newer\n", encoding="utf-8")
    sp.run(["git", "commit", "-aqm", "newer"], cwd=repo, env=env, check=True)
    head = sp.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    wt = tmp_path / "wt"
    sp.run(["git", "worktree", "add", "-q", "--detach", str(wt), head], cwd=repo, check=True)

    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_instances(id, board, recipe_id, recipe_version, recipe_hash, "
            "parameters_json, collector_task_id, status, base_sha, created_at, updated_at) "
            "VALUES('f97','test','r',1,'h','{}','t_c','running',?,datetime('now'),datetime('now'))",
            (base,),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id, step_id, activation, primitive, state, "
            "kanban_task_id, created_at, updated_at) "
            "VALUES('f97','build',2,'agent_task','running','t_f97',datetime('now'),datetime('now'))",
        )

    _align_recipe_workspace_base("t_f97", wt)
    assert sp.check_output(["git", "rev-parse", "HEAD"], cwd=wt, text=True).strip() == base

    # Idempotent on a second call; unknown task untouched.
    _align_recipe_workspace_base("t_f97", wt)
    _align_recipe_workspace_base("t_unknown", wt)
    assert sp.check_output(["git", "rev-parse", "HEAD"], cwd=wt, text=True).strip() == base

    # Unreachable base (foreign repo / fixture default): best-effort skip —
    # the worker proceeds on worktree HEAD and sealing guards lineage.
    with store._connect() as db:
        db.execute("UPDATE recipe_instances SET base_sha=? WHERE id='f97'", ("e" * 40,))
    _align_recipe_workspace_base("t_f97", wt)
    assert sp.check_output(["git", "rev-parse", "HEAD"], cwd=wt, text=True).strip() == base
