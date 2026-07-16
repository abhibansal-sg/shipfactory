"""Operator CLI contract tests."""

from __future__ import annotations

import argparse
import json
import sys
import types

from shipfactory import cli


VERBS = {"init", "seats", "seat-create", "seat-update", "seat-list", "org", "daemon", "verdict", "policy", "monitor", "watchdog", "costs", "sync", "dashboard", "runs", "pause", "resume", "recipe"}


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
    monkeypatch.setattr("shipfactory.store.init_db", lambda: called.append(True))
    cli.main(["init"])
    text = (tmp_path / "shipfactory" / "seats.yaml").read_text()
    assert "company: straits-lab-eng" in text and "verifier:" in text
    assert called == [True]


def test_pause_and_resume_use_store_contract(monkeypatch):
    calls = []
    monkeypatch.setattr("shipfactory.store.set_seat_paused", lambda seat, paused: calls.append((seat, paused)))
    cli.main(["pause", "dev"]); cli.main(["resume", "dev"])
    assert calls == [("dev", True), ("dev", False)]


def test_costs_parses_since_duration(monkeypatch):
    calls = []
    monkeypatch.setattr("shipfactory.store.costs_rollup", lambda by, days: calls.append((by, days)) or [])
    cli.main(["costs", "--by", "executor", "--since", "14d"])
    assert calls == [("executor", 14)]


def test_policy_set_uses_frozen_signature(monkeypatch):
    calls = []
    monkeypatch.setattr("shipfactory.store.set_policy", lambda task, policy: calls.append((task, policy)))
    cli.main(["policy", "set", "T-1", "--json", '{"mode":"manual","stages":[]}'])
    assert calls == [("T-1", {"mode": "manual", "stages": []})]


def test_verdict_argument_order(monkeypatch):
    calls = []
    monkeypatch.setattr("shipfactory.policy.record_verdict", lambda *args: calls.append(args) or {"ok": True})
    cli.main(["verdict", "T-2", "--stage", "review", "--outcome", "approve", "--body", "APPROVE clean pass", "--seat", "verifier"])
    assert calls == [("T-2", "review", "approve", "APPROVE clean pass", "verifier")]


def test_sync_delegates_explicitly(monkeypatch):
    calls = []
    monkeypatch.setattr("shipfactory.github_sync.sync", lambda **kw: calls.append(kw) or {})
    cli.main(["sync", "--board", "acme", "--repo", "o/r"])
    assert calls == [{"board": "acme", "repo": "o/r"}]


def test_seat_create_uses_shared_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / "builder").mkdir(parents=True)
    result = cli.main(["seat-create", "builder", "--profile", "builder", "--executor", "codex", "--model", "gpt", "--role", "engineer"])
    assert result["name"] == "builder"
