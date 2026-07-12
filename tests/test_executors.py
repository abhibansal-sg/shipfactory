from types import SimpleNamespace

from factory.executors import get_executor


def test_codex_command_and_usage(tmp_path):
    seat = SimpleNamespace(profile="dev", model="gpt-5", reasoning="medium")
    executor = get_executor("codex")
    cmd = executor.build_cmd(seat, "prompt", str(tmp_path))
    assert cmd[:5] == ["codex", "exec", "--json", "--skip-git-repo-check", "-s"]
    assert "workspace-write" in cmd and cmd[-1] == "-"
    assert executor.parse_usage('{"usage":{"input_tokens":12,"output_tokens":7}}')['tokens_total'] == 19
    assert executor.parse_usage("tokens used\n1,234") == {"tokens_in": 0, "tokens_out": 1234, "tokens_total": 1234}


def test_claude_command_and_stream_usage(tmp_path):
    seat = SimpleNamespace(profile="qa", model="sonnet", reasoning="adaptive")
    executor = get_executor("claude")
    cmd = executor.build_cmd(seat, "prompt", str(tmp_path))
    assert cmd[:5] == ["claude", "--print", "-", "--output-format", "stream-json"]
    assert "--effort" in cmd and "--add-dir" in cmd
    usage = executor.parse_usage('{"type":"result","usage":{"input_tokens":10,"output_tokens":4}}')
    assert usage == {"tokens_in": 10, "tokens_out": 4, "tokens_total": 14}


def test_identity_copy_and_unknown_executor(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "dev"
    profile.mkdir(parents=True)
    (profile / "AGENTS.md").write_text("be precise")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seat = SimpleNamespace(profile="dev")
    get_executor("codex").identity_files(seat, str(tmp_path / "work"))
    assert (tmp_path / "work" / "AGENTS.md").read_text() == "be precise"
    try:
        get_executor("missing")
    except ValueError:
        pass
    else:
        assert False, "unknown executors must be rejected"
