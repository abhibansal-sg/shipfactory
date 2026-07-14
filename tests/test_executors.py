from types import SimpleNamespace

from headframe.executors import get_executor


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


def test_codex_extract_text_finds_sentinel_in_jsonl():
    """Finding #23: the sentinel lives inside agent_message JSON, not on the
    raw log's last line — extract_text must surface it for _parse_result."""
    codex = get_executor("codex")
    log = "\n".join([
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls"}}',
        '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"APPROVE ok\\n\\nHEADFRAME_VERDICT: {\\"outcome\\":\\"approve\\",\\"body\\":\\"clean\\"}"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
    ])
    text = codex.extract_text(log)
    assert text.splitlines()[-1].startswith("HEADFRAME_VERDICT:")
    # Plain-text logs pass through unchanged (fallback contract).
    assert codex.extract_text("no json here\nHEADFRAME_RESULT: done x") == "no json here\nHEADFRAME_RESULT: done x"


def test_claude_extract_text_finds_sentinel_in_stream_json():
    claude = get_executor("claude")
    log = "\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}',
        '{"type":"result","result":"HEADFRAME_RESULT: done shipped the fix"}',
    ])
    text = claude.extract_text(log)
    assert text.splitlines()[-1] == "HEADFRAME_RESULT: done shipped the fix"
    assert claude.extract_text("plain log") == "plain log"


def test_hermes_extract_text_passthrough():
    assert get_executor("hermes").extract_text("raw") == "raw"


def test_codex_worktree_git_root_added_to_writable_roots(tmp_path):
    """Finding #24: linked-worktree workspaces need the parent .git granted,
    or codex's workspace-write sandbox denies index.lock and commits fail."""
    repo_git = tmp_path / "repo" / ".git" / "worktrees" / "t_x"
    repo_git.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").write_text(f"gitdir: {repo_git}\n")
    seat = SimpleNamespace(profile="dev", model="gpt-5", reasoning="")
    cmd = get_executor("codex").build_cmd(seat, "prompt", str(ws))
    joined = " ".join(cmd)
    assert "sandbox_workspace_write.writable_roots" in joined
    assert str(tmp_path / "repo" / ".git") in joined
    # Regular checkout (.git is a directory): no extra grant.
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    assert "writable_roots" not in " ".join(get_executor("codex").build_cmd(seat, "p", str(plain)))


def test_worktree_git_root_helper(tmp_path):
    from headframe.executors.base import worktree_git_root
    assert worktree_git_root(str(tmp_path)) is None  # no .git at all
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").write_text("not a pointer")
    assert worktree_git_root(str(ws)) is None  # malformed pointer
    (ws / ".git").write_text("gitdir: ../repo/.git/worktrees/t_y")
    assert worktree_git_root(str(ws)) == str((tmp_path / "repo" / ".git").resolve())
