"""Execution-policy gates for Factory tasks.

The module deliberately imports Factory's store, config, and hierarchy only at
the point of use.  Lane A can therefore install those modules independently,
while tests can provide small contract-compatible fakes.
"""

from __future__ import annotations

import importlib
import re
import subprocess
from typing import Any


_CITATION_SUFFIX = re.compile(r"\.(?:py|swift|ts|tsx|js|mjs|cjs|md|yaml|yml|sh|json|m|h|c|cpp|rs|go):\d+")
_CLEAN_APPROVE = re.compile(
    r"\bAPPROVE\b.*(?:no (?:findings|issues|regressions|violations)|nothing to cite|clean pass)",
    re.IGNORECASE | re.DOTALL,
)


def _module(name: str) -> Any:
    """Import a sibling module lazily."""

    return importlib.import_module(name)


def citation_ok(body: str) -> bool:
    """Return whether *body* has line-level proof or is an explicit clean pass."""

    text = str(body or "").strip()
    # #16-V2: scan the bounded citation suffix, then walk its path prefix
    # linearly.  A greedy path regex backtracks catastrophically on huge input.
    for match in _CITATION_SUFFIX.finditer(text):
        start = match.start()
        while start and (text[start - 1].isalnum() or text[start - 1] in "._/-"):
            start -= 1
        if start < match.start():
            return True
    # governor.mjs treats the two clauses independently: APPROVE may occur
    # anywhere, as may the explicit no-findings explanation.
    return bool(re.search(r"\bAPPROVE\b", text) and re.search(
        r"no (?:findings|issues|regressions|violations)|nothing to cite|clean pass",
        text,
        re.IGNORECASE,
    ))


def _participant_names(stage: dict[str, Any]) -> list[str]:
    """Normalize donor participant objects and Factory's string shorthand."""

    names: list[str] = []
    for participant in stage.get("participants", []):
        if isinstance(participant, str):
            names.append(participant)
        elif isinstance(participant, dict):
            value = participant.get("id") or participant.get("agentId") or participant.get("seat")
            if value:
                names.append(str(value))
    return names


def _default_policy() -> dict[str, Any]:
    """Build the ratified review -> approval -> land pipeline from seats."""

    cfg = _module("shipfactory.config").load_seats()
    seats = getattr(cfg, "seats", {}) or {}
    gates = getattr(cfg, "hierarchy_gates", {}) or {}

    def choose(preferred: str, role: str, fallback: str) -> str:
        if preferred in seats:
            return preferred
        for name, seat in seats.items():
            if getattr(seat, "role", None) == role:
                return name
        return fallback

    verdicts = list(gates.get("verdicts", []))
    landers = list(gates.get("landers", []))
    verifier = choose("verifier", "qa", verdicts[0] if verdicts else "verifier")
    architect = choose("architect", "architect", "architect")
    release = landers[0] if landers else choose("release", "release", "release")
    return {
        "mode": "normal",
        "commentRequired": True,
        "stages": [
            {"id": "review", "type": "review", "approvalsNeeded": 1, "participants": [verifier]},
            {"id": "approval", "type": "approval", "approvalsNeeded": 1, "participants": [architect]},
            {"id": "land", "type": "approval", "approvalsNeeded": 1, "participants": [release]},
        ],
    }


def _get_policy(task_id: str) -> dict[str, Any] | None:
    """Read a task policy through the frozen store contract."""

    return _module("shipfactory.store").get_policy(task_id)


def _decisions(task_id: str) -> list[dict[str, Any]]:
    """Read task decisions through the frozen store contract."""

    return list(_module("shipfactory.store").decisions_for(task_id) or [])


def _stage_status(task_id: str, policy: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Return (all_passed, first_stage_needing_action)."""

    decisions = _decisions(task_id)
    for stage in policy.get("stages", []):
        needed = int(stage.get("approvalsNeeded", 1) or 1)
        approved = {
            str(decision.get("seat"))
            for decision in decisions
            if decision.get("stage_id", decision.get("stageId")) == stage.get("id")
            and decision.get("outcome") in {"approved", "approve"}
        }
        if len(approved) < needed:
            return False, stage
    return True, None


def policy_satisfied(task_id: str) -> bool:
    """Return whether every configured stage has enough approvals."""

    policy = _get_policy(task_id)
    return True if not policy else _stage_status(task_id, policy)[0]


def _run_kanban(board: str, args: list[str]) -> None:
    """Run a mutating kanban command without importing kanban_db."""

    subprocess.run(["hermes", "kanban", "--board", board, *args], check=True, capture_output=True, text=True)


def _reopen(task_id: str, board: str, seat: str, summary: str) -> None:
    """Return a task to ready and route it to a stage participant."""

    # #16-V1: Hermes kanban takes comment text and assignee positionally.
    _run_kanban(board, ["comment", task_id, summary or "Execution policy stage pending."])
    _run_kanban(board, ["unblock", task_id])
    _run_kanban(board, ["assign", task_id, seat])
    # #16-V1: current Hermes exposes no completed -> ready transition;
    # unblock intentionally refuses completed tasks.  The completion hook runs
    # after that transition, so reopen the row atomically and retain an event.
    # The branch below preserves the lightweight module-contract test setup,
    # whose intentionally fake store has no real board to update.
    if not hasattr(_module("shipfactory.store"), "_db_path"):
        return
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        with kanban_db.write_txn(conn):
            updated = conn.execute(
                "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=? AND status='done'",
                (task_id,),
            )
            if updated.rowcount:
                kanban_db._append_event(  # type: ignore[attr-defined]
                    conn, task_id, "shipfactory_policy_reopened", {"seat": seat},
                )
    finally:
        conn.close()


def on_complete(task_id: str, board: str, assignee: str, summary: str) -> dict[str, Any]:
    """Apply the policy after worker completion and reopen when stages remain."""

    store = _module("shipfactory.store")
    # Recipes are the exclusive flow authority.  This lookup is intentionally
    # best-effort so the frozen lightweight policy-module tests remain usable.
    if hasattr(store, "_connect"):
        try:
            with store._connect() as conn:
                if conn.execute("SELECT 1 FROM recipe_steps WHERE kanban_task_id=?", (task_id,)).fetchone():
                    return {"action": "recipe", "next_stage": None}
        except Exception:
            pass
    policy = store.get_policy(task_id)
    if policy is None:
        return {"action": "allow", "next_stage": None}
    passed, stage = _stage_status(task_id, policy)
    if passed:
        hierarchy = _module("shipfactory.hierarchy")
        config = _module("shipfactory.config")
        cfg = config.load_seats()
        may_land = getattr(hierarchy, "may_land", lambda _cfg, _seat: True)
        if policy.get("stages") and not may_land(cfg, assignee):
            stage = policy["stages"][-1]
            participants = _participant_names(stage)
            next_stage = participants[0] if participants else assignee
            store.record_decision(task_id, str(stage.get("id")), str(stage.get("type", "approval")), assignee, "submitted", summary or "")
            _reopen(task_id, board, next_stage, summary or "Only a lander may complete this task.")
            return {"action": "reopen", "next_stage": str(stage.get("id"))}
        return {"action": "allow", "next_stage": None}

    assert stage is not None
    participants = _participant_names(stage)
    next_stage = participants[0] if participants else assignee
    # A submission is useful audit information but is not an approval.  The
    # donor's actual verdict outcomes remain approved/changes_requested.
    store.record_decision(task_id, str(stage.get("id")), str(stage.get("type", "review")), assignee, "submitted", summary or "")
    _reopen(task_id, board, next_stage, summary or f"Policy stage {stage.get('id')} is pending.")
    return {"action": "reopen", "next_stage": str(stage.get("id"))}


def record_verdict(task_id: str, stage_id: str, outcome: str, body: str, seat: str) -> dict[str, Any]:
    """Record a stage verdict, enforcing citations, stage membership, and role gates."""

    if not citation_ok(body):
        raise ValueError("verdict body needs a file:line citation or a clean APPROVE exemption")
    if outcome not in {"approve", "request_changes", "approved", "changes_requested"}:
        raise ValueError("outcome must be approve or request_changes")

    config = _module("shipfactory.config")
    hierarchy = _module("shipfactory.hierarchy")
    cfg = config.load_seats()
    if not hierarchy.may_verdict(cfg, seat):
        raise PermissionError(f"seat {seat!r} is not allowed to post verdicts")

    store = _module("shipfactory.store")
    policy = store.get_policy(task_id)
    if not policy:
        raise ValueError(f"no execution policy for task {task_id}")
    stage = next((item for item in policy.get("stages", []) if item.get("id") == stage_id), None)
    if stage is None:
        raise ValueError(f"unknown policy stage {stage_id!r}")
    if _participant_names(stage) and seat not in _participant_names(stage):
        raise PermissionError(f"seat {seat!r} is not a participant in stage {stage_id!r}")

    normalized = "approved" if outcome in {"approve", "approved"} else "changes_requested"
    store.record_decision(task_id, stage_id, str(stage.get("type", "review")), seat, normalized, body)
    if normalized == "changes_requested":
        return {"action": "reopen", "next_stage": stage_id}

    passed, next_stage = _stage_status(task_id, policy)
    if passed:
        return {"action": "allow", "next_stage": None}
    assert next_stage is not None
    participants = _participant_names(next_stage)
    return {"action": "reopen", "next_stage": str(next_stage.get("id"))}


__all__ = ["citation_ok", "on_complete", "policy_satisfied", "record_verdict"]
