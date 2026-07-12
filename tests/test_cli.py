"""Operator CLI contract tests."""

from __future__ import annotations

import argparse
import json
import sys
import types

from factory import cli


VERBS = {"init", "seats", "org", "daemon", "verdict", "policy", "monitor", "watchdog", "costs", "sync", "dashboard", "runs", "pause", "resume"}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    cli.register_cli(value)
    return value


def test_all_spec_verbs_registered():
    action = next(action for action in parser()._actions if isinstance(action, argparse._SubParsersAction))
    assert set(action.choices) == VERBS


def test_init_writes_skeleton_and_initializes_store(tmp_path, monkeypatch):
    called = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("factory.store.init_db", lambda: called.append(True))
    cli.main(["init"])
    text = (tmp_path / "factory" / "seats.yaml").read_text()
    assert "company: straits-lab-eng" in text and "verifier:" in text
    assert called == [True]


def test_pause_and_resume_use_store_contract(monkeypatch):
    calls = []
    monkeypatch.setattr("factory.store.set_seat_paused", lambda seat, paused: calls.append((seat, paused)))
    cli.main(["pause", "dev"]); cli.main(["resume", "dev"])
    assert calls == [("dev", True), ("dev", False)]


def test_costs_parses_since_duration(monkeypatch):
    calls = []
    monkeypatch.setattr("factory.store.costs_rollup", lambda by, days: calls.append((by, days)) or [])
    cli.main(["costs", "--by", "executor", "--since", "14d"])
    assert calls == [("executor", 14)]


def test_policy_set_uses_frozen_signature(monkeypatch):
    calls = []
    monkeypatch.setattr("factory.store.set_policy", lambda task, policy: calls.append((task, policy)))
    cli.main(["policy", "set", "T-1", "--json", '{"mode":"manual","stages":[]}'])
    assert calls == [("T-1", {"mode": "manual", "stages": []})]


def test_verdict_argument_order(monkeypatch):
    calls = []
    monkeypatch.setattr("factory.policy.record_verdict", lambda *args: calls.append(args) or {"ok": True})
    cli.main(["verdict", "T-2", "--stage", "review", "--outcome", "approve", "--body", "APPROVE clean pass", "--seat", "verifier"])
    assert calls == [("T-2", "review", "approve", "APPROVE clean pass", "verifier")]


def test_sync_delegates_explicitly(monkeypatch):
    calls = []
    monkeypatch.setattr("factory.github_sync.sync", lambda **kw: calls.append(kw) or {})
    cli.main(["sync", "--board", "acme", "--repo", "o/r"])
    assert calls == [{"board": "acme", "repo": "o/r"}]
