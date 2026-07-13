"""Factory dashboard API, mounted by Hermes below ``/api/plugins/factory``."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Dashboard APIs are imported directly from ``dashboard/plugin_api.py`` by
# Hermes, unlike the normal plugin entry point.  Make the repository root
# importable so ``factory`` resolves when this is an installed user plugin.
_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from factory import store
from factory.recipes import advancer


router = APIRouter()


class GateDecision(BaseModel):
    instance: str = Field(min_length=1)
    step: str = Field(min_length=1)
    reason: str = ""


def _latest_steps(db: Any, instance_id: str | None = None) -> list[dict[str, Any]]:
    scope = "WHERE instance_id=?" if instance_id else ""
    where = "WHERE s.instance_id=?" if instance_id else ""
    params: tuple[Any, ...] = (instance_id, instance_id) if instance_id else ()
    rows = db.execute(
        f"""
        SELECT s.* FROM recipe_steps AS s
        JOIN (
          SELECT instance_id, step_id, MAX(activation) AS activation
          FROM recipe_steps {scope} GROUP BY instance_id, step_id
        ) AS latest
          ON latest.instance_id=s.instance_id AND latest.step_id=s.step_id
         AND latest.activation=s.activation
        {where} ORDER BY s.step_id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _recipe_budgets_for(db: Any, instance: dict[str, Any]) -> dict[str, int | None]:
    row = db.execute(
        "SELECT normalized_yaml FROM recipe_versions WHERE id=? AND version=?",
        (instance["recipe_id"], instance["recipe_version"]),
    ).fetchone()
    budgets: dict[str, int | None] = {
        "max_activations": None,
        "max_step_activations": None,
        "max_tokens": None,
    }
    if row:
        try:
            raw = (yaml.safe_load(row["normalized_yaml"]) or {}).get("budgets", {})
            for key in budgets:
                value = raw.get(key)
                budgets[key] = int(value) if value is not None else None
        except (TypeError, ValueError, yaml.YAMLError):
            pass
    return budgets


def _recipe_step_order_for(db: Any, recipe_id: str, recipe_version: int) -> list[str]:
    row = db.execute(
        "SELECT normalized_yaml FROM recipe_versions WHERE id=? AND version=?",
        (recipe_id, recipe_version),
    ).fetchone()
    if not row:
        return []
    try:
        document = yaml.safe_load(row["normalized_yaml"]) or {}
        return [
            str(step["id"])
            for step in document.get("steps", [])
            if isinstance(step, dict) and step.get("id")
        ]
    except (TypeError, yaml.YAMLError):
        return []


def _budget_for(db: Any, instance: dict[str, Any]) -> dict[str, Any]:
    budget = _recipe_budgets_for(db, instance)["max_tokens"]
    charged = instance["tokens_charged"]
    return {"charged": charged, "budget": budget, "remaining": max(budget - charged, 0) if budget is not None else None}


def _instance_summary(db: Any, instance: dict[str, Any]) -> dict[str, Any]:
    latest = _latest_steps(db, instance["id"])
    states: dict[str, int] = defaultdict(int)
    for step in latest:
        states[step["state"]] += 1
    return {
        **instance,
        "recipe": f"{instance['recipe_id']}@{instance['recipe_version']}",
        "latest_steps": latest,
        "step_states": dict(states),
        "tokens": _budget_for(db, instance),
        "budgets": _recipe_budgets_for(db, instance),
    }


def _gate_or_400(instance_id: str, step_id: str) -> None:
    with store._connect() as db:
        instance = db.execute("SELECT 1 FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        step = db.execute(
            "SELECT primitive,state FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
            (instance_id, step_id),
        ).fetchone()
    if not instance or not step or step["primitive"] != "approval_gate" or step["state"] != "waiting":
        raise HTTPException(status_code=400, detail="approval gate is not waiting")


@router.get("/instances")
def list_instances() -> list[dict[str, Any]]:
    store.init_db()
    with store._connect() as db:
        rows = db.execute("SELECT * FROM recipe_instances ORDER BY updated_at DESC, id DESC").fetchall()
        return [_instance_summary(db, dict(row)) for row in rows]


@router.get("/instances/{instance_id}")
def get_instance(instance_id: str) -> dict[str, Any]:
    store.init_db()
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="unknown recipe instance")
        instance = _instance_summary(db, dict(row))
        steps = [dict(item) for item in db.execute("SELECT * FROM recipe_steps WHERE instance_id=? ORDER BY step_id,activation", (instance_id,)).fetchall()]
        recipe_order = _recipe_step_order_for(
            db, instance["recipe_id"], instance["recipe_version"]
        )
        positions = {step_id: index + 1 for index, step_id in enumerate(recipe_order)}
        fallback_position = len(positions) + 1
        steps.sort(
            key=lambda step: (
                positions.get(step["step_id"], fallback_position),
                step["step_id"],
                step["activation"],
            )
        )
        for step in steps:
            step["step_position"] = positions.get(step["step_id"])
        activations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        task_ids: list[str] = []
        for step in steps:
            activations[step["step_id"]].append(step)
            if step["kanban_task_id"]:
                task_ids.append(step["kanban_task_id"])
        decisions: list[dict[str, Any]] = []
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            decisions = [dict(item) for item in db.execute(f"SELECT * FROM decisions WHERE task_id IN ({placeholders}) ORDER BY at DESC,id DESC", task_ids).fetchall()]
        instance.update({
            "steps": steps,
            "activations": dict(activations),
            "blocked_reasons": [{"step": step["step_id"], "activation": step["activation"], "reason": step["blocked_reason"]} for step in steps if step["blocked_reason"]],
            "decisions": decisions,
        })
        return instance


@router.get("/waiting")
def waiting_gates() -> list[dict[str, Any]]:
    """Return every current approval gate backed by a kanban needs-input task."""
    store.init_db()
    with store._connect() as db:
        rows = db.execute(
            """
            SELECT s.*,i.board,i.recipe_id,i.recipe_version,i.status AS instance_status,
                   i.blocked_reason AS instance_blocked_reason,i.updated_at AS instance_updated_at
            FROM recipe_steps AS s JOIN recipe_instances AS i ON i.id=s.instance_id
            JOIN (SELECT instance_id,step_id,MAX(activation) AS activation FROM recipe_steps GROUP BY instance_id,step_id) AS latest
              ON latest.instance_id=s.instance_id AND latest.step_id=s.step_id AND latest.activation=s.activation
            WHERE s.primitive='approval_gate' AND s.state='waiting'
            ORDER BY i.updated_at DESC,s.step_id
            """
        ).fetchall()
        gates = [dict(row) for row in rows]
        positions: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for gate in gates:
            instance_id = gate["instance_id"]
            if instance_id not in positions:
                ordered_ids = _recipe_step_order_for(
                    db, gate["recipe_id"], gate["recipe_version"]
                )
                if not ordered_ids:
                    ordered_ids = [
                        step["step_id"] for step in _latest_steps(db, instance_id)
                    ]
                positions[instance_id] = {
                    step_id: index + 1 for index, step_id in enumerate(ordered_ids)
                }
                totals[instance_id] = len(ordered_ids)
            gate["step_position"] = positions[instance_id].get(gate["step_id"])
            gate["step_total"] = totals[instance_id]
        return gates


@router.get("/seats")
def seats() -> list[dict[str, Any]]:
    store.init_db()
    try:
        from factory.config import load_seats
        return [vars(seat) | {"paused": store.seat_paused(seat.name)} for seat in load_seats().seats.values()]
    except (FileNotFoundError, OSError, ValueError):
        return []


@router.get("/costs")
def costs(
    by: str = Query("seat", pattern="^(seat|executor|task|day|instance)$"),
    since_days: int = Query(1, ge=0, le=3650),
) -> list[dict[str, Any]]:
    if by in {"seat", "executor", "task"}:
        return store.costs_rollup(by, since_days)

    store.init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    with store._connect() as db:
        if by == "day":
            rows = db.execute(
                """SELECT utc_day AS day, COUNT(*) AS charges,
                          COALESCE(SUM(tokens), 0) AS tokens_total
                   FROM budget_charges WHERE created_at>=?
                   GROUP BY utc_day ORDER BY utc_day DESC""",
                (since,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT c.instance_id AS instance, i.board,
                          i.recipe_id || '@' || i.recipe_version AS recipe,
                          COUNT(*) AS charges,
                          COALESCE(SUM(c.tokens), 0) AS tokens_total
                   FROM budget_charges AS c
                   LEFT JOIN recipe_instances AS i ON i.id=c.instance_id
                   WHERE c.created_at>=?
                   GROUP BY c.instance_id, i.board, i.recipe_id, i.recipe_version
                   ORDER BY tokens_total DESC, c.instance_id""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]


@router.post("/approve")
def approve(decision: GateDecision) -> dict[str, str]:
    _gate_or_400(decision.instance, decision.step)
    return {"key": advancer.gate_decision(decision.instance, decision.step, "approve")}


@router.post("/reject")
def reject(decision: GateDecision) -> dict[str, str]:
    _gate_or_400(decision.instance, decision.step)
    return {"key": advancer.gate_decision(decision.instance, decision.step, "reject", decision.reason)}
