import sys
import types
from pathlib import Path
from types import SimpleNamespace

import shipfactory.spawn as spawn


def _install_stubs(monkeypatch, seat, calls):
    config = types.ModuleType("shipfactory.config")
    config.load_seats = lambda: SimpleNamespace(seats={seat.name: seat})
    store = types.ModuleType("shipfactory.store")
    store.seat_paused = lambda name: False
    store.record_run_start = lambda *args: calls.append(("start", args)) or 41
    store.record_run_end = lambda *args: calls.append(("end", args))
    monkeypatch.setitem(sys.modules, "shipfactory.config", config)
    monkeypatch.setitem(sys.modules, "shipfactory.store", store)
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
    assert spawn.shipfactory_spawn(task, str(tmp_path / "work"), board="b") == 1234
    record = spawn._RUNNING[1234]
    Path(record["log_path"]).write_text('{"usage":{"input_tokens":2,"output_tokens":3}}\nSHIPFACTORY_RESULT: done shipped\n')
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
    spawn.shipfactory_spawn(SimpleNamespace(id="t2", assignee="dev"), str(tmp_path / "work"))
    record = spawn._RUNNING[1234]
    Path(record["log_path"]).write_text("finished\n")
    record["proc"].code = 0
    result = spawn.reap_finished()[0]
    assert result["result"] == "blocked" and result["summary"] == "no result sentinel"
    assert any(item[0] == "block" for item in calls)


def test_unknown_seat_skips(monkeypatch):
    config = types.ModuleType("shipfactory.config")
    config.load_seats = lambda: SimpleNamespace(seats={})
    store = types.ModuleType("shipfactory.store")
    store.seat_paused = lambda name: False
    monkeypatch.setitem(sys.modules, "shipfactory.config", config)
    monkeypatch.setitem(sys.modules, "shipfactory.store", store)
    assert spawn.shipfactory_spawn(SimpleNamespace(id="t", assignee="nobody"), "/tmp") is None


def test_reap_codex_jsonl_sentinel_completes(monkeypatch, tmp_path):
    """Finding #23 end-to-end: codex --json output must reap as done, not
    'no result sentinel' — the exact failure that fused t_737aec66."""
    calls = []
    seat = SimpleNamespace(name="dev", profile="dev", executor="codex", model="gpt", reasoning="medium")
    _install_stubs(monkeypatch, seat, calls)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spawn.subprocess, "Popen", _Proc)
    spawn._RUNNING.clear()
    spawn.shipfactory_spawn(SimpleNamespace(id="t3", assignee="dev"), str(tmp_path / "work"), board="b")
    record = spawn._RUNNING[1234]
    Path(record["log_path"]).write_text(
        '{"type":"thread.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"APPROVE\\n\\nSHIPFACTORY_RESULT: done plan approved"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    record["proc"].code = 0
    result = spawn.reap_finished()[0]
    assert result["result"] == "done" and "plan approved" in result["summary"]
    assert any(item[0] == "complete" for item in calls)


def test_spawn_inlines_full_task_body_when_host_context_caps_it(monkeypatch, tmp_path):
    """Amendment H: deployed Hermes truncates task bodies to 8 KB inside
    build_worker_context, silently clipping Factory-inlined sealed review
    inputs. Any over-cap body must be re-delivered untruncated in the prompt."""
    calls = []
    seat = SimpleNamespace(name="dev", profile="dev", executor="codex", model="gpt", reasoning="medium")
    _install_stubs(monkeypatch, seat, calls)
    kanban = sys.modules["hermes_cli.kanban_db"]
    cap = 8 * 1024
    body = ("x" * (cap + 500)) + "\nSHIPFACTORY_REVIEW_INPUT_SHA256: tail-marker-beyond-cap"
    kanban._CTX_MAX_BODY_BYTES = cap
    kanban.get_task = lambda conn, task_id: SimpleNamespace(id=task_id, body=body)
    kanban.build_worker_context = (
        lambda conn, task_id: "## Body\n" + body[:cap] + "… [truncated]"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spawn.subprocess, "Popen", _Proc)
    spawn._RUNNING.clear()
    spawn.shipfactory_spawn(SimpleNamespace(id="t9", assignee="dev"), str(tmp_path / "work"), board="b")
    prompt = Path(spawn._RUNNING[1234]["prompt_path"]).read_text()
    assert "SHIPFACTORY_REVIEW_INPUT_SHA256: tail-marker-beyond-cap" in prompt
    assert "## Factory full task body (host context truncated it above)" in prompt


def test_spawn_leaves_under_cap_bodies_alone(monkeypatch, tmp_path):
    calls = []
    seat = SimpleNamespace(name="dev", profile="dev", executor="codex", model="gpt", reasoning="medium")
    _install_stubs(monkeypatch, seat, calls)
    kanban = sys.modules["hermes_cli.kanban_db"]
    kanban._CTX_MAX_BODY_BYTES = 8 * 1024
    kanban.get_task = lambda conn, task_id: SimpleNamespace(id=task_id, body="short body")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spawn.subprocess, "Popen", _Proc)
    spawn._RUNNING.clear()
    spawn.shipfactory_spawn(SimpleNamespace(id="t10", assignee="dev"), str(tmp_path / "work"), board="b")
    prompt = Path(spawn._RUNNING[1234]["prompt_path"]).read_text()
    assert "Factory full task body" not in prompt


def test_parse_result_prefers_verdict_over_trailing_result():
    """Finding #25: disciplined review workers emit SHIPFACTORY_VERDICT then
    SHIPFACTORY_RESULT (both contracts demand 'last line'). The verdict JSON is
    what parse_verdict needs — it must win over the trailing result line."""
    text = (
        "review done\n"
        'SHIPFACTORY_VERDICT: {"outcome":"request_changes","target_step":"build","body":"BLOCKER a.py:1 — x"}\n'
        "SHIPFACTORY_RESULT: done Verification requested changes\n"
    )
    result, summary = spawn._parse_result(text, 0)
    assert result == "done" and summary.startswith("SHIPFACTORY_VERDICT:")
    # Non-review workers keep the plain result contract.
    result, summary = spawn._parse_result("work\nSHIPFACTORY_RESULT: done shipped\n", 0)
    assert (result, summary) == ("done", "shipped")
