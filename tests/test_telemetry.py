import json

from shipfactory.telemetry import append_jsonl, hello_shakedown, on_claim, parse_usage

CODEX_USAGE = "tokens used\n152,138"
CLAUDE_USAGE = '{"usage":{"input_tokens":100,"output_tokens":50}}'


def test_usage_fixture_strings():
    assert parse_usage("codex", CODEX_USAGE) == {"tokens_in": 0, "tokens_out": 152138, "tokens_total": 152138}
    assert parse_usage("claude", CLAUDE_USAGE) == {"tokens_in": 100, "tokens_out": 50, "tokens_total": 150}


def test_jsonl_and_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    append_jsonl({"event": "custom", "n": 1})
    on_claim("T1", "demo", "dev", run_id=9)
    rows = [json.loads(line) for line in (tmp_path / "shipfactory" / "telemetry.jsonl").read_text().splitlines()]
    assert rows[0] == {"event": "custom", "n": 1}
    assert rows[1]["task_id"] == "T1" and rows[1]["run_id"] == 9


def test_hello_shakedown():
    assert hello_shakedown() == "shipfactory-live"
