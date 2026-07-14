from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from shipfactory import store
from shipfactory import watchdog


def issue(issue_id, *, parent=None, status="ready", updated="2026-07-12T00:00:00Z"):
    return {
        "id": issue_id, "companyId": "c", "parentId": parent, "status": status,
        "title": issue_id, "identifier": issue_id, "updatedAt": updated,
        "assigneeAgentId": "seat", "assigneeUserId": None,
    }


def test_classifier_stopped_then_fingerprint_skip():
    data = {
        "watchdog": {"companyId": "c", "issueId": "root"},
        "issues": [issue("root"), issue("leaf", parent="root")],
    }
    first = watchdog.classify_task_watchdog_subtree(data)
    assert first["state"] == "stopped"
    data["watchdog"]["lastReviewedFingerprint"] = first["stopFingerprint"]
    assert watchdog.classify_subtree(data)["state"] == "already_reviewed"


def test_classifier_live_and_pending_verdict_are_not_stopped():
    data = {"watchdog": {"companyId": "c", "issueId": "root"}, "issues": [issue("root"), issue("leaf", parent="root")]}
    data["activeRuns"] = [{"companyId": "c", "issueId": "leaf"}]
    assert watchdog.classify_task_watchdog_subtree(data)["state"] == "live"
    data.pop("activeRuns")
    data["pendingApprovals"] = [{"companyId": "c", "issueId": "leaf", "id": "approval-1", "status": "pending"}]
    assert watchdog.classify_task_watchdog_subtree(data)["state"] == "live"


def test_tick_uses_recovery_ladder_and_kanban_cli(monkeypatch):
    calls = []
    store = types.ModuleType("shipfactory.store")
    store.due_monitors = lambda now: [{"task_id": "T1", "recovery_policy": "wake_owner", "scheduled_by": "seat", "notes": "ping", "max_attempts": 3}]
    store.advance_monitor = lambda task_id, now, close=False: calls.append(("advance", task_id, now, close))
    config = types.ModuleType("shipfactory.config")
    config.load_seats = lambda: SimpleNamespace(company="demo")
    hierarchy = types.ModuleType("shipfactory.hierarchy")
    hierarchy.escalation_target = lambda cfg, seat: "manager"
    monkeypatch.setitem(sys.modules, "shipfactory.store", store)
    monkeypatch.setitem(sys.modules, "shipfactory.config", config)
    monkeypatch.setitem(sys.modules, "shipfactory.hierarchy", hierarchy)

    def run(command, **kwargs):
        calls.append(command)
        if "show" in command:
            return SimpleNamespace(stdout='{"id":"T1","title":"Task","assignee":"seat"}')
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(watchdog.subprocess, "run", run)
    result = watchdog.tick("demo", "2026-07-12T00:00:00Z")
    assert result == [{"task_id": "T1", "action": "wake_owner"}]
    assert any("comment" in call for call in calls if isinstance(call, list))
    assert ("advance", "T1", "2026-07-12T00:00:00Z", False) in calls


def test_due_monitor_reschedules_after_one_action(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.add_monitor("T1", "2026-07-12T00:00:00+00:00", None, 3,
                      "wake_owner", "ping", "seat", 60)
    monkeypatch.setattr(watchdog, "_task", lambda task_id, board: {
        "id": task_id, "title": "Task", "assignee": "seat", "status": "ready",
    })
    monkeypatch.setattr(watchdog, "_run_kanban", lambda *args, **kwargs: None)

    assert watchdog.tick("demo", "2026-07-12T00:00:00Z") == [
        {"task_id": "T1", "action": "wake_owner"}
    ]
    assert watchdog.tick("demo", "2026-07-12T00:00:00Z") == []
    assert store.due_monitors("2026-07-12T00:00:00Z") == []


def test_timeout_closes_monitor_without_action(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.add_monitor("T1", "2026-07-12T01:00:00+00:00",
                      "2026-07-12T00:00:00+00:00", 3, "wake_owner", "ping", "seat", 60)
    monkeypatch.setattr(watchdog, "_task", lambda *args: pytest.fail("timed-out monitor read its task"))

    assert watchdog.tick("demo", "2026-07-12T00:00:00Z") == [
        {"task_id": "T1", "action": "closed", "reason": "timeout"}
    ]
    assert store.due_monitors("9999-12-31T00:00:00+00:00") == []


def test_max_attempts_closes_monitor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.add_monitor("T1", "2026-07-12T00:00:00+00:00", None, 1,
                      "wake_owner", "ping", "seat", 60)
    monkeypatch.setattr(watchdog, "_task", lambda task_id, board: {
        "id": task_id, "title": "Task", "assignee": "seat", "status": "ready",
    })
    monkeypatch.setattr(watchdog, "_run_kanban", lambda *args, **kwargs: None)

    assert watchdog.tick("demo", "2026-07-12T00:00:00Z")[0]["action"] == "wake_owner"
    assert store.due_monitors("9999-12-31T00:00:00+00:00") == []


def test_terminal_escalation_happens_at_most_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.add_monitor("T1", "2026-07-12T00:00:00+00:00", None, 5,
                      "escalate_to_board", "ping", "seat", 60)
    monkeypatch.setattr(watchdog, "_task", lambda task_id, board: {
        "id": task_id, "title": "Task", "assignee": "seat", "status": "ready",
    })
    monkeypatch.setitem(sys.modules, "shipfactory.config", types.SimpleNamespace(
        load_seats=lambda: SimpleNamespace(company="demo")
    ))
    monkeypatch.setitem(sys.modules, "shipfactory.hierarchy", types.SimpleNamespace(
        escalation_target=lambda cfg, seat: None
    ))
    calls = []
    monkeypatch.setattr(watchdog, "_run_kanban", lambda board, args, **kwargs: calls.append(args))

    assert watchdog.tick("demo", "2026-07-12T00:00:00Z")[0]["action"] == "escalate_to_board"
    assert watchdog.tick("demo", "2026-07-12T01:00:00Z") == []
    assert sum(any(str(value).startswith("Escalation:") for value in args) for args in calls) == 1
