from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from factory import watchdog


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
    store = types.ModuleType("factory.store")
    store.due_monitors = lambda now: [{"task_id": "T1", "recovery_policy": "wake_owner", "scheduled_by": "seat", "notes": "ping"}]
    store.bump_monitor = lambda task_id: calls.append(("bump", task_id))
    config = types.ModuleType("factory.config")
    config.load_seats = lambda: SimpleNamespace(company="demo")
    hierarchy = types.ModuleType("factory.hierarchy")
    hierarchy.escalation_target = lambda cfg, seat: "manager"
    monkeypatch.setitem(sys.modules, "factory.store", store)
    monkeypatch.setitem(sys.modules, "factory.config", config)
    monkeypatch.setitem(sys.modules, "factory.hierarchy", hierarchy)

    def run(command, **kwargs):
        calls.append(command)
        if "show" in command:
            return SimpleNamespace(stdout='{"id":"T1","title":"Task","assignee":"seat"}')
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(watchdog.subprocess, "run", run)
    result = watchdog.tick("demo", "2026-07-12T00:00:00Z")
    assert result == [{"task_id": "T1", "action": "wake_owner"}]
    assert any("comment" in call for call in calls if isinstance(call, list))
    assert ("bump", "T1") in calls
