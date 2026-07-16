"""Factory monitor recovery and subtree-watchdog classification."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from datetime import datetime, timezone
from time import monotonic
from typing import Any


TERMINAL_STATUSES = {"done", "archived", "failed", "cancelled"}
LIVE_RUN_STATUSES = {"queued", "running", "scheduled_retry", "in_progress"}
RECOVERY_LADDER = ("wake_owner", "create_recovery_task", "escalate_to_board")
_ACTIVE_COMMAND_TIMEOUT = 120.0
_ACTIVE_TICK_DEADLINE: float | None = None


def _module(name: str) -> Any:
    """Import a sibling module lazily."""

    return importlib.import_module(name)


def _iso(value: Any) -> str | None:
    """Normalize a datetime-like value to an ISO-8601 UTC string."""

    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_ms(value: Any) -> int | None:
    """Convert a datetime-like value to epoch milliseconds."""

    normalized = _iso(value)
    if normalized is None:
        return None
    return int(datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp() * 1000)


def _path_ids(paths: list[dict[str, Any]] | None, company_id: str) -> set[str]:
    """Return issue ids with a live execution path for a company."""

    return {
        str(path["issueId"])
        for path in paths or []
        if path.get("companyId") == company_id and path.get("issueId")
    }


def _waiting_ids(paths: list[dict[str, Any]] | None, company_id: str, issue_id: str) -> list[str]:
    """Return stable ids for pending interactions/approvals."""

    values = []
    for path in paths or []:
        if path.get("companyId") == company_id and path.get("issueId") == issue_id:
            values.append(str(path.get("id") or f"{path.get('status')}:{issue_id}"))
    return sorted(values)


def _stable_fingerprint(company_id: str, root_id: str, leaves: list[dict[str, Any]]) -> str:
    """Create Paperclip-compatible stopped-leaf fingerprint."""

    payload = json.dumps(
        {"version": 1, "companyId": company_id, "watchedIssueId": root_id, "leaves": leaves},
        separators=(",", ":"),
    ).encode()
    return "task_watchdog_stop:" + hashlib.sha256(payload).hexdigest()


def classify_task_watchdog_subtree(input: dict[str, Any]) -> dict[str, Any]:
    """Classify a watched subtree as live, pending-first-run, stopped, or reviewed.

    The input mirrors Paperclip's classifier DTO, with plain dictionaries in
    place of database rows.  It is intentionally pure and has no kanban or DB
    side effects.
    """

    watchdog = input.get("watchdog", {})
    company_id = str(watchdog.get("companyId", watchdog.get("company_id", "")))
    root_id = watchdog.get("issueId", watchdog.get("root_task_id"))
    issues = list(input.get("issues", []))
    by_id = {str(issue.get("id")): issue for issue in issues}
    root = by_id.get(str(root_id))
    if not root or root.get("companyId") != company_id:
        return {"state": "not_applicable", "reason": "Watched issue is missing.", "includedIssueIds": []}
    if root.get("originKind") == "task_watchdog":
        return {"state": "not_applicable", "reason": "Task watchdog origin issues cannot themselves be watched.", "includedIssueIds": []}

    children: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        if issue.get("companyId") == company_id and issue.get("parentId"):
            children.setdefault(str(issue["parentId"]), []).append(issue)
    for values in children.values():
        values.sort(key=lambda item: str(item.get("id")))

    included: list[dict[str, Any]] = []

    def visit(issue: dict[str, Any]) -> None:
        if issue.get("originKind") == "task_watchdog":
            return
        included.append(issue)
        for child in children.get(str(issue.get("id")), []):
            visit(child)

    visit(root)
    included_ids = [str(issue["id"]) for issue in included]
    if not included_ids:
        return {"state": "not_applicable", "reason": "Watched subtree has no non-watchdog issues.", "includedIssueIds": []}

    live_ids = sorted(_path_ids(input.get("activeRuns"), company_id) | _path_ids(input.get("queuedWakeRequests"), company_id) & set(included_ids))
    # Set precedence above is intentionally made explicit below; it avoids a
    # subtle Python `|`/`&` precedence mismatch for callers with extra paths.
    live_ids = sorted((_path_ids(input.get("activeRuns"), company_id) | _path_ids(input.get("queuedWakeRequests"), company_id)) & set(included_ids))
    if live_ids:
        return {
            "state": "live",
            "reason": "At least one issue in the watched subtree has a live run, queued wake, or scheduled retry.",
            "includedIssueIds": included_ids,
            "liveIssueIds": live_ids,
        }

    evaluated = _epoch_ms(input.get("evaluatedAt"))
    grace = int(input.get("firstRunGraceMs") or 0)
    if evaluated is not None and grace > 0:
        completed = {str(item) for item in input.get("completedRunIssueIds", [])}
        pending = sorted(
            str(issue["id"])
            for issue in included
            if issue.get("status") not in TERMINAL_STATUSES
            and str(issue["id"]) not in completed
            and _epoch_ms(issue.get("createdAt")) is not None
            and evaluated - _epoch_ms(issue.get("createdAt")) < grace
        )
        if pending:
            return {
                "state": "pending_first_run",
                "reason": "A watched issue was created within the first-run grace window and has not yet completed a run; deferring evaluation until its first assignment run/wake is observable.",
                "includedIssueIds": included_ids,
                "pendingIssueIds": pending,
            }

    child_ids = {str(issue.get("id")): [] for issue in included}
    included_set = set(included_ids)
    for issue in included:
        parent = issue.get("parentId")
        if parent in included_set:
            child_ids[str(parent)].append(str(issue["id"]))
    blockers: dict[str, list[str]] = {}
    for relation in input.get("blockers", []):
        if relation.get("companyId") != company_id or relation.get("blockedIssueId") not in included_set:
            continue
        blockers.setdefault(str(relation["blockedIssueId"]), []).append(str(relation["blockerIssueId"]))

    leaves: list[dict[str, Any]] = []
    for issue in sorted((item for item in included if not child_ids[str(item["id"])]), key=lambda item: str(item["id"])):
        pending_interactions = _waiting_ids(input.get("pendingInteractions"), company_id, str(issue["id"]))
        pending_approvals = _waiting_ids(input.get("pendingApprovals"), company_id, str(issue["id"]))
        if issue.get("status") == "in_progress" or pending_interactions or pending_approvals:
            continue
        leaves.append({
            "issueId": str(issue["id"]),
            "identifier": issue.get("identifier"),
            "title": issue.get("title", ""),
            "status": issue.get("status", ""),
            "assigneeAgentId": issue.get("assigneeAgentId"),
            "assigneeUserId": issue.get("assigneeUserId"),
            "blockerIssueIds": sorted(set(blockers.get(str(issue["id"]), []))),
            "pendingInteractionIds": pending_interactions,
            "pendingApprovalIds": pending_approvals,
            "updatedAt": _iso(issue.get("updatedAt")) or str(issue.get("updatedAt", "")),
            "latestCommentAt": _iso(issue.get("latestCommentAt")),
            "latestDocumentAt": _iso(issue.get("latestDocumentAt")),
            "latestWorkProductAt": _iso(issue.get("latestWorkProductAt")),
        })
    if not leaves:
        return {"state": "live", "reason": "No stopped leaves without a pending verdict.", "includedIssueIds": included_ids}

    fingerprint = _stable_fingerprint(company_id, str(root_id), leaves)
    if watchdog.get("lastReviewedFingerprint", watchdog.get("last_fingerprint")) == fingerprint:
        return {
            "state": "already_reviewed",
            "reason": "The current stopped subtree fingerprint was already reviewed by the watchdog.",
            "includedIssueIds": included_ids,
            "stopFingerprint": fingerprint,
            "stoppedLeaves": leaves,
        }
    return {
        "state": "stopped",
        "reason": "No issue in the watched subtree has a live execution path.",
        "includedIssueIds": included_ids,
        "stopFingerprint": fingerprint,
        "stoppedLeaves": leaves,
    }


def classify_subtree(input: dict[str, Any]) -> dict[str, Any]:
    """Compatibility alias for the public subtree classifier."""

    return classify_task_watchdog_subtree(input)


def _run_kanban(board: str, args: list[str], *, parse_json: bool = False,
                timeout: float | None = None) -> Any:
    """Run a kanban command through Hermes' CLI boundary."""
    command = ["hermes", "kanban", "--board", board, *args]
    bounded = float(timeout if timeout is not None else _ACTIVE_COMMAND_TIMEOUT)
    if _ACTIVE_TICK_DEADLINE is not None:
        bounded = min(bounded, max(0.0, _ACTIVE_TICK_DEADLINE - monotonic()))
    if bounded <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=bounded,
    )
    if not parse_json:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _task(task_id: str, board: str) -> dict[str, Any]:
    """Read a task through the CLI for recovery routing."""

    value = _run_kanban(board, ["show", task_id, "--json"], parse_json=True)
    return value if isinstance(value, dict) else {}


def _recovery_rung(policy: str, attempts: int) -> str | None:
    """Return the one ladder rung selected by the completed-attempt count."""

    if policy == "create_recovery_issue":
        policy = "create_recovery_task"
    try:
        index = RECOVERY_LADDER.index(policy)
    except ValueError:
        index = 0
    index += attempts
    return RECOVERY_LADDER[index] if index < len(RECOVERY_LADDER) else None


def _at_or_after(value: Any, boundary: Any) -> bool:
    """Return whether an ISO timestamp has reached an optional boundary."""

    value_ms = _epoch_ms(value)
    boundary_ms = _epoch_ms(boundary)
    return value_ms is not None and boundary_ms is not None and value_ms >= boundary_ms


def _route_recovery(row: dict[str, Any], board: str, now: str) -> dict[str, Any]:
    """Execute one monitor recovery action and return an audit result."""

    store = _module("shipfactory.store")
    task_id = str(row["task_id"])
    if _at_or_after(now, row.get("timeout_at")):
        store.advance_monitor(task_id, now, close=True)
        return {"task_id": task_id, "action": "closed", "reason": "timeout"}

    attempts = int(row.get("attempt_count", 0) or 0)
    max_attempts = int(row.get("max_attempts", 1) or 1)
    if attempts >= max_attempts:
        store.advance_monitor(task_id, now, close=True)
        return {"task_id": task_id, "action": "closed", "reason": "max_attempts"}

    task = _task(task_id, board)
    if str(task.get("status") or "") in TERMINAL_STATUSES:
        store.advance_monitor(task_id, now, close=True)
        return {"task_id": task_id, "action": "closed", "reason": "terminal_task"}

    owner = row.get("owner") or row.get("assignee") or task.get("assignee") or task.get("assignee_name")
    policy = _recovery_rung(str(row.get("recovery_policy") or "wake_owner"), attempts)
    if policy is None:
        store.advance_monitor(task_id, now, close=True)
        return {"task_id": task_id, "action": "closed", "reason": "top_rung"}

    notes = str(row.get("notes") or "Monitor check is due.")
    if policy == "wake_owner":
        if owner:
            _run_kanban(board, ["comment", task_id, "--body", f"Watchdog wake: {notes}"])
            _run_kanban(board, ["unblock", task_id])
            _run_kanban(board, ["assign", task_id, "--assignee", str(owner)])
        action = "wake_owner"
    else:
        cfg = _module("shipfactory.config").load_seats()
        hierarchy = _module("shipfactory.hierarchy")
        seat = str(owner or task.get("assignee") or "")
        target = hierarchy.escalation_target(cfg, seat) if seat else None
        if policy in {"create_recovery_task", "create_recovery_issue"}:
            assignee = target or seat or None
            args = ["create", "--title", f"Recovery: {task.get('title', task_id)}", "--body", notes]
            if assignee:
                args += ["--assignee", str(assignee)]
            _run_kanban(board, args)
            action = "create_recovery_task"
        else:
            assignee = target or None
            args = ["create", "--title", f"Escalation: {task.get('title', task_id)}", "--body", notes]
            if assignee:
                args += ["--assignee", str(assignee)]
            _run_kanban(board, args)
            action = "escalate_to_board"
    close = policy == RECOVERY_LADDER[-1] or attempts + 1 >= max_attempts
    store.advance_monitor(task_id, now, close=close)
    return {"task_id": task_id, "action": action}


def tick(conn: Any = None, board: str | None = None, now_iso: str | None = None,
         *, command_timeout_seconds: float | None = None,
         tick_timeout_seconds: float | None = None) -> list[dict[str, Any]]:
    """Run due monitor recoveries once and return the actions taken.

    ``conn`` is accepted for daemon compatibility; Factory watchdog state is
    accessed through the frozen store contract rather than this kanban
    connection.  For direct callers, ``tick("board", "timestamp")`` remains
    supported as a compact form.
    """

    global _ACTIVE_COMMAND_TIMEOUT, _ACTIVE_TICK_DEADLINE
    store = _module("shipfactory.store")
    if isinstance(conn, str) and board is not None and now_iso is None and "T" in board:
        now_iso, board, conn = board, conn, None
    elif isinstance(conn, str) and board is None:
        board, conn = conn, None
    now = now_iso or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows = store.due_monitors(now)
    if board is None:
        board = getattr(_module("shipfactory.config").load_seats(), "company", "")
    if command_timeout_seconds is None or tick_timeout_seconds is None:
        try:
            config = _module("shipfactory.config")
            cfg = config.load_seats()
            limits = config.recipe_runtime_config(getattr(cfg, "recipes", None))
        except Exception:
            limits = {
                "watchdog_subprocess_timeout_seconds": 120,
                "watchdog_tick_timeout_seconds": 120,
            }
        command_timeout_seconds = command_timeout_seconds or limits[
            "watchdog_subprocess_timeout_seconds"
        ]
        tick_timeout_seconds = tick_timeout_seconds or limits[
            "watchdog_tick_timeout_seconds"
        ]
    _ACTIVE_COMMAND_TIMEOUT = float(command_timeout_seconds)
    _ACTIVE_TICK_DEADLINE = monotonic() + float(tick_timeout_seconds)
    outcomes: list[dict[str, Any]] = []
    try:
        for row in rows:
            if monotonic() >= _ACTIVE_TICK_DEADLINE:
                break
            task_id = str(row["task_id"])
            try:
                outcome = _route_recovery(row, board, now)
            except subprocess.TimeoutExpired as exc:
                error = f"kanban subprocess timed out after {exc.timeout}s"
                if hasattr(store, "record_monitor_outcome"):
                    store.record_monitor_outcome(task_id, "timed_out", error)
                attempts = int(row.get("attempt_count", 0) or 0) + 1
                close = attempts >= int(row.get("max_attempts", 1) or 1)
                store.advance_monitor(task_id, now, close=close)
                outcome = {"task_id": task_id, "action": "timed_out", "reason": error}
            else:
                if hasattr(store, "record_monitor_outcome"):
                    store.record_monitor_outcome(
                        task_id, str(outcome.get("action") or "checked"),
                        str(outcome.get("reason")) if outcome.get("reason") else None,
                    )
            outcomes.append(outcome)
    finally:
        _ACTIVE_TICK_DEADLINE = None
        _ACTIVE_COMMAND_TIMEOUT = 120.0
    return outcomes


def reconcile_watchdog(input: dict[str, Any], board: str | None = None) -> dict[str, Any]:
    """Classify one watchdog input and persist its changed fingerprint."""

    result = classify_task_watchdog_subtree(input)
    if result.get("state") == "stopped":
        store = _module("shipfactory.store")
        watchdog = input.get("watchdog", {})
        root = watchdog.get("issueId", watchdog.get("root_task_id"))
        store.set_watchdog_fingerprint(root, result["stopFingerprint"])
        if board and watchdog.get("agent"):
            leaves = ", ".join(str(item.get("issueId")) for item in result.get("stoppedLeaves", []))
            _run_kanban(board, ["create", "--title", f"Watchdog review: {root}", "--body", f"{watchdog.get('instructions', '')}\nStopped leaves: {leaves}", "--assignee", str(watchdog["agent"])])
    return result


__all__ = ["classify_subtree", "classify_task_watchdog_subtree", "reconcile_watchdog", "tick"]
