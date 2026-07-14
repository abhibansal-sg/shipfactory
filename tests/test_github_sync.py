from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from headframe import github_sync


def test_github_new_issue_creates_task_and_mapping(monkeypatch):
    calls = []
    state = {}
    store = types.ModuleType("headframe.store")
    store.sync_get = lambda number: state.get(number)
    store.sync_upsert = lambda number, task_id, gh, kanban: state.__setitem__(number, {"task_id": task_id, "gh_updated": gh, "k_updated": kanban})
    monkeypatch.setitem(sys.modules, "headframe.store", store)

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            return SimpleNamespace(stdout=json.dumps([{
                "number": 7, "title": "Fix", "body": "details", "state": "OPEN",
                "labels": [{"name": "seat:dev"}, {"name": "priority:P1"}], "milestone": {"title": "Q3"},
                "updatedAt": "2026-07-12T00:00:00Z",
            }]))
        if command[:3] == ["hermes", "kanban", "--board"] and "list" in command:
            return SimpleNamespace(stdout="[]")
        return SimpleNamespace(stdout='{"id":"K7"}')

    monkeypatch.setattr(github_sync.subprocess, "run", run)
    result = github_sync.sync_once("demo", "owner/repo")
    assert result["created"] == 1
    assert state[7]["task_id"] == "K7"
    assert any("--assignee" in call and "dev" in call for call in calls)


def test_newer_github_side_wins_and_logs_conflict(monkeypatch, tmp_path):
    state = {3: {"task_id": "K3", "gh_updated": "2026-07-11T00:00:00Z", "k_updated": "2026-07-11T00:00:00Z"}}
    calls = []
    store = types.ModuleType("headframe.store")
    store.sync_get = lambda number: state.get(number)
    store.sync_upsert = lambda *args: state.__setitem__(args[0], {"task_id": args[1], "gh_updated": args[2], "k_updated": args[3]})
    monkeypatch.setitem(sys.modules, "headframe.store", store)
    config = types.ModuleType("headframe.config")
    config.load_seats = lambda: SimpleNamespace(company="demo")
    policy = types.ModuleType("headframe.policy")
    policy.policy_satisfied = lambda task_id: True
    monkeypatch.setitem(sys.modules, "headframe.config", config)
    monkeypatch.setitem(sys.modules, "headframe.policy", policy)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            return SimpleNamespace(stdout=json.dumps([{
                "number": 3, "title": "GH title", "body": "GH body", "state": "OPEN", "labels": [], "milestone": None,
                "updatedAt": "2026-07-12T00:00:02Z",
            }]))
        if "list" in command:
            return SimpleNamespace(stdout=json.dumps([{"id": "K3", "title": "Old", "body": "old", "status": "ready", "updated_at": "2026-07-12T00:00:01Z"}]))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(github_sync.subprocess, "run", run)
    result = github_sync.sync_once("demo", "owner/repo")
    assert result == {"created": 0, "github_to_kanban": 1, "kanban_to_github": 0, "conflicts": 1, "skipped": 0}
    log = Path(tmp_path) / "headframe" / "sync-conflicts.jsonl"
    assert log.exists()
    assert json.loads(log.read_text())["winner"] == "github"
    assert any("edit" in call for call in calls)
