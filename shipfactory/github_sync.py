"""Explicit two-way GitHub Issues ↔ Hermes kanban synchronization."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_COMMAND_TIMEOUT = 30.0


def _module(name: str) -> Any:
    """Import a sibling module lazily."""

    return importlib.import_module(name)


def _run(command: list[str]) -> str:
    """Run a CLI command and return stdout."""

    result = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT
    )  # #16-V2: a hung gh/hermes child must not wedge a daemon tick.
    return result.stdout or ""


def _json_command(command: list[str]) -> Any:
    """Run a JSON-producing command."""

    text = _run(command)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"expected JSON from {' '.join(command)}") from exc


def _board(board: str | None) -> str:
    """Resolve a board from an explicit argument or Factory config."""

    if board:
        return board
    return str(getattr(_module("shipfactory.config").load_seats(), "company", ""))


def _gh_issues(repo: str) -> list[dict[str, Any]]:
    """List all open and closed issues with the fields needed by the mapper."""

    value = _json_command([
        "gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
        "--json", "number,title,body,state,labels,milestone,updatedAt",
    ])
    return list(value if isinstance(value, list) else value.get("issues", []))


def _kanban_tasks(board: str) -> list[dict[str, Any]]:
    """List board tasks through the Hermes CLI boundary."""

    value = _json_command(["hermes", "kanban", "--board", board, "list", "--json"])
    return list(value if isinstance(value, list) else value.get("tasks", []))


def _labels(issue: dict[str, Any]) -> list[str]:
    """Extract GitHub label names from either compact or full JSON shape."""

    values = []
    for label in issue.get("labels", []) or []:
        values.append(str(label.get("name") if isinstance(label, dict) else label))
    return values


def _mapping(store: Any, issue_number: int) -> dict[str, Any] | None:
    """Read one sync mapping."""

    return store.sync_get(issue_number)


def _timestamp(value: Any) -> float:
    """Convert an ISO timestamp to a comparable UTC epoch."""

    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _now() -> str:
    """Return the current UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _conflict_log(winner: str, loser: str, issue: dict[str, Any], task: dict[str, Any]) -> None:
    """Append a conflict record without introducing another state store."""

    home = os.environ.get("HERMES_HOME")
    if not home:
        return
    path = Path(home) / "shipfactory" / "sync-conflicts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": _now(),
        "gh_number": issue.get("number"),
        "task_id": task.get("id", task.get("task_id")),
        "winner": winner,
        "loser": loser,
        "github_updated_at": issue.get("updatedAt"),
        "kanban_updated_at": task.get("updated_at", task.get("updatedAt")),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _create_task(board: str, issue: dict[str, Any]) -> str:
    """Create a mapped kanban task from a GitHub issue."""

    labels = _labels(issue)
    seat = next((label[5:] for label in labels if label.startswith("seat:")), None)
    priority = next((label[9:] for label in labels if label.startswith("priority:")), None)
    args = ["create", "--title", str(issue.get("title", "")), "--body", str(issue.get("body") or "")]
    if seat:
        args += ["--assignee", seat]
    if priority:
        args += ["--priority", priority]
    milestone = issue.get("milestone")
    if milestone:
        goal = milestone.get("title") if isinstance(milestone, dict) else milestone
        args += ["--goal-tag", str(goal)]
    value = _json_command(["hermes", "kanban", "--board", board, *args])
    if isinstance(value, dict):
        task_id = value.get("id") or value.get("task_id") or value.get("number")
    else:
        task_id = str(value)
    if not task_id:
        raise RuntimeError("kanban create did not return a task id")
    return str(task_id)


def _edit_task(board: str, task_id: str, issue: dict[str, Any]) -> None:
    """Apply GitHub title, body, assignee, priority, and milestone to kanban."""

    labels = _labels(issue)
    args = ["edit", task_id, "--title", str(issue.get("title", "")), "--body", str(issue.get("body") or "")]
    seat = next((label[5:] for label in labels if label.startswith("seat:")), None)
    priority = next((label[9:] for label in labels if label.startswith("priority:")), None)
    if seat:
        args += ["--assignee", seat]
    if priority:
        args += ["--priority", priority]
    milestone = issue.get("milestone")
    if milestone:
        args += ["--goal-tag", str(milestone.get("title") if isinstance(milestone, dict) else milestone)]
    _run(["hermes", "kanban", "--board", board, *args])


def _edit_github(repo: str, issue: dict[str, Any], task: dict[str, Any]) -> None:
    """Apply kanban fields back to GitHub."""

    number = str(issue["number"])
    title = str(task.get("title", ""))
    body = str(task.get("body", ""))
    state = "closed" if str(task.get("status", "")).lower() == "done" else "open"
    command = ["gh", "issue", "edit", number, "--repo", repo, "--title", title, "--body", body, "--state", state]
    assignee = task.get("assignee") or task.get("assignee_name")
    priority = task.get("priority")
    if assignee:
        command += ["--add-label", f"seat:{assignee}"]
    if priority:
        command += ["--add-label", f"priority:{priority}"]
    milestone = task.get("goal_tag") or task.get("milestone")
    if milestone:
        command += ["--milestone", str(milestone.get("title") if isinstance(milestone, dict) else milestone)]
    _run(command)


def _github_closed_is_allowed(task_id: str) -> bool:
    """Ask the policy engine whether a GitHub close may become kanban done."""

    return bool(_module("shipfactory.policy").policy_satisfied(task_id))


def _apply_github_to_task(board: str, issue: dict[str, Any], task: dict[str, Any]) -> None:
    """Synchronize the winning GitHub representation to kanban."""

    task_id = str(task.get("id", task.get("task_id")))
    _edit_task(board, task_id, issue)
    if str(issue.get("state", "")).lower() == "closed":
        if _github_closed_is_allowed(task_id):
            _run(["hermes", "kanban", "--board", board, "complete", task_id])
        else:
            _run(["hermes", "kanban", "--board", board, "comment", task_id, "--body", "GitHub issue closed; pending stages."])


def sync_once(board: str | None = None, repo: str | None = None) -> dict[str, int]:
    """Perform one explicit, non-daemon synchronization pass."""

    board_name = _board(board)
    if not repo:
        raise ValueError("repo is required for GitHub sync")
    store = _module("shipfactory.store")
    issues = _gh_issues(repo)
    tasks = _kanban_tasks(board_name)
    by_task = {str(task.get("id", task.get("task_id"))): task for task in tasks}
    counts = {"created": 0, "github_to_kanban": 0, "kanban_to_github": 0, "conflicts": 0, "skipped": 0}

    for issue in issues:
        number = int(issue["number"])
        mapping = _mapping(store, number)
        if not mapping:
            task_id = _create_task(board_name, issue)
            if str(issue.get("state", "")).lower() == "closed":
                if _github_closed_is_allowed(task_id):
                    _run(["hermes", "kanban", "--board", board_name, "complete", task_id])
                else:
                    _run(["hermes", "kanban", "--board", board_name, "comment", task_id, "--body", "GitHub issue closed; pending stages."])
            store.sync_upsert(number, task_id, issue.get("updatedAt"), _now())
            counts["created"] += 1
            continue

        task_id = str(mapping.get("task_id"))
        task = by_task.get(task_id)
        if not task:
            counts["skipped"] += 1
            continue
        gh_at = _timestamp(issue.get("updatedAt", mapping.get("gh_updated")))
        kanban_at = _timestamp(task.get("updated_at", task.get("updatedAt", mapping.get("k_updated"))))
        if gh_at == 0 and kanban_at == 0:
            counts["skipped"] += 1
            continue
        if gh_at >= kanban_at:
            if gh_at and kanban_at:
                counts["conflicts"] += 1
                _conflict_log("github", "kanban", issue, task)
            _apply_github_to_task(board_name, issue, task)
            store.sync_upsert(number, task_id, issue.get("updatedAt"), task.get("updated_at", task.get("updatedAt")))
            counts["github_to_kanban"] += 1
        else:
            if gh_at and kanban_at:
                counts["conflicts"] += 1
                _conflict_log("kanban", "github", issue, task)
            _edit_github(repo, issue, task)
            store.sync_upsert(number, task_id, issue.get("updatedAt"), task.get("updated_at", task.get("updatedAt")))
            counts["kanban_to_github"] += 1
    return counts


def sync(board: str | None = None, repo: str | None = None) -> dict[str, int]:
    """Public alias for one explicit synchronization pass."""

    return sync_once(board=board, repo=repo)


def tick(board: str | None = None, repo: str | None = None) -> dict[str, int]:
    """Run a daemon-compatible sync tick using ``HERMES_GITHUB_REPO`` if set."""

    return sync_once(board=board, repo=repo or os.environ.get("HERMES_GITHUB_REPO"))


__all__ = ["sync", "sync_once", "tick"]
