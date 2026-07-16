from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from shipfactory import policy


def install_fakes(monkeypatch, *, stored_policy=None):
    decisions = []
    state = {"policy": stored_policy, "set": [], "records": decisions}
    store = types.ModuleType("shipfactory.store")
    store.get_policy = lambda task_id: state["policy"]
    store.set_policy = lambda task_id, value: (state["set"].append(value), state.__setitem__("policy", value))
    store.decisions_for = lambda task_id: list(decisions)
    store.record_decision = lambda *args: decisions.append({
        "task_id": args[0], "stage_id": args[1], "stage_type": args[2], "seat": args[3], "outcome": args[4], "body": args[5]
    })
    config = types.ModuleType("shipfactory.config")
    config.load_seats = lambda: SimpleNamespace(
        seats={"verifier": SimpleNamespace(role="qa"), "architect": SimpleNamespace(role="architect"), "release": SimpleNamespace(role="release")},
        hierarchy_gates={"verdicts": ["verifier"], "landers": ["release"]},
        company="demo",
    )
    hierarchy = types.ModuleType("shipfactory.hierarchy")
    hierarchy.may_verdict = lambda cfg, seat: seat == "verifier"
    monkeypatch.setitem(sys.modules, "shipfactory.store", store)
    monkeypatch.setitem(sys.modules, "shipfactory.config", config)
    monkeypatch.setitem(sys.modules, "shipfactory.hierarchy", hierarchy)
    return state


def test_citation_gate_matches_governor_and_clean_exemption():
    assert policy.citation_ok("Finding: factory/policy.py:18 is unsafe")
    assert policy.citation_ok("APPROVE: clean pass; no findings")
    assert not policy.citation_ok("APPROVE")
    assert not policy.citation_ok("Looks good, but no proof")


def test_completion_without_stored_policy_is_noop(monkeypatch):
    state = install_fakes(monkeypatch)
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda command, **kwargs: calls.append(command))

    result = policy.on_complete("T1", "demo", "dev", "implemented")

    assert result == {"action": "allow", "next_stage": None}
    assert state["policy"] is None
    assert state["set"] == []
    assert state["records"] == []
    assert calls == []


def test_completion_with_stored_policy_reopens_and_routes_first_stage(monkeypatch):
    policy_data = {
        "mode": "normal", "commentRequired": True,
        "stages": [{"id": "review", "type": "review", "approvalsNeeded": 1, "participants": ["verifier"]}],
    }
    state = install_fakes(monkeypatch, stored_policy=policy_data)
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(stdout=""))

    result = policy.on_complete("T1", "demo", "dev", "implemented")

    assert result == {"action": "reopen", "next_stage": "review"}
    assert state["records"][0]["outcome"] == "submitted"
    assert any("comment" in command for command in calls)
    assert any("unblock" in command for command in calls)
    assert any("verifier" in command for command in calls[-1:])


def test_verdict_requires_role_and_proof(monkeypatch):
    policy_data = {
        "mode": "normal", "commentRequired": True,
        "stages": [{"id": "review", "type": "review", "approvalsNeeded": 1, "participants": ["verifier"]}],
    }
    install_fakes(monkeypatch, stored_policy=policy_data)
    with pytest.raises(ValueError):
        policy.record_verdict("T1", "review", "approve", "APPROVE", "verifier")
    with pytest.raises(PermissionError):
        policy.record_verdict("T1", "review", "approve", "factory/policy.py:1 clean", "architect")


def test_approved_final_stage_allows(monkeypatch):
    policy_data = {
        "mode": "normal", "commentRequired": True,
        "stages": [{"id": "review", "type": "review", "approvalsNeeded": 1, "participants": ["verifier"]}],
    }
    state = install_fakes(monkeypatch, stored_policy=policy_data)
    result = policy.record_verdict("T1", "review", "approve", "APPROVE: no findings", "verifier")
    assert result == {"action": "allow", "next_stage": None}
    assert state["records"][0]["outcome"] == "approved"
