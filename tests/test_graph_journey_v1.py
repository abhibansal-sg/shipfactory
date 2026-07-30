"""End-to-end deterministic GraphRunner v1 journeys (Milestone E Task 17)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time

from shipfactory import decisions, graph_runner, graph_runtime, spawn, store
from shipfactory.graph_recipe import load, validate


ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = ROOT / "recipes" / "v1" / "plan-build-review.yaml"


def _start(tmp_path, monkeypatch, *, key: str):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    recipe = load(RECIPE_PATH)
    run = graph_runner.start_run(
        project_id="journey-project", board="journey-board", recipe=recipe,
        request_text="Build the exact requested result.", launch_key=key,
        workspace_path=str(ROOT),
    )
    with store._connect() as db:
        graph_runner.reconcile_run(db, run["id"])
    return recipe, run


def _latest(run_id: str, box_id: str) -> dict:
    with store._connect() as db:
        row = db.execute(
            """SELECT * FROM box_attempts_v1 WHERE run_id=? AND box_id=?
               ORDER BY ordinal DESC LIMIT 1""",
            (run_id, box_id),
        ).fetchone()
    assert row is not None, box_id
    return dict(row)


def _complete(run_id: str, box_id: str, result: str, work: str) -> dict:
    attempt = _latest(run_id, box_id)
    graph_runner.enqueue_event(
        run_id=run_id, source="deterministic-journey",
        payload={
            "type": "box_completed", "run_id": run_id,
            "attempt_id": attempt["id"], "expected_state": attempt["state"],
            "result": result, "work": work,
        },
        key=f"journey:{run_id}:{attempt['id']}:completed",
    )
    summary = graph_runner.apply_events(owner=f"journey-{attempt['id']}", run_id=run_id)
    assert summary == {"leased": 1, "applied": 1, "discarded": 0, "failed": 0}
    with store._connect() as db:
        graph_runner.reconcile_run(db, run_id)
    return attempt


def _fail(run_id: str, box_id: str, failure: str) -> dict:
    attempt = _latest(run_id, box_id)
    graph_runner.enqueue_event(
        run_id=run_id, source="deterministic-journey",
        payload={
            "type": "box_failed", "run_id": run_id,
            "attempt_id": attempt["id"], "expected_state": attempt["state"],
            "failure": failure,
        },
        key=f"journey:{run_id}:{attempt['id']}:failed",
    )
    summary = graph_runner.apply_events(owner=f"journey-{attempt['id']}", run_id=run_id)
    assert summary == {"leased": 1, "applied": 1, "discarded": 0, "failed": 0}
    return attempt


def test_ratified_recipe_completes_with_rework_parallel_join_restart_and_human_pause(
    tmp_path, monkeypatch,
):
    recipe, run = _start(tmp_path, monkeypatch, key="journey-complete")
    run_id = run["id"]

    _complete(run_id, "planner", "done", "plan-v1")
    _complete(run_id, "plan-review", "revise", "revise-plan")
    _complete(run_id, "planner", "done", "plan-v2")
    _complete(run_id, "plan-review", "approved", "plan-approved")
    _complete(run_id, "builder", "done", "build-v1")

    # Deliberately finish parallel reviews out of recipe declaration order.
    _complete(run_id, "simplicity-review", "done", "simple-v1")
    _complete(run_id, "correctness-review", "done", "correct-v1")
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM box_attempts_v1 WHERE run_id=? AND box_id='synthesize'",
            (run_id,),
        ).fetchone()[0] == 0
    _complete(run_id, "risk-review", "done", "risk-v1")

    synthesis_v1 = _latest(run_id, "synthesize")
    combined_v1 = json.loads(synthesis_v1["input_work_json"])["preceding_outputs"]
    combined_work = [item["output_work"] for item in combined_v1]
    assert {"correct-v1", "risk-v1", "simple-v1"}.issubset(combined_work)
    assert combined_work.count("correct-v1") == 1
    assert combined_work.count("risk-v1") == 1
    assert combined_work.count("simple-v1") == 1
    _complete(run_id, "synthesize", "rework", "rework-build")

    _complete(run_id, "builder", "done", "build-v2")
    _complete(run_id, "risk-review", "done", "risk-v2")
    _complete(run_id, "simplicity-review", "done", "simple-v2")
    _complete(run_id, "correctness-review", "done", "correct-v2")
    _complete(run_id, "synthesize", "pass", "all-reviews-pass")

    human = _latest(run_id, "human-approval")
    assert human["state"] == "ready"
    assert graph_runtime.spawn_ready(1, board="journey-board") == []
    human = _latest(run_id, "human-approval")
    assert human["state"] == "waiting_human"

    # A daemon restart loses process memory but not the durable human wait.
    spawn._RUNNING.clear()
    with store._connect() as db:
        graph_runner.reconcile_run(db, run_id)
    assert _latest(run_id, "human-approval")["state"] == "waiting_human"

    decision = decisions.enqueue_human_box_decision(
        attempt_id=human["id"], result="approved", actor_kind="human",
        actor_id="journey-operator", channel="test", nonce="journey-human-approval",
    )
    assert decision["replayed"] is False
    assert graph_runner.apply_events(owner="journey-human", run_id=run_id) == {
        "leased": 1, "applied": 1, "discarded": 0, "failed": 0,
    }
    with store._connect() as db:
        graph_runner.reconcile_run(db, run_id)
    _complete(run_id, "final-delivery", "done", "delivered")

    with store._connect() as db:
        stored_run = dict(db.execute("SELECT * FROM recipe_runs_v1 WHERE id=?", (run_id,)).fetchone())
        attempts = [dict(row) for row in db.execute(
            "SELECT * FROM box_attempts_v1 WHERE run_id=? ORDER BY created_at,id", (run_id,),
        )]
        events = [dict(row) for row in db.execute(
            "SELECT * FROM run_events_v1 WHERE run_id=? ORDER BY created_at,key", (run_id,),
        )]
        legacy_instances = db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0]
        legacy_steps = db.execute("SELECT COUNT(*) FROM recipe_steps").fetchone()[0]

    assert stored_run["state"] == "completed"
    assert stored_run["recipe_snapshot_json"] == recipe.canonical_json
    assert stored_run["recipe_hash"] == recipe.hash
    assert len(attempts) == 16
    expected_ordinals = {
        "planner": 2, "plan-review": 2, "builder": 2,
        "correctness-review": 2, "risk-review": 2, "simplicity-review": 2,
        "synthesize": 2, "human-approval": 1, "final-delivery": 1,
    }
    assert {
        box_id: max(item["ordinal"] for item in attempts if item["box_id"] == box_id)
        for box_id in expected_ordinals
    } == expected_ordinals
    assert len({item["id"] for item in attempts}) == len(attempts)
    assert events and all(item["state"] == "applied" for item in events)
    assert len({item["key"] for item in events}) == len(events)
    assert legacy_instances == 0
    assert legacy_steps == 0


def test_ratified_recipe_blocks_after_three_consecutive_technical_failures(
    tmp_path, monkeypatch,
):
    _recipe, run = _start(tmp_path, monkeypatch, key="journey-failure-limit")
    for ordinal in range(1, 4):
        attempt = _fail(run["id"], "planner", f"executor failure {ordinal}")
        assert attempt["ordinal"] == ordinal

    with store._connect() as db:
        stored_run = dict(db.execute("SELECT * FROM recipe_runs_v1 WHERE id=?", (run["id"],)).fetchone())
        attempts = [dict(row) for row in db.execute(
            "SELECT * FROM box_attempts_v1 WHERE run_id=? ORDER BY ordinal", (run["id"],),
        )]
    assert stored_run["state"] == "escalated"
    assert json.loads(stored_run["blocked_reason"])["type"] == "technical_failure_limit"
    assert [item["ordinal"] for item in attempts] == [1, 2, 3]
    assert all(item["state"] == "failed" for item in attempts)


def test_ratified_recipe_blocks_on_third_same_rework_arrow_traversal(
    tmp_path, monkeypatch,
):
    _recipe, run = _start(tmp_path, monkeypatch, key="journey-loop-limit")
    for ordinal in range(1, 4):
        _complete(run["id"], "planner", "done", f"plan-{ordinal}")
        _complete(run["id"], "plan-review", "revise", f"revise-{ordinal}")

    with store._connect() as db:
        stored_run = dict(db.execute("SELECT * FROM recipe_runs_v1 WHERE id=?", (run["id"],)).fetchone())
        reviews = [dict(row) for row in db.execute(
            """SELECT * FROM box_attempts_v1 WHERE run_id=? AND box_id='plan-review'
               ORDER BY ordinal""", (run["id"],),
        )]
    assert stored_run["state"] == "escalated"
    reason = json.loads(stored_run["blocked_reason"])
    assert reason["type"] == "rework_limit"
    assert reason["source_box_id"] == "plan-review"
    assert reason["result"] == "revise"
    assert reason["history"]
    assert [item["ordinal"] for item in reviews] == [1, 2, 3]


def _live_executor_run(tmp_path, monkeypatch, *, script_body: str, command_args: list[str]):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "executor.py"
    script.write_text(script_body, encoding="utf-8")
    monkeypatch.setenv(
        "FACTORY_EXECUTOR_CMD_CODEX",
        " ".join([sys.executable, str(script), *command_args]),
    )
    seat = SimpleNamespace(executor="codex", model="deterministic-test")
    executor = SimpleNamespace(
        version="test", identity_files=lambda *_args: None,
        build_cmd=lambda *_args: ["must-be-overridden"],
        extract_text=lambda text: text,
        parse_usage=lambda _text: {"tokens_in": None, "tokens_out": None},
    )
    monkeypatch.setattr(
        graph_runtime, "load_seats",
        lambda: SimpleNamespace(seats={"worker": seat}),
    )
    monkeypatch.setattr(graph_runtime, "get_executor", lambda _name: executor)
    monkeypatch.setattr(spawn, "get_executor", lambda _name: executor)
    recipe = validate({
        "name": "live-subprocess-proof", "start": "live-box",
        "boxes": [{
            "id": "live-box", "name": "Live box", "who": "worker",
            "instructions": "Execute these exact live instructions.", "end": True,
        }],
        "arrows": [],
    })
    run = graph_runner.start_run(
        project_id="live-project", board="live-board", recipe=recipe,
        request_text="Live request from the operator.", launch_key="live-key",
        workspace_path=str(workspace),
    )
    with store._connect() as db:
        graph_runner.reconcile_run(db, run["id"])
    return run


def test_real_subprocess_binds_before_pid_adopts_restart_and_enqueues_before_apply(
    tmp_path, monkeypatch,
):
    captured = tmp_path / "captured-prompt.txt"
    release = tmp_path / "release"
    run = _live_executor_run(
        tmp_path, monkeypatch,
        script_body=(
            "import pathlib,sys,time\n"
            "prompt=sys.stdin.read()\n"
            "pathlib.Path(sys.argv[1]).write_text(prompt, encoding='utf-8')\n"
            "release=pathlib.Path(sys.argv[2])\n"
            "deadline=time.time()+10\n"
            "while not release.exists() and time.time()<deadline: time.sleep(0.02)\n"
            "print('auditable live work')\n"
            "print('SHIPFACTORY_RESULT: done')\n"
        ),
        command_args=[str(captured), str(release)],
    )
    spawn._RUNNING.clear()
    pids = graph_runtime.spawn_ready(1, board="live-board")
    assert len(pids) == 1
    pid = pids[0]
    deadline = time.time() + 5
    while not captured.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert captured.exists()

    record = spawn._RUNNING[pid]
    executor_run_id = record["run_id"]
    attempt_id = record["attempt_id"]
    with store._connect() as db:
        attempt = db.execute(
            "SELECT state,executor_run_id FROM box_attempts_v1 WHERE id=?", (attempt_id,),
        ).fetchone()
        durable = db.execute(
            "SELECT pid,process_start_token FROM runs WHERE id=?", (executor_run_id,),
        ).fetchone()
        lease = db.execute(
            "SELECT key FROM resource_leases WHERE key=?",
            (f"worker_slot:run:{executor_run_id}",),
        ).fetchone()
    assert dict(attempt) == {"state": "running", "executor_run_id": executor_run_id}
    assert durable["pid"] == pid and durable["process_start_token"]
    assert lease is not None

    # Simulate daemon process-memory loss and prove exact PID/token adoption.
    original_proc = record["proc"]
    spawn._RUNNING.clear()
    restored = spawn.restore_running(max_workers=1)
    assert restored == {"restored": [pid], "crashed": []}
    assert spawn._RUNNING[pid]["attempt_id"] == attempt_id

    # This pytest process remains the child's real OS parent. Reattach its
    # original waitable handle after proving adoption so the success-path
    # assertion sees the real exit code rather than adopted disappearance 255.
    spawn._RUNNING[pid]["proc"] = original_proc
    release.touch()
    assert original_proc.wait(timeout=5) == 0
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "shipfactory.artifacts":
            raise AssertionError("graph subprocess reap imported artifact sealing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    outcome = spawn.reap_finished()
    assert outcome == [{
        "pid": pid, "attempt_id": attempt_id, "result": "done",
        "summary": "auditable live work", "exit_code": 0,
    }]
    with store._connect() as db:
        event = dict(db.execute(
            "SELECT * FROM run_events_v1 WHERE run_id=?", (run["id"],),
        ).fetchone())
        still_running = db.execute(
            "SELECT state FROM box_attempts_v1 WHERE id=?", (attempt_id,),
        ).fetchone()[0]
    assert event["state"] == "pending"
    assert still_running == "running"

    assert graph_runner.apply_events(owner="live-reducer", run_id=run["id"])["applied"] == 1
    with store._connect() as db:
        graph_runner.reconcile_run(db, run["id"])
        assert db.execute(
            "SELECT state FROM recipe_runs_v1 WHERE id=?", (run["id"],),
        ).fetchone()[0] == "completed"
    prompt = captured.read_text(encoding="utf-8")
    assert "Live request from the operator." in prompt
    assert "Execute these exact live instructions." in prompt
    assert "SHIPFACTORY_RESULT: <label>" in prompt
    assert Path(record["prompt_path"]).read_text(encoding="utf-8") == prompt
    assert "auditable live work" in Path(record["log_path"]).read_text(encoding="utf-8")


def test_real_subprocess_nonzero_exit_becomes_retryable_technical_failure(
    tmp_path, monkeypatch,
):
    run = _live_executor_run(
        tmp_path, monkeypatch,
        script_body=(
            "import sys\n"
            "sys.stdin.read()\n"
            "print('partial live work')\n"
            "print('SHIPFACTORY_RESULT: done')\n"
            "raise SystemExit(7)\n"
        ),
        command_args=[],
    )
    spawn._RUNNING.clear()
    pid = graph_runtime.spawn_ready(1, board="live-board")[0]
    spawn._RUNNING[pid]["proc"].wait(timeout=5)
    outcome = spawn.reap_finished()[0]
    assert outcome["result"] == "failed"
    assert outcome["exit_code"] == 7
    assert graph_runner.apply_events(owner="live-failure", run_id=run["id"])["applied"] == 1
    with store._connect() as db:
        attempts = [dict(row) for row in db.execute(
            "SELECT state,ordinal,technical_failure FROM box_attempts_v1 WHERE run_id=? ORDER BY ordinal",
            (run["id"],),
        )]
    assert attempts == [
        {"state": "failed", "ordinal": 1, "technical_failure": "harness exited with nonzero code 7"},
        {"state": "ready", "ordinal": 2, "technical_failure": None},
    ]
