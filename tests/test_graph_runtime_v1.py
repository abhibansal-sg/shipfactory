"""GraphRunner v1 generic completion envelope parser (Milestone C Task 11)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipfactory import graph_runtime
import shipfactory.spawn as spawn
from shipfactory import store
from shipfactory.graph_recipe import validate


def test_render_graph_prompt_preserves_unicode_and_exact_sections():
    recipe = validate({
        "name": "runtime",
        "start": "write",
        "boxes": [{
            "id": "write",
            "name": "Write",
            "who": "worker",
            "instructions": "Répondre avec précision 日本語.",
            "end": True,
        }],
        "arrows": [],
    })

    assert graph_runtime._render_prompt(
        request="Créer un résumé 🚀",
        input_work={
            "request": "Créer un résumé 🚀",
            "preceding_outputs": [
                {"attempt_id": "a-1", "box_id": "prior",
                 "output_work": "Déjà fait ✓"},
            ],
            "activating_token_ids": ["token-1"],
        },
        box=recipe.box("write"),
        allowed_labels=("done",),
    ) == (
        "## REQUEST\n"
        "Créer un résumé 🚀\n\n"
        "## PRECEDING WORK\n"
        "Déjà fait ✓\n\n"
        "## INSTRUCTIONS\n"
        "Répondre avec précision 日本語.\n\n"
        "## COMPLETION CONTRACT\n"
        "Your final non-empty output line must be exactly "
        "`SHIPFACTORY_RESULT: <label>`, where `<label>` is a lowercase "
        "result label matching `^[a-z][a-z0-9-]*$`. The only allowed label "
        "for this box is `done`; use it exactly and do not invent a synonym. "
        "Put all produced work "
        "before that final line.\n"
    )


def test_allowed_result_labels_come_from_frozen_outgoing_routes():
    recipe = validate({
        "name": "labels",
        "start": "review",
        "boxes": [
            {"id": "review", "name": "Review", "who": "worker",
             "instructions": "Review."},
            {"id": "finish", "name": "Finish", "who": "worker",
             "instructions": "Finish.", "end": True},
        ],
        "arrows": [
            {"from": "review", "result": "revise", "to": ["review"]},
            {"from": "review", "result": "approved", "to": ["finish"]},
        ],
    })

    assert graph_runtime._allowed_result_labels(recipe, "review") == (
        "revise", "approved",
    )
    assert graph_runtime._allowed_result_labels(recipe, "finish") == ("done",)
    assert graph_runtime._render_prompt(
        request="Review this.",
        input_work={
            "request": "Review this.",
            "preceding_outputs": [],
            "activating_token_ids": ["token-1"],
        },
        box=recipe.box("review"),
        allowed_labels=("revise", "approved"),
    ) == (
        "## REQUEST\n"
        "Review this.\n\n"
        "## PRECEDING WORK\n"
        "(none)\n\n"
        "## INSTRUCTIONS\n"
        "Review.\n\n"
        "## COMPLETION CONTRACT\n"
        "Your final non-empty output line must be exactly "
        "`SHIPFACTORY_RESULT: <label>`, where `<label>` is a lowercase "
        "result label matching `^[a-z][a-z0-9-]*$`. The allowed labels for "
        "this box are `revise`, `approved`; use exactly one of them and do "
        "not invent a synonym. Put all produced work before that final line.\n"
    )


def test_load_input_accepts_only_graph_runner_durable_preceding_output_shape():
    durable = {
        "request": "Review this.",
        "preceding_outputs": [{
            "attempt_id": "attempt-1",
            "box_id": "planner",
            "output_work": "A durable plan.",
        }],
        "activating_token_ids": ["token-1"],
    }
    assert graph_runtime._load_input(
        json.dumps(durable), "Review this.",
    ) == durable

    invented = {
        **durable,
        "preceding_outputs": [{
            "attempt_id": "attempt-1",
            "box_id": "planner",
            "result": "done",
            "work": "A durable plan.",
        }],
    }
    with pytest.raises(graph_runtime.GraphRuntimeError, match="invalid preceding work"):
        graph_runtime._load_input(json.dumps(invented), "Review this.")


RUNTIME_RECIPE = validate({
    "name": "runtime-adapter",
    "start": "write",
    "boxes": [{
        "id": "write",
        "name": "Write",
        "who": "worker",
        "instructions": "Produce the requested work.",
        "end": True,
    }],
    "arrows": [],
})

HUMAN_RUNTIME_RECIPE = validate({
    "name": "human-runtime-adapter",
    "start": "write",
    "boxes": [{
        "id": "write",
        "name": "Approve",
        "who": "human",
        "instructions": "Choose the declared result.",
        "end": True,
    }],
    "arrows": [],
})


class _GraphProc:
    pid = 4321

    def __init__(self, *_args, **_kwargs):
        self.code = None

    def poll(self):
        return self.code


def _ready_attempt(tmp_path, monkeypatch, *, input_work=None, recipe=RUNTIME_RECIPE):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = "graph-run"
    attempt_id = "graph-attempt"
    work = input_work if input_work is not None else {
        "request": "Build it",
        "preceding_outputs": [],
        "activating_token_ids": ["root-token"],
    }
    now = "2026-07-29T00:00:00+00:00"
    with store._connect() as db:
        db.execute(
            """INSERT INTO recipe_runs_v1(
                 id,project_id,board,recipe_name,recipe_hash,
                 recipe_snapshot_json,request_text,workspace_path,launch_key,
                 state,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?, 'running',?,?)""",
            (
                run_id, "project", "board", recipe.name,
                recipe.hash, recipe.canonical_json, "Build it",
                str(workspace), "launch-key", now, now,
            ),
        )
        db.execute(
            """INSERT INTO box_attempts_v1(
                 id,run_id,box_id,ordinal,state,input_work_json,created_at,updated_at
               ) VALUES(?,?,?,?, 'ready',?,?,?)""",
            (
                attempt_id, run_id, "write", 1,
                json.dumps(work, ensure_ascii=False), now, now,
            ),
        )
    seat = SimpleNamespace(
        name="worker", profile="worker", executor="codex", model="gpt",
        reasoning="medium", config={}, skills=(),
    )
    cfg = SimpleNamespace(seats={"worker": seat}, recipes={"max_workers": 1})
    monkeypatch.setattr(graph_runtime, "load_seats", lambda: cfg)
    monkeypatch.setattr(store, "seat_paused", lambda _name: False)
    monkeypatch.setattr(spawn, "_capture_start_token", lambda *_args: "token-4321")
    spawn._RUNNING.clear()
    return run_id, attempt_id, workspace


def test_spawn_ready_moves_human_box_to_waiting_human_without_launch(
    monkeypatch, tmp_path,
):
    _run_id, attempt_id, _workspace = _ready_attempt(
        tmp_path, monkeypatch, recipe=HUMAN_RUNTIME_RECIPE,
    )
    monkeypatch.setattr(
        spawn.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("human box must not launch a process"),
    )

    assert graph_runtime.spawn_ready(1) == []
    with store._connect() as db:
        attempt = db.execute(
            "SELECT state,executor_run_id FROM box_attempts_v1 WHERE id=?",
            (attempt_id,),
        ).fetchone()
    assert dict(attempt) == {"state": "waiting_human", "executor_run_id": None}


def test_shared_target_adapter_preserves_command_and_environment_parity(
    monkeypatch, tmp_path,
):
    captures = []
    run_ids = iter((101, 102))

    def record_run_start(*_args, **kwargs):
        if kwargs:
            raise TypeError("legacy store double accepts positional metadata only")
        return next(run_ids)

    fake_store = SimpleNamespace(
        record_run_start=record_run_start,
        acquire_resource_lease=lambda *_args, **_kwargs: {"acquired": True},
        record_run_spawned=lambda *_args, **_kwargs: None,
        record_run_crashed=lambda *_args, **_kwargs: None,
        release_resource_lease=lambda *_args, **_kwargs: True,
    )
    fake_executor = SimpleNamespace(
        version="1",
        build_cmd=lambda seat, prompt, workspace: [
            "executor", seat.model, prompt, workspace,
        ],
    )

    class Proc:
        def __init__(self, command, **kwargs):
            self.pid = 5000 + len(captures)
            captures.append({"command": command, "env": kwargs["env"]})

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "operator-home"))
    monkeypatch.setattr(spawn, "_store_module", lambda: fake_store)
    monkeypatch.setattr(spawn, "get_executor", lambda _name: fake_executor)
    monkeypatch.setattr(spawn.subprocess, "Popen", Proc)
    monkeypatch.setattr(spawn, "_capture_start_token", lambda *_args: "token")
    spawn._RUNNING.clear()
    root = tmp_path / "workspace"
    root.mkdir()
    prompt_path = tmp_path / "prompt"
    prompt_path.write_text("same prompt", encoding="utf-8")
    seat = SimpleNamespace(executor="codex", model="model")
    common = {
        "target_id": "same-target",
        "seat_name": "worker",
        "board": "same-board",
        "workspace_path": root,
        "log_path": tmp_path / "worker.log",
        "prompt_path": prompt_path,
        "prompt": "same prompt",
        "max_workers": 2,
    }

    spawn._spawn_target(
        dict(common, target_kind="legacy_task"), seat=seat, cfg=SimpleNamespace(),
    )
    spawn._spawn_target(
        dict(common, target_kind="graph_box", graph_run_id="graph-run"),
        seat=seat, cfg=SimpleNamespace(),
    )

    assert captures[0]["command"] == captures[1]["command"]
    assert captures[0]["env"] == captures[1]["env"]
    spawn._RUNNING.clear()


def test_graph_spawn_binds_durable_run_before_popen(monkeypatch, tmp_path):
    run_id, attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)
    observed = {}

    def popen(*args, **kwargs):
        with store._connect() as db:
            attempt = db.execute(
                "SELECT state,executor_run_id FROM box_attempts_v1 WHERE id=?",
                (attempt_id,),
            ).fetchone()
            durable = db.execute(
                "SELECT task_id,pid FROM runs WHERE id=?",
                (attempt["executor_run_id"],),
            ).fetchone()
        observed.update(
            state=attempt["state"], executor_run_id=attempt["executor_run_id"],
            task_id=durable["task_id"], pid=durable["pid"],
            cwd=kwargs["cwd"],
        )
        return _GraphProc()

    monkeypatch.setattr(spawn.subprocess, "Popen", popen)

    assert graph_runtime.spawn_ready(1) == [4321]
    assert observed == {
        "state": "running",
        "executor_run_id": spawn._RUNNING[4321]["run_id"],
        "task_id": attempt_id,
        "pid": None,
        "cwd": str(tmp_path / "workspace"),
    }
    assert spawn._RUNNING[4321]["target_kind"] == "graph_box"
    assert spawn._RUNNING[4321]["graph_run_id"] == run_id


def test_bound_spawn_failure_preserves_recovery_anchor_when_event_write_fails(
    monkeypatch, tmp_path,
):
    _run_id, attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)
    monkeypatch.setattr(
        spawn.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    monkeypatch.setattr(
        graph_runtime,
        "enqueue_terminal_event",
        lambda **_kwargs: (_ for _ in ()).throw(
            store.GraphStoreIntegrityError("event write failed")
        ),
    )

    assert graph_runtime.spawn_ready(1) == []
    with store._connect() as db:
        attempt = db.execute(
            "SELECT state,executor_run_id FROM box_attempts_v1 WHERE id=?",
            (attempt_id,),
        ).fetchone()
        durable = db.execute(
            "SELECT ended_at FROM runs WHERE id=?",
            (attempt["executor_run_id"],),
        ).fetchone()
    assert attempt["state"] == "running"
    assert durable["ended_at"] is None


def test_graph_reap_success_enqueues_exact_event_without_artifacts_and_releases_lease(
    monkeypatch, tmp_path,
):
    run_id, attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn.subprocess, "Popen", _GraphProc)
    released = []
    real_release = store.release_resource_lease
    monkeypatch.setattr(
        store, "release_resource_lease",
        lambda key: released.append(key) or real_release(key),
    )
    graph_runtime.spawn_ready(1)
    record = spawn._RUNNING[4321]
    executor_run_id = record["run_id"]
    Path(record["log_path"]).write_text(
        "completed work ✓\nSHIPFACTORY_RESULT: shipped\n",
        encoding="utf-8",
    )
    record["proc"].code = 0
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "shipfactory.artifacts":
            pytest.fail("graph reap must not import the artifact subsystem")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert spawn.reap_finished() == [{
        "pid": 4321, "attempt_id": attempt_id, "result": "shipped",
        "summary": "completed work ✓", "exit_code": 0,
    }]
    with store._connect() as db:
        event = db.execute(
            "SELECT source,payload_json FROM run_events_v1 WHERE run_id=?",
            (run_id,),
        ).fetchone()
        ended = db.execute(
            "SELECT ended_at,result FROM runs WHERE id=?", (executor_run_id,),
        ).fetchone()
    assert event["source"] == "graph_runtime"
    assert json.loads(event["payload_json"]) == {
        "type": "box_completed",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "expected_state": "running",
        "result": "shipped",
        "work": "completed work ✓",
    }
    assert ended["ended_at"] is not None and ended["result"] == "done"
    assert released == [f"worker_slot:run:{executor_run_id}"]


def test_graph_reap_nonzero_is_exact_technical_failure(monkeypatch, tmp_path):
    run_id, attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn.subprocess, "Popen", _GraphProc)
    graph_runtime.spawn_ready(1)
    record = spawn._RUNNING[4321]
    Path(record["log_path"]).write_text(
        "partial work\nSHIPFACTORY_RESULT: done\n", encoding="utf-8",
    )
    record["proc"].code = 7

    outcome = spawn.reap_finished()[0]
    with store._connect() as db:
        payload = json.loads(db.execute(
            "SELECT payload_json FROM run_events_v1 WHERE run_id=?", (run_id,),
        ).fetchone()[0])
    assert outcome["result"] == "failed"
    assert payload == {
        "type": "box_failed",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "expected_state": "running",
        "failure": "harness exited with nonzero code 7",
    }


def test_graph_terminal_event_key_deduplicates_retry(monkeypatch, tmp_path):
    run_id, attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)

    first = graph_runtime.enqueue_terminal_event(
        executor_run_id=99, graph_run_id=run_id, attempt_id=attempt_id,
        event_type="box_failed", failure="technical failure",
    )
    second = graph_runtime.enqueue_terminal_event(
        executor_run_id=99, graph_run_id=run_id, attempt_id=attempt_id,
        event_type="box_failed", failure="technical failure",
    )

    assert first["key"] == second["key"]
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM run_events_v1 WHERE key=?", (first["key"],),
        ).fetchone()[0] == 1


def test_graph_duplicate_reap_after_restart_keeps_one_durable_event(
    monkeypatch, tmp_path,
):
    run_id, _attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn.subprocess, "Popen", _GraphProc)
    graph_runtime.spawn_ready(1)
    record = spawn._RUNNING[4321]
    Path(record["log_path"]).write_text(
        "completed once\nSHIPFACTORY_RESULT: shipped\n", encoding="utf-8",
    )
    record["proc"].code = 0
    replay = dict(record)

    assert len(spawn.reap_finished()) == 1
    spawn._RUNNING[4321] = replay
    assert len(spawn.reap_finished()) == 1

    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM run_events_v1 WHERE run_id=?", (run_id,),
        ).fetchone()[0] == 1


def test_restore_running_infers_graph_target_and_adopts_exact_identity(
    monkeypatch, tmp_path,
):
    run_id, attempt_id, _workspace = _ready_attempt(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn.subprocess, "Popen", _GraphProc)
    graph_runtime.spawn_ready(1)
    executor_run_id = spawn._RUNNING[4321]["run_id"]
    spawn._RUNNING.clear()
    monkeypatch.setattr(
        spawn, "_process_start_token",
        lambda pid: "token-4321" if pid == 4321 else None,
    )
    monkeypatch.setattr(
        spawn, "_plan_worker_transition",
        lambda *_args, **_kwargs: pytest.fail(
            "graph restore must not plan a board transition",
        ),
    )

    assert spawn.restore_running(max_workers=1) == {
        "restored": [4321], "crashed": [],
    }
    record = spawn._RUNNING[4321]
    assert record["target_kind"] == "graph_box"
    assert record["target_id"] == attempt_id
    assert record["graph_run_id"] == run_id
    assert record["run_id"] == executor_run_id


def test_restore_running_turns_pidless_bound_graph_spawn_into_failure_event(
    monkeypatch, tmp_path,
):
    run_id, attempt_id, workspace = _ready_attempt(tmp_path, monkeypatch)
    executor_run_id = store.record_run_start(
        attempt_id, "worker", "codex", "gpt", None,
        board="board", workspace_path=str(workspace),
    )
    graph_runtime._bind_attempt(attempt_id, run_id, executor_run_id)
    monkeypatch.setattr(
        spawn, "_plan_worker_transition",
        lambda *_args, **_kwargs: pytest.fail(
            "pidless graph recovery must not plan a board transition"
        ),
    )

    assert spawn.restore_running(max_workers=1) == {
        "restored": [], "crashed": [executor_run_id],
    }
    with store._connect() as db:
        event = db.execute(
            "SELECT payload_json FROM run_events_v1 WHERE run_id=?", (run_id,),
        ).fetchone()
        durable = db.execute(
            "SELECT ended_at,result FROM runs WHERE id=?", (executor_run_id,),
        ).fetchone()
    assert json.loads(event["payload_json"]) == {
        "type": "box_failed",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "expected_state": "running",
        "failure": "worker crashed: pid missing",
    }
    assert (
        durable["ended_at"] is not None
        and durable["result"] == "crashed: pid missing"
    )


def test_parse_graph_completion_accepts_arbitrary_label():
    result, work = spawn.parse_graph_completion(
        "did the thing\nSHIPFACTORY_RESULT: shipped\n", 0
    )
    assert result == "shipped"
    assert work == "did the thing"


def test_parse_graph_completion_rejects_malformed_label():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT: Not_Valid!\n", 0
        )


def test_parse_graph_completion_rejects_extra_text_after_sentinel():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT: shipped\ntrailing junk\n", 0
        )


def test_parse_graph_completion_rejects_nonzero_exit_code():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT: shipped\n", 1
        )


def test_parse_graph_completion_rejects_missing_sentinel():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion("did the thing with no sentinel\n", 0)


def test_parse_graph_completion_rejects_empty_work():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion("SHIPFACTORY_RESULT: shipped\n", 0)


def test_parse_graph_completion_preserves_unicode_work():
    result, work = spawn.parse_graph_completion(
        "已完成任务 🎉 café résumé\nSHIPFACTORY_RESULT: done-ok\n", 0
    )
    assert result == "done-ok"
    assert work == "已完成任务 🎉 café résumé"


def test_parse_graph_completion_rejects_zero_spaces_after_colon():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT:shipped\n", 0
        )


def test_parse_graph_completion_rejects_double_space_after_colon():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT:  shipped\n", 0
        )


def test_parse_graph_completion_rejects_tab_after_colon():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT:\tshipped\n", 0
        )


def test_parse_graph_completion_rejects_unicode_space_after_colon():
    with pytest.raises(ValueError):
        spawn.parse_graph_completion(
            "did the thing\nSHIPFACTORY_RESULT:\u00a0shipped\n", 0
        )


def test_parse_graph_completion_preserves_crlf_and_blank_line_verbatim():
    result, work = spawn.parse_graph_completion(
        "alpha\r\n\r\nSHIPFACTORY_RESULT: done\r\n", 0
    )
    assert result == "done"
    assert work == "alpha\r\n"


@pytest.mark.parametrize(
    "separator",
    ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_parse_graph_completion_handles_every_python_line_separator(separator):
    result, work = spawn.parse_graph_completion(
        f"work{separator}SHIPFACTORY_RESULT: done{separator}", 0
    )
    assert result == "done"
    assert work == "work"


def test_parse_graph_completion_simple_work_has_no_trailing_newline():
    result, work = spawn.parse_graph_completion(
        "work\nSHIPFACTORY_RESULT: done\n", 0
    )
    assert result == "done"
    assert work == "work"


def test_parse_graph_completion_allows_trailing_blank_lines_after_sentinel():
    result, work = spawn.parse_graph_completion(
        "work\nSHIPFACTORY_RESULT: done\n\n\n", 0
    )
    assert result == "done"
    assert work == "work"


def test_legacy_parse_result_behavior_is_byte_identical():
    """The new generic envelope parser must never alter legacy `_parse_result`.

    Legacy tasks emit `SHIPFACTORY_RESULT: done|blocked <summary>` (a fixed
    two-value vocabulary) or a `SHIPFACTORY_VERDICT: {...}` JSON sentinel that
    always wins over a trailing RESULT line. `parse_graph_completion` is an
    entirely separate function for the new generic v1 grammar and must not
    change `_parse_result`'s outputs for any of these legacy shapes.
    """
    # Plain done/blocked sentinel, exactly as before.
    assert spawn._parse_result("did work\nSHIPFACTORY_RESULT: done shipped\n", 0) == (
        "done", "shipped",
    )
    assert spawn._parse_result("SHIPFACTORY_RESULT: blocked stuck\n", 0) == (
        "blocked", "stuck",
    )
    # Verdict sentinel wins over a trailing RESULT line, unchanged.
    text = (
        "SHIPFACTORY_VERDICT: {\"result\":\"changes\"}\n"
        "SHIPFACTORY_RESULT: done ignored\n"
    )
    assert spawn._parse_result(text, 0) == (
        "done", 'SHIPFACTORY_VERDICT: {"result":"changes"}',
    )
    # Missing sentinel with zero exit code, unchanged fallback.
    assert spawn._parse_result("no sentinel here\n", 0) == (
        "blocked", "no result sentinel",
    )
    # Missing sentinel with nonzero exit code, unchanged fallback.
    assert spawn._parse_result("no sentinel here\n", 1) == (
        "blocked", "harness exited with code 1",
    )
