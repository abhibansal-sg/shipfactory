import sys
import types
from pathlib import Path
from types import SimpleNamespace

import headframe.spawn as spawn


def _install_stubs(monkeypatch, seat, calls):
    config = types.ModuleType("headframe.config")
    config.load_seats = lambda: SimpleNamespace(seats={seat.name: seat})
    store = types.ModuleType("headframe.store")
    store.seat_paused = lambda name: False
    store.record_run_start = lambda *args: calls.append(("start", args)) or 41
    store.record_run_end = lambda *args: calls.append(("end", args))
    monkeypatch.setitem(sys.modules, "headframe.config", config)
    monkeypatch.setitem(sys.modules, "headframe.store", store)
    kanban = types.ModuleType("hermes_cli.kanban_db")
    kanban.connect = lambda board=None: SimpleNamespace(close=lambda: None)
    kanban.build_worker_context = lambda conn, task_id: f"context for {task_id}"
    kanban.complete_task = lambda conn, task_id, summary=None: calls.append(("complete", task_id, summary))
    kanban.block_task = lambda conn, task_id, reason=None: calls.append(("block", task_id, reason))
    hermes = types.ModuleType("hermes_cli")
    hermes.kanban_db = kanban
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban)


class _Proc:
    pid = 1234
    stdin = None

    def __init__(self, *_args, **_kwargs):
        import io
        self.stdin = io.BytesIO()
        self.code = None

    def poll(self):
        return self.code


def test_spawn_and_reap_done(monkeypatch, tmp_path):
    calls = []
    seat = SimpleNamespace(name="dev", profile="dev", executor="codex", model="gpt", reasoning="medium")
    _install_stubs(monkeypatch, seat, calls)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spawn.subprocess, "Popen", _Proc)
    spawn._RUNNING.clear()
    task = SimpleNamespace(id="t1", assignee="dev")
    assert spawn.headframe_spawn(task, str(tmp_path / "work"), board="b") == 1234
    record = spawn._RUNNING[1234]
    Path(record["log_path"]).write_text('{"usage":{"input_tokens":2,"output_tokens":3}}\nHEADFRAME_RESULT: done shipped\n')
    record["proc"].code = 0
    assert spawn.reap_finished()[0]["result"] == "done"
    assert any(item[0] == "complete" for item in calls)


def test_exit_zero_without_sentinel_blocks(monkeypatch, tmp_path):
    calls = []
    seat = SimpleNamespace(name="dev", profile="dev", executor="claude", model="sonnet", reasoning="")
    _install_stubs(monkeypatch, seat, calls)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spawn.subprocess, "Popen", _Proc)
    spawn._RUNNING.clear()
    spawn.headframe_spawn(SimpleNamespace(id="t2", assignee="dev"), str(tmp_path / "work"))
    record = spawn._RUNNING[1234]
    Path(record["log_path"]).write_text("finished\n")
    record["proc"].code = 0
    result = spawn.reap_finished()[0]
    assert result["result"] == "blocked" and result["summary"] == "no result sentinel"
    assert any(item[0] == "block" for item in calls)


def test_unknown_seat_skips(monkeypatch):
    config = types.ModuleType("headframe.config")
    config.load_seats = lambda: SimpleNamespace(seats={})
    store = types.ModuleType("headframe.store")
    store.seat_paused = lambda name: False
    monkeypatch.setitem(sys.modules, "headframe.config", config)
    monkeypatch.setitem(sys.modules, "headframe.store", store)
    assert spawn.headframe_spawn(SimpleNamespace(id="t", assignee="nobody"), "/tmp") is None


def test_reap_codex_jsonl_sentinel_completes(monkeypatch, tmp_path):
    """Finding #23 end-to-end: codex --json output must reap as done, not
    'no result sentinel' — the exact failure that fused t_737aec66."""
    calls = []
    seat = SimpleNamespace(name="dev", profile="dev", executor="codex", model="gpt", reasoning="medium")
    _install_stubs(monkeypatch, seat, calls)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spawn.subprocess, "Popen", _Proc)
    spawn._RUNNING.clear()
    spawn.headframe_spawn(SimpleNamespace(id="t3", assignee="dev"), str(tmp_path / "work"), board="b")
    record = spawn._RUNNING[1234]
    Path(record["log_path"]).write_text(
        '{"type":"thread.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"APPROVE\\n\\nHEADFRAME_RESULT: done plan approved"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    record["proc"].code = 0
    result = spawn.reap_finished()[0]
    assert result["result"] == "done" and "plan approved" in result["summary"]
    assert any(item[0] == "complete" for item in calls)


def test_parse_result_prefers_verdict_over_trailing_result():
    """Finding #25: disciplined review workers emit HEADFRAME_VERDICT then
    HEADFRAME_RESULT (both contracts demand 'last line'). The verdict JSON is
    what parse_verdict needs — it must win over the trailing result line."""
    text = (
        "review done\n"
        'HEADFRAME_VERDICT: {"outcome":"request_changes","target_step":"build","body":"BLOCKER a.py:1 — x"}\n'
        "HEADFRAME_RESULT: done Verification requested changes\n"
    )
    result, summary = spawn._parse_result(text, 0)
    assert result == "done" and summary.startswith("HEADFRAME_VERDICT:")
    # Non-review workers keep the plain result contract.
    result, summary = spawn._parse_result("work\nHEADFRAME_RESULT: done shipped\n", 0)
    assert (result, summary) == ("done", "shipped")
