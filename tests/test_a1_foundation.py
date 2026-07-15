"""A1 durable-run, resource, budget, usage, watchdog, and reap regressions."""

from __future__ import annotations

import subprocess
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipfactory import daemon, store, watchdog
import shipfactory.spawn as spawn
from shipfactory.recipes import advancer


def _run(task_id: str, *, board: str = "restart", log_path: Path | None = None) -> int:
    run_id = store.record_run_start(
        task_id, "dev", "codex", "gpt", None, board=board,
        workspace_path="/tmp/work", log_path=log_path, prompt_path="/tmp/prompt",
        provider="codex", resolved_model="gpt", executor_version="1",
    )
    store.record_run_spawned(run_id, 4242, "start-token")
    assert store.acquire_resource_lease(
        "worker_slot", 2, key=f"worker_slot:run:{run_id}",
        metadata={"run_id": run_id, "task_id": task_id, "board": board},
    )
    return run_id


def test_restart_reconstructs_live_worker_then_reaps_completion(tmp_path, monkeypatch):
    log = tmp_path / "worker.log"
    log.write_text("SHIPFACTORY_RESULT: done restored worker finished\n")
    run_id = _run("live-worker", log_path=log)
    alive = {"value": True}
    monkeypatch.setattr(
        spawn, "_process_start_token",
        lambda pid: "start-token" if alive["value"] else None,
    )
    spawn._RUNNING.clear()

    restored = spawn.restore_running(max_workers=2)
    assert restored == {"restored": [4242], "crashed": []}
    assert spawn._RUNNING[4242]["run_id"] == run_id

    alive["value"] = False
    outcome = spawn.reap_finished()
    assert outcome[0]["result"] == "done"
    assert store.run_row(run_id)["ended_at"] is not None
    assert 4242 not in spawn._RUNNING


def test_restart_marks_dead_worker_crashed_and_journals_reconciliation(monkeypatch):
    run_id = _run("dead-worker")
    monkeypatch.setattr(spawn, "_process_start_token", lambda pid: None)
    spawn._RUNNING.clear()

    restored = spawn.restore_running(max_workers=2)

    assert restored == {"restored": [], "crashed": [run_id]}
    assert store.run_row(run_id)["result"].startswith("crashed:")
    with store._connect() as db:
        intent = db.execute(
            "SELECT state,last_error FROM action_intents "
            "WHERE kind='worker_task_transition'"
        ).fetchone()
    assert intent is not None


def test_dispatch_worker_slot_capacity_queues_second_task(kanban_conn, monkeypatch):
    from hermes_cli import kanban_db
    from hermes_cli import profiles

    cfg = SimpleNamespace(company="cap", seats={}, recipes={"max_workers": 1})
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda **kwargs: cfg)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(spawn, "restore_running", lambda **kwargs: {"restored": [], "crashed": []})
    monkeypatch.setattr(spawn, "reap_finished", lambda: [])
    spawned: list[str] = []

    def governed(task, workspace, *, board=None):
        key = f"slot:{task.id}"
        assert store.acquire_resource_lease("worker_slot", 1, key=key)
        spawned.append(task.id)
        return 7000 + len(spawned)

    monkeypatch.setattr(spawn, "shipfactory_spawn", governed)
    first = kanban_db.create_task(kanban_conn, title="first", assignee="dev")
    second = kanban_db.create_task(kanban_conn, title="second", assignee="dev")

    daemon.tick(kanban_conn, board="cap")
    assert spawned == [first]

    assert kanban_db.complete_task(kanban_conn, first, summary="finished")
    assert store.release_resource_lease(f"slot:{first}")
    daemon.tick(kanban_conn, board="cap")
    assert spawned == [first, second]


def test_mid_dispatch_capacity_exhaustion_requeues_without_failure(kanban_conn, monkeypatch):
    """Hermes runtime enforcement may free a board slot before Factory reaps its lease."""
    from hermes_cli import kanban_db
    from hermes_cli import profiles
    from shipfactory import config

    seat = SimpleNamespace(executor="codex", model="gpt", profile="dev")
    cfg = SimpleNamespace(company="cap-race", seats={"dev": seat}, recipes={"max_workers": 1})
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda **kwargs: cfg)
    monkeypatch.setattr(config, "load_seats", lambda: cfg)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(spawn, "restore_running", lambda **kwargs: {"restored": [], "crashed": []})
    monkeypatch.setattr(spawn, "reap_finished", lambda: [])

    timed_out = kanban_db.create_task(
        kanban_conn, title="times out", assignee="dev", priority=0,
        max_runtime_seconds=1,
    )
    healthy = kanban_db.create_task(
        kanban_conn, title="healthy queue target", assignee="dev", priority=10,
    )
    claimed = kanban_db.claim_task(kanban_conn, timed_out)
    assert claimed is not None
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute(
            "UPDATE tasks SET worker_pid=99999999 WHERE id=?", (timed_out,),
        )
        kanban_conn.execute(
            "UPDATE task_runs SET worker_pid=99999999,started_at=0 WHERE id=?",
            (claimed.current_run_id,),
        )
    assert store.acquire_resource_lease("worker_slot", 1, key="stale-worker-slot")

    result = daemon.tick(kanban_conn, board="cap-race")

    task = kanban_db.get_task(kanban_conn, healthy)
    assert timed_out in result["dispatch"].timed_out
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert not result["dispatch"].auto_blocked
    assert all(item[0] != healthy for item in result["dispatch"].spawned)
    assert (healthy, "worker_slot_capacity") in result["dispatch"].respawn_guarded

    daemon.tick(kanban_conn, board="cap-race")
    retried = kanban_db.get_task(kanban_conn, healthy)
    assert retried.status == "ready"
    assert retried.consecutive_failures == 0


def test_spawn_registration_failure_terminates_process_and_releases_lease(tmp_path, monkeypatch):
    from hermes_cli import kanban_db
    from shipfactory import config

    seat = SimpleNamespace(executor="hermes", model="native", profile="dev")
    cfg = SimpleNamespace(seats={"dev": seat}, recipes={"max_workers": 1})
    monkeypatch.setattr(config, "load_seats", lambda: cfg)
    monkeypatch.setattr(kanban_db, "_default_spawn", lambda *args, **kwargs: 8123)
    monkeypatch.setattr(spawn, "_capture_start_token", lambda *args, **kwargs: "token")
    killed = []
    monkeypatch.setattr(spawn.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(
        store, "record_run_spawned",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("registration failed")),
    )

    with pytest.raises(RuntimeError, match="registration failed"):
        spawn.shipfactory_spawn(
            SimpleNamespace(id="registration-failure", assignee="dev", current_run_id=41),
            str(tmp_path / "work"), board="native",
        )

    row = store.run_row(1)
    assert killed == [(8123, 15)]
    assert row["result"].startswith("crashed:")
    assert store.active_resource_units("worker_slot") == 0
    assert 8123 not in spawn._RUNNING


def test_start_identity_waits_past_five_fast_misses(monkeypatch):
    observations = iter([None] * 7 + ["late-token"])
    monkeypatch.setattr(spawn, "_process_start_token", lambda pid: next(observations))
    monkeypatch.setattr(spawn, "sleep", lambda delay: None)

    assert spawn._capture_start_token(99) == "late-token"


def test_missing_start_identity_records_nullable_reconcilable_run(tmp_path, monkeypatch):
    from hermes_cli import kanban_db
    from shipfactory import config

    seat = SimpleNamespace(executor="hermes", model="native", profile="dev")
    cfg = SimpleNamespace(seats={"dev": seat}, recipes={"max_workers": 1})
    monkeypatch.setattr(config, "load_seats", lambda: cfg)
    monkeypatch.setattr(kanban_db, "_default_spawn", lambda *args, **kwargs: 8234)
    monkeypatch.setattr(spawn, "_capture_start_token", lambda *args, **kwargs: None)

    pid = spawn.shipfactory_spawn(
        SimpleNamespace(id="nullable-token", assignee="dev", current_run_id=52),
        str(tmp_path / "work"), board="native",
    )

    row = store.nonterminal_runs()[0]
    assert pid == 8234
    assert row["process_start_token"] is None
    assert row["task_attempt_id"] == 52
    assert spawn._RUNNING[pid]["process_start_token"] is None

    spawn._RUNNING.clear()
    monkeypatch.setattr(spawn, "_pid_alive", lambda candidate: candidate == pid)
    restored = spawn.restore_running(max_workers=1)
    assert restored == {"restored": [pid], "crashed": []}
    assert spawn._RUNNING[pid]["run_id"] == row["id"]

    spawn._RUNNING.clear()
    store.record_run_crashed(row["id"], "test cleanup")
    store.release_resource_lease(f"worker_slot:run:{row['id']}")


def test_daemon_passes_config_ceiling_and_ignores_environment(kanban_conn, monkeypatch):
    from hermes_cli import kanban_db

    cfg = SimpleNamespace(company="budget", recipes={
        "enabled": True,
        "dispatcher_max_in_progress": 2,
        "board_day_token_ceiling": 1234,
        "execution_profiles": {"standard": {}},
        "selector": {"enabled": False},
    })
    monkeypatch.setenv("FACTORY_BOARD_DAY_TOKEN_CEILING", "1")
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda **kwargs: cfg)
    monkeypatch.setattr(spawn, "restore_running", lambda **kwargs: {"restored": [], "crashed": []})
    monkeypatch.setattr(spawn, "reap_finished", lambda: [])
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda *args, **kwargs: None)
    captured = []

    def apply(conn, *, profiles, board, board_day_token_ceiling):
        captured.append(board_day_token_ceiling)
        return 0

    monkeypatch.setattr(advancer, "apply_events", apply)
    monkeypatch.setattr(advancer, "deliver_outbox", lambda *args, **kwargs: 0)
    monkeypatch.setattr(advancer, "reconcile_root_collectors", lambda *args, **kwargs: 0)

    daemon.tick(kanban_conn, board="budget")
    assert captured == [1234]


def test_missing_usage_stays_null_and_rollup_reports_unknown():
    unknown = store.record_run_start("unknown", "dev", "codex", "gpt", 1)
    known = store.record_run_start("known", "dev", "codex", "gpt", 2)
    store.record_run_end(unknown, 0, None, None, 1.0, "done")
    store.record_run_end(known, 0, 0, 0, 1.0, "done")

    assert store.run_row(unknown)["tokens_total"] is None
    rollup = store.costs_rollup("seat", 1)[0]
    assert rollup["tokens_total"] == 0
    assert rollup["usage_unknown_runs"] == 1
    assert rollup["usage_known_runs"] == 1


def test_watchdog_subprocess_timeout_is_recorded_and_bounded(monkeypatch):
    store.add_monitor(
        "hung-monitor", "2026-07-15T00:00:00+00:00", None, 2,
        "wake_owner", "check it", "qa", 60,
    )

    def hangs(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(watchdog.subprocess, "run", hangs)
    outcomes = watchdog.tick(
        "demo", "2026-07-15T00:00:00Z",
        command_timeout_seconds=0.01, tick_timeout_seconds=0.05,
    )

    assert outcomes[0]["action"] == "timed_out"
    with store._connect() as db:
        row = db.execute(
            "SELECT state,last_outcome,last_error FROM monitors WHERE task_id='hung-monitor'"
        ).fetchone()
    assert row["state"] == "active"
    assert row["last_outcome"] == "timed_out"
    assert "timed out" in row["last_error"]


def test_reap_transition_failure_leaves_retryable_action_artifact(kanban_conn, monkeypatch):
    from hermes_cli import kanban_db

    task_id = kanban_db.create_task(kanban_conn, title="reap target", assignee="dev")
    claimed = kanban_db.claim_task(kanban_conn, task_id)
    run_id = store.record_run_start(
        task_id, "dev", "codex", "gpt", None, board="test",
        task_attempt_id=claimed.current_run_id,
    )
    store.record_run_spawned(run_id, 7777, "reap-token")
    advancer.plan_worker_transition(
        run_id=run_id, task_id=task_id, board="test", result="done", summary="done",
        process_start_token="reap-token", task_attempt_id=claimed.current_run_id,
    )
    monkeypatch.setattr(
        kanban_db, "complete_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("board write failed")),
    )

    advancer.run_action_intents(
        kanban_conn, board="test", kinds={"worker_task_transition"}, limit=1,
    )

    with store._connect() as db:
        rows = db.execute(
            "SELECT attempt,state,last_error FROM action_intents "
            "WHERE kind='worker_task_transition' ORDER BY attempt"
        ).fetchall()
    assert [(row["attempt"], row["state"]) for row in rows] == [
        (1, "retryable_failed"), (2, "planned"),
    ]
    assert "board write failed" in rows[0]["last_error"]


def test_worker_transition_abandons_superseded_task_attempt(kanban_conn):
    from hermes_cli import kanban_db

    task_id = kanban_db.create_task(kanban_conn, title="attempt fence", assignee="dev")
    old_claim = kanban_db.claim_task(kanban_conn, task_id)
    run_id = store.record_run_start(
        task_id, "dev", "codex", "gpt", None, board="test",
        task_attempt_id=old_claim.current_run_id,
    )
    store.record_run_spawned(run_id, 7331, "old-token")
    advancer.plan_worker_transition(
        run_id=run_id, task_id=task_id, board="test", result="blocked",
        summary="old attempt failed", process_start_token="old-token",
        task_attempt_id=old_claim.current_run_id,
    )

    assert kanban_db.reclaim_task(kanban_conn, task_id, reason="new attempt")
    new_claim = kanban_db.claim_task(kanban_conn, task_id)
    assert new_claim.current_run_id != old_claim.current_run_id

    advancer.run_action_intents(
        kanban_conn, board="test", kinds={"worker_task_transition"}, limit=1,
    )

    current = kanban_db.get_task(kanban_conn, task_id)
    assert current.status == "running"
    assert current.current_run_id == new_claim.current_run_id
    with store._connect() as db:
        intent = db.execute(
            "SELECT state,result_json,last_error FROM action_intents "
            "WHERE kind='worker_task_transition'"
        ).fetchone()
    assert intent["state"] == "abandoned"
    assert json.loads(intent["result_json"])["probe"] == "task_attempt_mismatch"
    assert "superseded" in intent["last_error"]


def test_hermes_executor_uses_same_durable_run_tracking(tmp_path, monkeypatch):
    from hermes_cli import kanban_db
    from shipfactory import config

    seat = SimpleNamespace(executor="hermes", model="native", profile="dev")
    cfg = SimpleNamespace(seats={"dev": seat}, recipes={"max_workers": 1})
    monkeypatch.setattr(config, "load_seats", lambda: cfg)
    monkeypatch.setattr(kanban_db, "_default_spawn", lambda *args, **kwargs: 8888)
    monkeypatch.setattr(spawn, "_process_start_token", lambda pid: "hermes-start")

    pid = spawn.shipfactory_spawn(
        SimpleNamespace(id="hermes-task", assignee="dev"), str(tmp_path / "work"),
        board="native",
    )
    row = store.nonterminal_runs()[0]
    assert pid == 8888
    assert row["executor"] == "hermes"
    assert row["provider"] == "hermes"
    assert row["process_start_token"] == "hermes-start"
    assert spawn._RUNNING[pid]["run_id"] == row["id"]

    spawn._RUNNING.clear()
    store.record_run_crashed(row["id"], "test cleanup")
    store.release_resource_lease(f"worker_slot:run:{row['id']}")
