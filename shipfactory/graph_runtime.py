"""GraphRunner v1 executor bridge built on Factory process supervision."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from shipfactory import spawn, store
from shipfactory.config import load_seats
from shipfactory.executors import get_executor
from shipfactory.graph_recipe import (
    GraphRecipe,
    GraphRecipeError,
    ensure_runtime_supported,
    validate,
)


class GraphRuntimeError(RuntimeError):
    """A ready graph attempt cannot be launched safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allowed_result_labels(recipe: GraphRecipe, box_id: str) -> tuple[str, ...]:
    labels = tuple(
        str(arrow["result"])
        for arrow in recipe.arrows
        if arrow["from"] == box_id
    )
    if labels:
        return labels
    if recipe.box(box_id).get("end") is True:
        return ("done",)
    raise GraphRuntimeError(f"box {box_id!r} has no routable result labels")


def _render_prompt(
    *, request: str, input_work: dict[str, Any], box: dict[str, object],
    allowed_labels: tuple[str, ...],
) -> str:
    preceding = input_work["preceding_outputs"]
    work = (
        "\n\n".join(str(item["output_work"]) for item in preceding)
        if preceding else "(none)"
    )
    if len(allowed_labels) == 1:
        label_contract = (
            f"The only allowed label for this box is `{allowed_labels[0]}`; "
            "use it exactly and do not invent a synonym. "
        )
    else:
        declared = ", ".join(f"`{label}`" for label in allowed_labels)
        label_contract = (
            f"The allowed labels for this box are {declared}; use exactly one "
            "of them and do not invent a synonym. "
        )
    return (
        "## REQUEST\n"
        f"{request}\n\n"
        "## PRECEDING WORK\n"
        f"{work}\n\n"
        "## INSTRUCTIONS\n"
        f"{box['instructions']}\n\n"
        "## COMPLETION CONTRACT\n"
        "Your final non-empty output line must be exactly "
        "`SHIPFACTORY_RESULT: <label>`, where `<label>` is a lowercase "
        "result label matching `^[a-z][a-z0-9-]*$`. "
        f"{label_contract}"
        "Put all produced work "
        "before that final line.\n"
    )


def _load_input(raw: str, request: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GraphRuntimeError("input_work_json is malformed") from exc
    if (
        not isinstance(value, dict)
        or frozenset(value) != {
            "request", "preceding_outputs", "activating_token_ids",
        }
        or value["request"] != request
        or not isinstance(value["preceding_outputs"], list)
        or not isinstance(value["activating_token_ids"], list)
        or not value["activating_token_ids"]
        or any(not isinstance(token, str) or not token for token in value["activating_token_ids"])
    ):
        raise GraphRuntimeError("input_work_json has invalid shape")
    for reference in value["preceding_outputs"]:
        if (
            not isinstance(reference, dict)
            or frozenset(reference) != {"attempt_id", "box_id", "output_work"}
            or any(not isinstance(reference[key], str) for key in reference)
        ):
            raise GraphRuntimeError("input_work_json has invalid preceding work")
    return value


def _load_recipe(row: dict[str, Any]) -> GraphRecipe:
    try:
        document = json.loads(row["recipe_snapshot_json"])
        recipe = validate(document)
    except Exception as exc:
        raise GraphRuntimeError("frozen recipe is malformed") from exc
    if recipe.hash != row["recipe_hash"] or recipe.name != row["recipe_name"]:
        raise GraphRuntimeError("frozen recipe identity mismatch")
    try:
        ensure_runtime_supported(recipe)
    except GraphRecipeError as exc:
        raise GraphRuntimeError(str(exc)) from exc
    return recipe


def _event_key(executor_run_id: int) -> str:
    material = f"graph-runtime:{int(executor_run_id)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def enqueue_terminal_event(
    *, executor_run_id: int, graph_run_id: str, attempt_id: str,
    event_type: str, result: str | None = None, work: str | None = None,
    failure: str | None = None,
) -> dict[str, Any]:
    if event_type == "box_completed":
        payload = {
            "type": "box_completed",
            "run_id": graph_run_id,
            "attempt_id": attempt_id,
            "expected_state": "running",
            "result": result,
            "work": work,
        }
    elif event_type == "box_failed":
        payload = {
            "type": "box_failed",
            "run_id": graph_run_id,
            "attempt_id": attempt_id,
            "expected_state": "running",
            "failure": failure,
        }
    else:
        raise ValueError(f"unknown graph terminal event type {event_type!r}")
    return store.enqueue_run_event_v1(
        key=_event_key(executor_run_id),
        run_id=graph_run_id,
        source="graph_runtime",
        payload=payload,
    )


def _bind_attempt(attempt_id: str, graph_run_id: str, executor_run_id: int) -> None:
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE box_attempts_v1
               SET state='running',executor_run_id=?,updated_at=?
               WHERE id=? AND run_id=? AND state='ready'
                 AND executor_run_id IS NULL
                 AND EXISTS(
                   SELECT 1 FROM recipe_runs_v1
                   WHERE id=? AND state='running'
                 )""",
            (executor_run_id, _now(), attempt_id, graph_run_id, graph_run_id),
        ).rowcount
        if changed != 1:
            raise GraphRuntimeError("ready graph attempt changed before binding")


def _wait_for_human(attempt_id: str, graph_run_id: str) -> None:
    """Atomically expose one ready human box to the protected decision API."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE box_attempts_v1
               SET state='waiting_human',updated_at=?
               WHERE id=? AND run_id=? AND state='ready'
                 AND executor_run_id IS NULL
                 AND EXISTS(
                   SELECT 1 FROM recipe_runs_v1
                   WHERE id=? AND state='running'
                 )""",
            (_now(), attempt_id, graph_run_id, graph_run_id),
        ).rowcount
        if changed != 1:
            raise GraphRuntimeError("ready human attempt changed before waiting")


def _bound_spawn_failure(
    graph_run_id: str, attempt_id: str, executor_run_id: int, exc: Exception,
) -> None:
    enqueue_terminal_event(
        executor_run_id=executor_run_id,
        graph_run_id=graph_run_id,
        attempt_id=attempt_id,
        event_type="box_failed",
        failure=f"spawn failed: {exc}",
    )


def _ready_rows(limit: int, *, board: str | None = None) -> list[dict[str, Any]]:
    store.init_db()
    with store._connect() as db:
        board_clause = " AND r.board=?" if board is not None else ""
        parameters: tuple[Any, ...] = (
            (board, max(0, int(limit)))
            if board is not None else (max(0, int(limit)),)
        )
        return [
            dict(row) for row in db.execute(
                """SELECT
                     a.id AS attempt_id,a.run_id,a.box_id,a.input_work_json,
                     r.board,r.recipe_name,r.recipe_hash,r.recipe_snapshot_json,
                     r.request_text,r.workspace_path
                   FROM box_attempts_v1 AS a
                   JOIN recipe_runs_v1 AS r ON r.id=a.run_id
                   WHERE a.state='ready' AND a.executor_run_id IS NULL
                     AND r.state='running'
                """ + board_clause + """
                   ORDER BY a.created_at,a.id
                   LIMIT ?""",
                parameters,
            )
        ]


def spawn_ready(max_workers: int, *, board: str | None = None) -> list[int]:
    """Launch up to ``max_workers`` ready graph attempts."""
    if int(max_workers) <= 0:
        return []
    rows = _ready_rows(max_workers, board=board)
    if not rows:
        return []
    cfg = None
    pids: list[int] = []
    for row in rows:
        try:
            recipe = _load_recipe(row)
            box = recipe.box(row["box_id"])
            who = str(box["who"])
            if who == "human":
                _wait_for_human(row["attempt_id"], row["run_id"])
                continue
            if cfg is None:
                cfg = load_seats()
            seat = cfg.seats.get(who)
            if seat is None or store.seat_paused(who):
                continue
            root = Path(str(row["workspace_path"] or ""))
            if not row["workspace_path"] or not root.is_dir():
                continue
            input_work = _load_input(
                row["input_work_json"], row["request_text"],
            )
            prompt = _render_prompt(
                request=row["request_text"], input_work=input_work, box=box,
                allowed_labels=_allowed_result_labels(recipe, row["box_id"]),
            )
            if seat.executor == "hermes":
                from hermes_cli import kanban_db
                cap = int(getattr(kanban_db, "_CTX_MAX_BODY_BYTES", 8192))
                if len(prompt.encode("utf-8")) > cap:
                    continue
            executor = get_executor(seat.executor)
            executor.identity_files(seat, str(root))
            logs = spawn._shipfactory_home() / "runs"
            logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            stem = f"graph-{row['attempt_id']}-{stamp}"
            log_path = logs / f"{stem}.log"
            prompt_path = logs / f"{stem}.prompt"
            prompt_path.write_text(prompt, encoding="utf-8")
            pid = spawn._spawn_target({
                "target_kind": "graph_box",
                "target_id": row["attempt_id"],
                "seat_name": who,
                "graph_run_id": row["run_id"],
                "board": row["board"],
                "workspace_path": root,
                "log_path": log_path,
                "prompt_path": prompt_path,
                "prompt": prompt,
                "max_workers": max_workers,
                "bind_run": lambda executor_run_id, attempt_id=row["attempt_id"],
                    graph_run_id=row["run_id"]: _bind_attempt(
                        attempt_id, graph_run_id, executor_run_id,
                    ),
                "on_bound_spawn_failure": (
                    lambda executor_run_id, exc, attempt_id=row["attempt_id"],
                    graph_run_id=row["run_id"]: _bound_spawn_failure(
                        graph_run_id, attempt_id, executor_run_id, exc,
                    )
                ),
            }, seat=seat, cfg=cfg)
        except spawn.WorkerCapacityExhausted:
            break
        except (GraphRuntimeError, OSError, ValueError):
            continue
        pids.append(pid)
    return pids


__all__ = ["GraphRuntimeError", "enqueue_terminal_event", "spawn_ready"]
