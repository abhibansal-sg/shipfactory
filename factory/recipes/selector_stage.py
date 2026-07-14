"""Bounded, leased, idempotent triage-selector daemon stage."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from factory import store
from factory.config import load_seats, selector_config
from .instantiate import instantiate
from .loader import RecipeError, load_library
from .selector import (
    lease_source_task,
    park_no_recipe_match,
    run_selection,
    validate_or_park_selection,
)

logger = logging.getLogger(__name__)


def _empty() -> dict[str, int]:
    return {"leased": 0, "instantiated": 0, "parked": 0, "skipped": 0}


def _selection_row(selection_id: str) -> dict[str, Any]:
    with store._connect() as db:
        row = db.execute("SELECT * FROM triage_selections WHERE id=?", (selection_id,)).fetchone()
    return dict(row) if row else {}


def _cache_selection(selection_id: str, selection: dict) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE triage_selections SET ranked_json=?,outcome='selecting',updated_at=? WHERE id=?",
            (json.dumps(selection, sort_keys=True), store._now(), selection_id),
        )


def _record_outcome(selection_id: str, outcome: str, *, selection: dict | None = None,
                    root: str | None = None) -> None:
    nodes = selection.get("nodes", []) if isinstance(selection, dict) else []
    chosen = [node.get("chosen") for node in nodes if isinstance(node, dict)]
    parameters = [node.get("parameters") for node in nodes if isinstance(node, dict)]
    skips = [node.get("skip_steps") for node in nodes if isinstance(node, dict)]
    with store._connect() as db:
        db.execute(
            "UPDATE triage_selections SET chosen_recipe=?,parameters_json=?,skip_steps_json=?,"
            "outcome=?,root_collector_task_id=COALESCE(?,root_collector_task_id),"
            "lease_until=NULL,updated_at=? WHERE id=?",
            (
                json.dumps(chosen, sort_keys=True), json.dumps(parameters, sort_keys=True),
                json.dumps(skips, sort_keys=True), outcome, root, store._now(), selection_id,
            ),
        )


def _admit_selection(selection_id: str, board: str, allowance: int, ceiling: int) -> bool:
    """Atomically debit one non-refundable selector attempt against board-day."""
    day = datetime.now(timezone.utc).date().isoformat()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        daily = int(db.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM budget_charges WHERE board=? AND utc_day=?",
            (board, day),
        ).fetchone()[0])
        if daily + allowance > ceiling:
            return False
        activation = int(db.execute(
            "SELECT COUNT(*) FROM budget_charges WHERE instance_id=? AND step_id='selector'",
            (selection_id,),
        ).fetchone()[0]) + 1
        key = hashlib.sha256(f"{selection_id}|selector|{activation}|admit".encode()).hexdigest()
        db.execute(
            "INSERT INTO budget_charges(key,board,utc_day,instance_id,step_id,activation,tokens,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (key, board, day, selection_id, "selector", activation, allowance, store._now()),
        )
    return True


def _promote_for_parking(conn: Any, source_task_id: str) -> None:
    from hermes_cli import kanban_db
    task = kanban_db.get_task(conn, source_task_id)
    if task and task.status == "triage":
        if not kanban_db.specify_triage_task(conn, source_task_id, author="factory-selector"):
            raise RuntimeError("selector source moved before parking")
    kanban_db.assign_task(conn, source_task_id, None)


def _park(conn: Any, source_task_id: str, reason: str, details: list[dict] | None = None) -> None:
    from hermes_cli import kanban_db
    _promote_for_parking(conn, source_task_id)
    if reason == "no_recipe_match":
        park_no_recipe_match(conn, source_task_id, details or [])
        task = kanban_db.get_task(conn, source_task_id)
        if not task or task.status != "blocked":
            raise RuntimeError("selector source could not be parked")
    elif not kanban_db.block_task(conn, source_task_id, kind="needs_input", reason=reason):
        raise RuntimeError("selector source could not be parked")


def _mismatch_reasons(selection: object, error: Exception | None = None) -> list[dict]:
    reasons: list[dict] = []
    if isinstance(selection, dict):
        for node in selection.get("nodes", []):
            if isinstance(node, dict):
                reasons.extend(x for x in node.get("ranked_candidates", []) if isinstance(x, dict))
    if error is not None:
        reasons.append({"error": str(error)})
    return reasons


def _instance_id(selection_id: str, node_id: str) -> str:
    digest = hashlib.sha256(f"{selection_id}|{node_id}".encode()).hexdigest()[:24]
    return f"selection-{digest}"


def _existing_instance(instance_id: str) -> dict[str, Any] | None:
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
    return dict(row) if row else None


def _instantiate_nodes(conn: Any, *, board: str, selection_id: str, nodes: list[dict],
                       library, bare_recipe: str, seats: set[str]) -> int:
    from hermes_cli import kanban_db
    by_id = {node["id"]: node for node in nodes}
    collectors: dict[str, str] = {}
    created = 0

    def build(node_id: str) -> str:
        nonlocal created
        if node_id in collectors:
            return collectors[node_id]
        node = by_id[node_id]
        parent_collectors = [build(parent) for parent in node["needs"]]
        if node["chosen"] is None:
            supplied = node["parameters"]
            if set(supplied) != {"assignee_seat"} or supplied["assignee_seat"] not in seats:
                raise RecipeError("bare task requires a configured assignee_seat")
            recipe = library.get(bare_recipe)
            parameters = {
                "title": node["title"], "body": node["body"],
                "assignee_seat": supplied["assignee_seat"],
            }
            skips: list[str] = []
        else:
            recipe = library.get(node["chosen"])
            parameters = node["parameters"]
            skips = node["skip_steps"]
        instance_id = _instance_id(selection_id, node_id)
        existing = _existing_instance(instance_id)
        if existing:
            if (existing["recipe_id"], existing["recipe_version"]) != (
                recipe.document["id"], recipe.document["version"],
            ):
                raise RuntimeError("selection instance recipe changed across retry")
            collector = existing["collector_task_id"]
        else:
            result = instantiate(
                conn, board=board, recipe=recipe, parameters=parameters,
                skip_steps=skips, parent_tasks=parent_collectors, instance_id=instance_id,
            )
            collector = result["collector_task_id"]
            created += 1
        for parent in parent_collectors:
            kanban_db.link_tasks(conn, parent, collector)
        collectors[node_id] = collector
        return collector

    for node_id in by_id:
        build(node_id)
    return created


def _finish_root(conn: Any, source_task_id: str, collectors: list[str]) -> None:
    from hermes_cli import kanban_db
    task = kanban_db.get_task(conn, source_task_id)
    if task and task.status == "triage":
        if not kanban_db.specify_triage_task(conn, source_task_id, author="factory-selector"):
            raise RuntimeError("selector source moved before root conversion")
    kanban_db.assign_task(conn, source_task_id, None)
    task = kanban_db.get_task(conn, source_task_id)
    if task and task.status == "todo":
        kanban_db.recompute_ready(conn)
        task = kanban_db.get_task(conn, source_task_id)
    if task and task.status in {"ready", "running"}:
        if not kanban_db.block_task(
            conn, source_task_id, kind="needs_input", reason="recipe_root_collector",
        ):
            raise RuntimeError("selector root could not be parked")
    for collector in collectors:
        kanban_db.link_tasks(conn, collector, source_task_id)


def run_stage(conn: Any, board: str) -> dict[str, int]:
    """Run at most the configured number of leased selector operations."""
    from hermes_cli import kanban_db
    result = _empty()
    cfg = load_seats()
    recipes = cfg.recipes or {}
    settings = selector_config(recipes)
    if not recipes.get("enabled") or not settings["enabled"] or board != cfg.company:
        return result
    library = load_library(recipes["library_path"])
    seats = set(cfg.seats)
    profiles = set(recipes["execution_profiles"])
    allowance = int(settings["selection_allowance"])
    ceiling = int(recipes["board_day_token_ceiling"])
    tasks = kanban_db.list_tasks(
        conn, status="triage", limit=int(settings["max_per_tick"]), order_by="created",
    )
    for task in tasks:
        selection_id = lease_source_task(task.id, board)
        if selection_id is None:
            result["skipped"] += 1
            continue
        result["leased"] += 1
        try:
            row = _selection_row(selection_id)
            cached = None
            if row.get("ranked_json") not in (None, "[]"):
                cached = json.loads(row["ranked_json"])
            if cached is None:
                if not _admit_selection(selection_id, board, allowance, ceiling):
                    _park(conn, task.id, "budget_refused")
                    _record_outcome(selection_id, "budget_refused")
                    result["parked"] += 1
                    continue
                cached = run_selection(
                    task, library, seats=cfg.seats, max_tokens=allowance,
                )
                _cache_selection(selection_id, cached)
            try:
                nodes = validate_or_park_selection(
                    conn, task.id, cached, library, seats=seats, profiles=profiles,
                )
            except RecipeError as exc:
                _park(conn, task.id, "no_recipe_match", _mismatch_reasons(cached, exc))
                _record_outcome(selection_id, "no_recipe_match", selection=cached)
                result["parked"] += 1
                continue
            if not nodes:
                parked = kanban_db.get_task(conn, task.id)
                if parked and parked.status == "blocked":
                    kanban_db.assign_task(conn, task.id, None)
                    _record_outcome(selection_id, "needs_clarification", selection=cached)
                else:
                    _park(conn, task.id, "no_recipe_match", _mismatch_reasons(cached))
                    _record_outcome(selection_id, "no_recipe_match", selection=cached)
                result["parked"] += 1
                continue
            try:
                created = _instantiate_nodes(
                    conn, board=board, selection_id=selection_id, nodes=nodes,
                    library=library, bare_recipe=recipes["bare_task_recipe"], seats=seats,
                )
            except RecipeError as exc:
                _park(conn, task.id, "no_recipe_match", _mismatch_reasons(cached, exc))
                _record_outcome(selection_id, "no_recipe_match", selection=cached)
                result["parked"] += 1
                continue
            collectors = [
                _existing_instance(_instance_id(selection_id, node["id"]))["collector_task_id"]
                for node in nodes
            ]
            _record_outcome(selection_id, "selected", selection=cached, root=task.id)
            _finish_root(conn, task.id, collectors)
            result["instantiated"] += created
        except Exception:
            logger.exception("selector stage failed for source task %s", task.id)
            result["skipped"] += 1
    return result


__all__ = ["run_stage"]
