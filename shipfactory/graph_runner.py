"""Durable GraphRunner v1 entry points."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from . import store
from .graph_recipe import GraphRecipe


_RESULT_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_COMPLETION_KEYS = frozenset({
    "type", "run_id", "attempt_id", "expected_state", "result", "work",
})
_FAILURE_KEYS = frozenset({
    "type", "run_id", "attempt_id", "expected_state", "failure",
})
_WORK_REFERENCE_KEYS = frozenset({"request", "preceding_outputs"})
_INPUT_WORK_KEYS = frozenset({
    "activating_token_ids", "request", "preceding_outputs",
})
_LINEAGE_FRAME_KEYS = frozenset({"split_group_id", "branch_id"})
_COMPLETABLE_STATES = frozenset({"ready", "running", "waiting_human"})
_LIVE_ATTEMPT_STATES = frozenset({
    "pending", "ready", "running", "waiting_human",
})


class _DiscardEvent(RuntimeError):
    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome = outcome


def start_run(
    *,
    project_id: str,
    board: str,
    recipe: GraphRecipe,
    request_text: str,
    launch_key: str,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    """Create one frozen Run and its pending root route token atomically."""
    store.init_db()
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = store.create_recipe_run_v1(
            run_id=str(uuid.uuid4()),
            project_id=project_id,
            board=board,
            recipe_name=recipe.name,
            recipe_hash=recipe.hash,
            recipe_snapshot_json=recipe.canonical_json,
            request_text=request_text,
            workspace_path=workspace_path,
            launch_key=launch_key,
            conn=conn,
        )
        store.insert_route_token_v1(
            token_id=str(uuid.uuid4()),
            run_id=run["id"],
            source_attempt_id=None,
            arrow_index=None,
            destination_box_id=recipe.start,
            lineage=[],
            work_refs={"request": request_text, "preceding_outputs": []},
            conn=conn,
        )
        return run


def reconcile_run(conn: Any, run_id: str) -> dict[str, Any]:
    """Reconcile pending sequential tokens and exact split-generation joins."""
    owns_transaction = not conn.in_transaction
    savepoint: str | None = None
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        savepoint = f"graph_reconcile_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        result = _reconcile_run_in_transaction(conn, run_id)
        if savepoint is not None:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif owns_transaction:
            conn.commit()
        return result
    except Exception:
        if savepoint is not None:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.rollback()
        raise


def _reconcile_run_in_transaction(
    conn: Any,
    run_id: str,
) -> dict[str, Any]:
    run = store.get_recipe_run_v1(run_id, conn=conn)
    if run is None:
        raise store.GraphStoreIntegrityError(
            f"GraphRunner run does not exist: {run_id}"
        )
    if run["state"] != "running":
        return {"attempts_created": 0, "tokens_consumed": 0}

    recipe = store._graph_recipe_for_run(conn, run_id)
    tokens = [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND state='pending'
               ORDER BY created_at,id""",
            (run_id,),
        )
    ]
    attempts_created = 0
    tokens_consumed = 0
    ordinary_tokens: list[dict[str, Any]] = []
    join_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    governing_cache: dict[tuple[str, str], str] = {}
    branch_reach_cache: dict[tuple[str, str], set[str]] = {}
    for token in tokens:
        lineage = _token_lineage(conn, token)
        destination_box_id = token["destination_box_id"]
        if _is_forward_join(recipe, destination_box_id) and lineage:
            cache_key = (token["id"], destination_box_id)
            split_group_id = governing_cache.setdefault(
                cache_key,
                _governing_split_group_id(
                    conn,
                    run=run,
                    recipe=recipe,
                    destination_box_id=destination_box_id,
                    lineage=lineage,
                    allowed_states={"open"},
                    branch_reach_cache=branch_reach_cache,
                ),
            )
            join_groups.setdefault(
                (destination_box_id, split_group_id),
                [],
            ).append(token)
        else:
            ordinary_tokens.append(token)

    for token in ordinary_tokens:
        input_work = _input_work_for_token(conn, run, token)
        _insert_ready_attempt(
            conn,
            run_id=run_id,
            box_id=token["destination_box_id"],
            input_work=input_work,
        )
        consumed = store.consume_route_tokens_v1(
            run_id=run_id,
            destination_box_id=token["destination_box_id"],
            token_ids=[token["id"]],
            conn=conn,
        )
        if len(consumed) != 1:
            raise store.GraphStoreConflict(
                f"route token was not consumed exactly once: {token['id']}"
            )
        attempts_created += 1
        tokens_consumed += 1

    for (destination_box_id, split_group_id), arrivals in sorted(
        join_groups.items()
    ):
        if _join_must_wait(
            conn,
            run=run,
            recipe=recipe,
            destination_box_id=destination_box_id,
            split_group_id=split_group_id,
            arrivals=arrivals,
        ):
            continue
        ordered_arrivals = sorted(arrivals, key=lambda token: token["id"])
        input_work = _join_input_work(conn, run, ordered_arrivals)
        _insert_ready_attempt(
            conn,
            run_id=run_id,
            box_id=destination_box_id,
            input_work=input_work,
        )
        consumed = store.consume_route_tokens_v1(
            run_id=run_id,
            destination_box_id=destination_box_id,
            token_ids=[token["id"] for token in ordered_arrivals],
            conn=conn,
        )
        if len(consumed) != len(ordered_arrivals):
            raise store.GraphStoreConflict(
                "join route tokens were not consumed exactly once: "
                f"{split_group_id}"
            )
        _close_collapsed_join_groups(
            conn,
            run_id=run_id,
            split_group_id=split_group_id,
            arrivals=ordered_arrivals,
        )
        attempts_created += 1
        tokens_consumed += len(consumed)
    return {
        "attempts_created": attempts_created,
        "tokens_consumed": tokens_consumed,
    }


def _insert_ready_attempt(
    conn: Any,
    *,
    run_id: str,
    box_id: str,
    input_work: dict[str, Any],
) -> None:
    ordinal = int(conn.execute(
        """SELECT COALESCE(MAX(ordinal),0)+1
           FROM box_attempts_v1
           WHERE run_id=? AND box_id=?""",
        (run_id, box_id),
    ).fetchone()[0])
    store.insert_box_attempt_v1(
        attempt_id=str(uuid.uuid4()),
        run_id=run_id,
        box_id=box_id,
        ordinal=ordinal,
        state="ready",
        input_work=input_work,
        conn=conn,
    )


def enqueue_event(
    *, run_id: str, source: str, payload: Any, key: str,
) -> dict[str, Any]:
    """Durably enqueue one GraphRunner event."""
    return store.enqueue_run_event_v1(
        key=key,
        run_id=run_id,
        source=source,
        payload=payload,
    )


def apply_events(
    *, owner: str, limit: int = 100, run_id: str | None = None,
) -> dict[str, Any]:
    """Lease and atomically apply direct box terminal events."""
    if (
        not isinstance(owner, str)
        or not owner
        or owner != owner.strip()
        or len(owner.encode("utf-8")) > 128
    ):
        raise ValueError(
            "event lease owner must be a nonempty stripped string "
            "of at most 128 UTF-8 bytes"
        )
    summary = {
        "leased": 0,
        "applied": 0,
        "discarded": 0,
        "failed": 0,
    }
    store.init_db()
    lease_budget = max(1, min(int(limit), 1000))
    while summary["leased"] < lease_budget:
        with store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            leased = store.lease_run_events_v1(
                owner=owner,
                limit=1,
                run_id=run_id,
                conn=conn,
            )
        if not leased:
            break
        event = leased[0]
        summary["leased"] += 1
        try:
            with store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    outcome = _apply_event(conn, event)
                except _DiscardEvent as exc:
                    store.finish_run_event_v1(
                        event["key"],
                        owner=owner,
                        expected_attempt_count=event["attempt_count"],
                        state="discarded",
                        outcome=exc.outcome,
                        error=str(exc),
                        conn=conn,
                    )
                    summary["discarded"] += 1
                else:
                    store.finish_run_event_v1(
                        event["key"],
                        owner=owner,
                        expected_attempt_count=event["attempt_count"],
                        state="applied",
                        outcome=outcome,
                        conn=conn,
                    )
                    summary["applied"] += 1
        except Exception as exc:
            summary["failed"] += 1
            try:
                with store._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    store.finish_run_event_v1(
                        event["key"],
                        owner=owner,
                        expected_attempt_count=event["attempt_count"],
                        state="failed",
                        outcome="apply_failed",
                        error=str(exc),
                        conn=conn,
                    )
            except store.GraphStoreConflict:
                # The lease expired or was recovered after the transition
                # transaction rolled back. Leave the event recoverable; a
                # later lease attempt will return it to pending.
                pass
    return summary


def _apply_event(conn: Any, event: dict[str, Any]) -> str:
    try:
        payload = json.loads(event["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return _apply_box_completed(conn, event)
    if isinstance(payload, dict) and payload.get("type") == "box_failed":
        return _apply_box_failed(conn, event)
    return _apply_box_completed(conn, event)


def _split_group(
    conn: Any,
    *,
    run_id: str,
    split_group_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM split_groups_v1 WHERE id=?",
        (split_group_id,),
    ).fetchone()
    if row is None or row["run_id"] != run_id:
        raise store.GraphStoreIntegrityError(
            f"lineage split group does not belong to its Run: {split_group_id}"
        )
    return dict(row)


def _json_array(value: Any, *, context: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise store.GraphStoreIntegrityError(
            f"{context} is not valid JSON"
        ) from exc
    if not isinstance(parsed, list):
        raise store.GraphStoreIntegrityError(f"{context} is not a list")
    return parsed


def _token_lineage(conn: Any, token: dict[str, Any]) -> list[dict[str, str]]:
    lineage = _json_array(
        token["lineage_json"],
        context=f"route token lineage {token['id']}",
    )
    validated: list[dict[str, str]] = []
    for index, frame in enumerate(lineage):
        if (
            not isinstance(frame, dict)
            or frozenset(frame) != _LINEAGE_FRAME_KEYS
            or not all(
                isinstance(frame.get(key), str) and frame[key]
                for key in _LINEAGE_FRAME_KEYS
            )
        ):
            raise store.GraphStoreIntegrityError(
                f"route token lineage frame is invalid: {token['id']}"
            )
        group = _split_group(
            conn,
            run_id=token["run_id"],
            split_group_id=frame["split_group_id"],
        )
        parent = _json_array(
            group["parent_lineage_json"],
            context=f"split group parent lineage {group['id']}",
        )
        branches = _json_array(
            group["branch_ids_json"],
            context=f"split group branches {group['id']}",
        )
        if (
            parent != validated
            or len(branches) < 2
            or len(set(branches)) != len(branches)
            or not all(isinstance(branch_id, str) and branch_id for branch_id in branches)
            or frame["branch_id"] not in branches
            or group["state"] not in {"open", "closed", "cancelled"}
        ):
            raise store.GraphStoreIntegrityError(
                f"route token lineage group membership is invalid: {token['id']}"
            )
        validated.append({
            "split_group_id": frame["split_group_id"],
            "branch_id": frame["branch_id"],
        })
    return validated


def _box_can_reach(
    recipe: GraphRecipe,
    source_box_id: str,
    destination_box_id: str,
) -> bool:
    pending = [source_box_id]
    visited: set[str] = set()
    while pending:
        box_id = pending.pop()
        if box_id == destination_box_id:
            return True
        if box_id in visited:
            continue
        visited.add(box_id)
        for arrow in recipe.arrows:
            if (
                arrow["from"] == box_id
                and not recipe.is_rework_arrow(
                    str(arrow["from"]), str(arrow["result"]),
                )
            ):
                pending.extend(str(value) for value in arrow["to"])
    return False


def _is_forward_join(recipe: GraphRecipe, box_id: str) -> bool:
    return sum(
        1
        for arrow in recipe.arrows
        if box_id in arrow["to"]  # type: ignore[operator]
        and not recipe.is_rework_arrow(
            str(arrow["from"]), str(arrow["result"]),
        )
    ) > 1


def _lineage_has_branch(
    lineage: list[dict[str, str]],
    *,
    split_group_id: str,
    branch_id: str,
) -> bool:
    return any(
        frame["split_group_id"] == split_group_id
        and frame["branch_id"] == branch_id
        for frame in lineage
    )


def _branch_can_still_reach(
    conn: Any,
    *,
    run: dict[str, Any],
    recipe: GraphRecipe,
    split_group_id: str,
    branch_id: str,
    destination_box_id: str,
) -> bool:
    for row in conn.execute(
        """SELECT * FROM route_tokens_v1
           WHERE run_id=? AND state='pending'""",
        (run["id"],),
    ):
        token = dict(row)
        lineage = _token_lineage(conn, token)
        if (
            _lineage_has_branch(
                lineage,
                split_group_id=split_group_id,
                branch_id=branch_id,
            )
            and _box_can_reach(
                recipe,
                token["destination_box_id"],
                destination_box_id,
            )
        ):
            return True

    for row in conn.execute(
        f"""SELECT * FROM box_attempts_v1
            WHERE run_id=? AND state IN (
                {','.join('?' for _ in _LIVE_ATTEMPT_STATES)}
            )""",
        (run["id"], *sorted(_LIVE_ATTEMPT_STATES)),
    ):
        attempt = dict(row)
        lineage = _attempt_lineage(conn, attempt)
        if (
            _lineage_has_branch(
                lineage,
                split_group_id=split_group_id,
                branch_id=branch_id,
            )
            and _box_can_reach(
                recipe,
                attempt["box_id"],
                destination_box_id,
            )
        ):
            return True
    return False


def _branch_generation_can_reach(
    conn: Any,
    *,
    run_id: str,
    recipe: GraphRecipe,
    split_group_id: str,
    branch_id: str,
    destination_box_id: str,
) -> bool:
    for row in conn.execute(
        "SELECT * FROM route_tokens_v1 WHERE run_id=?",
        (run_id,),
    ):
        token = dict(row)
        if (
            _lineage_has_branch(
                _token_lineage(conn, token),
                split_group_id=split_group_id,
                branch_id=branch_id,
            )
            and _box_can_reach(
                recipe,
                token["destination_box_id"],
                destination_box_id,
            )
        ):
            return True
    return False


def _governing_split_group_id(
    conn: Any,
    *,
    run: dict[str, Any],
    recipe: GraphRecipe,
    destination_box_id: str,
    lineage: list[dict[str, str]],
    allowed_states: set[str],
    branch_reach_cache: dict[tuple[str, str], set[str]],
) -> str:
    eligible: list[str] = []
    for frame in lineage:
        split_group_id = frame["split_group_id"]
        group = _split_group(
            conn,
            run_id=run["id"],
            split_group_id=split_group_id,
        )
        if group["state"] not in allowed_states:
            continue
        eligible.append(split_group_id)
        cache_key = (split_group_id, destination_box_id)
        if cache_key not in branch_reach_cache:
            branch_ids = _json_array(
                group["branch_ids_json"],
                context=f"split group branches {split_group_id}",
            )
            branch_reach_cache[cache_key] = {
                branch_id
                for branch_id in branch_ids
                if _branch_generation_can_reach(
                    conn,
                    run_id=run["id"],
                    recipe=recipe,
                    split_group_id=split_group_id,
                    branch_id=branch_id,
                    destination_box_id=destination_box_id,
                )
            }
        if len(branch_reach_cache[cache_key]) > 1:
            return split_group_id

    if eligible:
        return eligible[-1]
    raise store.GraphStoreIntegrityError(
        "join arrival has no split group in an allowed state: "
        f"{destination_box_id}"
    )


def _join_must_wait(
    conn: Any,
    *,
    run: dict[str, Any],
    recipe: GraphRecipe,
    destination_box_id: str,
    split_group_id: str,
    arrivals: list[dict[str, Any]],
) -> bool:
    branch_reach_cache: dict[tuple[str, str], set[str]] = {}
    arrived_by_group: dict[str, set[str]] = {}
    seen_lineages: set[tuple[tuple[str, str], ...]] = set()
    for token in arrivals:
        lineage = _token_lineage(conn, token)
        governing_id = _governing_split_group_id(
            conn,
            run=run,
            recipe=recipe,
            destination_box_id=destination_box_id,
            lineage=lineage,
            allowed_states={"open"},
            branch_reach_cache=branch_reach_cache,
        )
        if governing_id != split_group_id:
            raise store.GraphStoreIntegrityError(
                f"join arrival lineage is invalid: {token['id']}"
            )
        lineage_key = tuple(
            (frame["split_group_id"], frame["branch_id"])
            for frame in lineage
        )
        if lineage_key in seen_lineages:
            raise store.GraphStoreIntegrityError(
                f"join arrival lineage is duplicated: {token['id']}"
            )
        seen_lineages.add(lineage_key)
        governing_index = next(
            index
            for index, frame in enumerate(lineage)
            if frame["split_group_id"] == split_group_id
        )
        for frame in lineage[governing_index:]:
            group = _split_group(
                conn,
                run_id=run["id"],
                split_group_id=frame["split_group_id"],
            )
            if group["state"] != "open":
                raise store.GraphStoreIntegrityError(
                    "pending join token belongs to non-open split group: "
                    f"{frame['split_group_id']}"
                )
            arrived_by_group.setdefault(
                frame["split_group_id"], set(),
            ).add(frame["branch_id"])

    for group_id, arrived_branches in arrived_by_group.items():
        group = _split_group(
            conn,
            run_id=run["id"],
            split_group_id=group_id,
        )
        branch_ids = _json_array(
            group["branch_ids_json"],
            context=f"split group branches {group_id}",
        )
        for branch_id in branch_ids:
            if (
                branch_id not in arrived_branches
                and _branch_can_still_reach(
                    conn,
                    run=run,
                    recipe=recipe,
                    split_group_id=group_id,
                    branch_id=branch_id,
                    destination_box_id=destination_box_id,
                )
            ):
                return True
    return False


def _close_collapsed_join_groups(
    conn: Any,
    *,
    run_id: str,
    split_group_id: str,
    arrivals: list[dict[str, Any]],
) -> None:
    collapsed: list[str] = []
    seen: set[str] = set()
    for token in arrivals:
        lineage = _token_lineage(conn, token)
        try:
            governing_index = next(
                index
                for index, frame in enumerate(lineage)
                if frame["split_group_id"] == split_group_id
            )
        except StopIteration as exc:
            raise store.GraphStoreIntegrityError(
                f"join arrival omits its governing split group: {token['id']}"
            ) from exc
        for frame in reversed(lineage[governing_index:]):
            group_id = frame["split_group_id"]
            if group_id not in seen:
                seen.add(group_id)
                collapsed.append(group_id)

    if split_group_id not in seen:
        raise store.GraphStoreIntegrityError(
            f"join did not collapse its governing split group: {split_group_id}"
        )
    now = store._now()
    for group_id in collapsed:
        updated = conn.execute(
            """UPDATE split_groups_v1
               SET state='closed',closed_at=?
               WHERE id=? AND run_id=? AND state='open'""",
            (now, group_id, run_id),
        ).rowcount
        if updated != 1:
            raise store.GraphStoreConflict(
                f"split group changed while closing join: {group_id}"
            )


def _join_input_work(
    conn: Any,
    run: dict[str, Any],
    arrivals: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_arrivals = sorted(arrivals, key=lambda token: token["id"])
    preceding_outputs: list[dict[str, str]] = []
    seen_outputs: set[tuple[str, str, str]] = set()
    for token in ordered_arrivals:
        token_input = _input_work_for_token(conn, run, token)
        for reference in token_input["preceding_outputs"]:
            identity = (
                reference["attempt_id"],
                reference["box_id"],
                reference["output_work"],
            )
            if identity not in seen_outputs:
                seen_outputs.add(identity)
                preceding_outputs.append(reference)
    return {
        "activating_token_ids": [
            token["id"] for token in ordered_arrivals
        ],
        "request": run["request_text"],
        "preceding_outputs": preceding_outputs,
    }


def _input_work_for_token(
    conn: Any,
    run: dict[str, Any],
    token: dict[str, Any],
) -> dict[str, Any]:
    try:
        work_refs = json.loads(token["work_refs_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise store.GraphStoreIntegrityError(
            f"route token work references are invalid: {token['id']}"
        ) from exc
    if not isinstance(work_refs, dict):
        raise store.GraphStoreIntegrityError(
            f"route token work references are not an object: {token['id']}"
        )
    if frozenset(work_refs) != _WORK_REFERENCE_KEYS:
        raise store.GraphStoreIntegrityError(
            "route token work references must have the exact required fields: "
            f"{token['id']}"
        )
    if work_refs.get("request") != run["request_text"]:
        raise store.GraphStoreIntegrityError(
            f"route token request does not match its Run: {token['id']}"
        )
    preceding_outputs = work_refs["preceding_outputs"]
    if not isinstance(preceding_outputs, list):
        raise store.GraphStoreIntegrityError(
            f"route token preceding outputs are not a list: {token['id']}"
        )

    is_root = token["source_attempt_id"] is None
    if is_root:
        if token["arrow_index"] is not None:
            raise store.GraphStoreIntegrityError(
                f"root route token has an arrow index: {token['id']}"
            )
        if preceding_outputs:
            raise store.GraphStoreIntegrityError(
                f"root route token must have empty history: {token['id']}"
            )
        validated: list[dict[str, str]] = []
    else:
        if token["arrow_index"] is None:
            raise store.GraphStoreIntegrityError(
                f"non-root route token has no arrow index: {token['id']}"
            )
        source = conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE id=? AND run_id=?""",
            (token["source_attempt_id"], run["id"]),
        ).fetchone()
        if (
            source is None
            or source["state"] != "completed"
            or not isinstance(source["output_work"], str)
        ):
            raise store.GraphStoreIntegrityError(
                f"route token source attempt provenance is invalid: {token['id']}"
            )
        recipe = store._graph_recipe_for_run(conn, run["id"])
        arrow_index = int(token["arrow_index"])
        if arrow_index < 0 or arrow_index >= len(recipe.arrows):
            raise store.GraphStoreIntegrityError(
                f"route token arrow provenance is invalid: {token['id']}"
            )
        arrow = recipe.arrows[arrow_index]
        if (
            arrow["from"] != source["box_id"]
            or arrow["result"] != source["result"]
            or token["destination_box_id"] not in arrow["to"]
        ):
            raise store.GraphStoreIntegrityError(
                f"route token arrow provenance is invalid: {token['id']}"
            )
        source_input = _attempt_input_work(conn, run, dict(source))
        expected_outputs = [
            *source_input["preceding_outputs"],
            {
                "attempt_id": source["id"],
                "box_id": source["box_id"],
                "output_work": source["output_work"],
            },
        ]
        if preceding_outputs != expected_outputs:
            raise store.GraphStoreIntegrityError(
                "route token preceding outputs do not match source attempt "
                f"provenance: {token['id']}"
            )
        validated = _validated_output_references(
            conn,
            run_id=run["id"],
            references=preceding_outputs,
            context=f"route token {token['id']}",
        )

    return {
        "activating_token_ids": [token["id"]],
        "request": run["request_text"],
        "preceding_outputs": validated,
    }


def _attempt_input_work(
    conn: Any,
    run: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    try:
        input_work = json.loads(attempt["input_work_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise store.GraphStoreIntegrityError(
            f"box attempt input work is invalid: {attempt['id']}"
        ) from exc
    if (
        not isinstance(input_work, dict)
        or frozenset(input_work) != _INPUT_WORK_KEYS
        or not isinstance(input_work.get("activating_token_ids"), list)
        or not input_work["activating_token_ids"]
        or not all(
            isinstance(token_id, str) and token_id
            for token_id in input_work["activating_token_ids"]
        )
        or len(set(input_work["activating_token_ids"]))
        != len(input_work["activating_token_ids"])
        or input_work.get("request") != run["request_text"]
    ):
        raise store.GraphStoreIntegrityError(
            f"box attempt input work is invalid: {attempt['id']}"
        )
    preceding_outputs = _validated_output_references(
        conn,
        run_id=run["id"],
        references=input_work.get("preceding_outputs"),
        context=f"box attempt {attempt['id']}",
    )
    return {
        "activating_token_ids": list(input_work["activating_token_ids"]),
        "request": run["request_text"],
        "preceding_outputs": preceding_outputs,
    }


def _validated_output_references(
    conn: Any,
    *,
    run_id: str,
    references: Any,
    context: str,
) -> list[dict[str, str]]:
    if not isinstance(references, list):
        raise store.GraphStoreIntegrityError(
            f"{context} preceding outputs are not a list"
        )
    validated: list[dict[str, str]] = []
    for reference in references:
        if (
            not isinstance(reference, dict)
            or frozenset(reference) != {
                "attempt_id", "box_id", "output_work",
            }
            or not all(isinstance(value, str) for value in reference.values())
        ):
            raise store.GraphStoreIntegrityError(
                f"{context} output reference is invalid"
            )
        output = conn.execute(
            """SELECT box_id,output_work,state
               FROM box_attempts_v1
               WHERE id=? AND run_id=?""",
            (reference["attempt_id"], run_id),
        ).fetchone()
        if (
            output is None
            or output["state"] != "completed"
            or output["box_id"] != reference["box_id"]
            or output["output_work"] != reference["output_work"]
        ):
            raise store.GraphStoreIntegrityError(
                f"{context} output reference is not immutable"
            )
        validated.append(dict(reference))
    return validated


def _completion_payload(event: dict[str, Any]) -> dict[str, str]:
    try:
        payload = json.loads(event["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise _DiscardEvent("invalid_payload", "event payload is not valid JSON") from exc
    if not isinstance(payload, dict) or frozenset(payload) != _COMPLETION_KEYS:
        raise _DiscardEvent(
            "invalid_payload",
            "box_completed payload does not have the exact required fields",
        )
    if payload.get("type") != "box_completed":
        raise _DiscardEvent("unsupported_event", "event is not box_completed")
    for field in ("run_id", "attempt_id", "expected_state", "result", "work"):
        if not isinstance(payload.get(field), str):
            raise _DiscardEvent(
                "invalid_payload",
                f"box_completed {field} must be a string",
            )
    if payload["run_id"] != event["run_id"]:
        raise _DiscardEvent(
            "run_mismatch",
            "box_completed run does not match its durable event",
        )
    if payload["expected_state"] not in _COMPLETABLE_STATES:
        raise _DiscardEvent(
            "invalid_attempt_state",
            "box_completed expected_state is not completable",
        )
    if _RESULT_RE.fullmatch(payload["result"]) is None:
        raise _DiscardEvent(
            "invalid_result",
            "box_completed result is not a valid result label",
        )
    if not payload["work"].strip():
        raise _DiscardEvent(
            "invalid_work",
            "box_completed work must be non-empty",
        )
    return payload  # type: ignore[return-value]


def _failure_payload(event: dict[str, Any]) -> dict[str, str]:
    try:
        payload = json.loads(event["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise _DiscardEvent("invalid_payload", "event payload is not valid JSON") from exc
    if not isinstance(payload, dict) or frozenset(payload) != _FAILURE_KEYS:
        raise _DiscardEvent(
            "invalid_payload",
            "box_failed payload does not have the exact required fields",
        )
    if payload.get("type") != "box_failed":
        raise _DiscardEvent("unsupported_event", "event is not box_failed")
    for field in ("run_id", "attempt_id", "expected_state", "failure"):
        if not isinstance(payload.get(field), str):
            raise _DiscardEvent(
                "invalid_payload",
                f"box_failed {field} must be a string",
            )
    if payload["run_id"] != event["run_id"]:
        raise _DiscardEvent(
            "run_mismatch",
            "box_failed run does not match its durable event",
        )
    if payload["expected_state"] not in _COMPLETABLE_STATES:
        raise _DiscardEvent(
            "invalid_attempt_state",
            "box_failed expected_state is not fail-able",
        )
    if not payload["failure"].strip():
        raise _DiscardEvent(
            "invalid_failure",
            "box_failed failure must be non-empty",
        )
    return payload  # type: ignore[return-value]


def _assert_event_lease(conn: Any, event: dict[str, Any]) -> None:
    current_event = conn.execute(
        """SELECT state,lease_owner,attempt_count
           FROM run_events_v1 WHERE key=?""",
        (event["key"],),
    ).fetchone()
    if (
        current_event is None
        or current_event["state"] != "leased"
        or current_event["lease_owner"] != event["lease_owner"]
        or current_event["attempt_count"] != event["attempt_count"]
    ):
        raise store.GraphStoreConflict(
            f"run event lease changed before apply: {event['key']}"
        )


def _apply_box_failed(conn: Any, event: dict[str, Any]) -> str:
    _assert_event_lease(conn, event)
    payload = _failure_payload(event)
    run = store.get_recipe_run_v1(event["run_id"], conn=conn)
    if run is None:
        raise _DiscardEvent("missing_run", "box_failed Run does not exist")
    if run["state"] != "running":
        raise _DiscardEvent(
            "run_not_running",
            f"box_failed Run is {run['state']}",
        )
    attempt_row = conn.execute(
        "SELECT * FROM box_attempts_v1 WHERE id=?",
        (payload["attempt_id"],),
    ).fetchone()
    if attempt_row is None:
        raise _DiscardEvent("missing_attempt", "box_failed attempt does not exist")
    attempt = dict(attempt_row)
    if attempt["run_id"] != run["id"]:
        raise _DiscardEvent(
            "attempt_run_mismatch",
            "box_failed attempt belongs to another Run",
        )
    if attempt["state"] != payload["expected_state"]:
        raise _DiscardEvent(
            "stale_attempt_state",
            "box_failed expected state no longer matches the attempt",
        )

    store.update_box_attempt_v1(
        attempt["id"],
        expected_state=payload["expected_state"],
        state="failed",
        technical_failure=payload["failure"],
        conn=conn,
    )
    latest_completed_ordinal = int(conn.execute(
        """SELECT COALESCE(MAX(ordinal),0)
           FROM box_attempts_v1
           WHERE run_id=? AND box_id=? AND state='completed'
             AND input_work_json=?""",
        (run["id"], attempt["box_id"], attempt["input_work_json"]),
    ).fetchone()[0])
    failure_streak = [
        dict(row)
        for row in conn.execute(
            """SELECT id,ordinal,technical_failure
               FROM box_attempts_v1
               WHERE run_id=? AND box_id=? AND state='failed'
                 AND input_work_json=? AND ordinal>?
               ORDER BY ordinal""",
            (
                run["id"],
                attempt["box_id"],
                attempt["input_work_json"],
                latest_completed_ordinal,
            ),
        )
    ]
    if len(failure_streak) >= 3:
        blocked_reason = json.dumps(
            {
                "attempt_ids": [item["id"] for item in failure_streak],
                "box_id": attempt["box_id"],
                "failures": [
                    item["technical_failure"] for item in failure_streak
                ],
                "type": "technical_failure_limit",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = store._now()
        updated = conn.execute(
            """UPDATE recipe_runs_v1
               SET state='escalated',blocked_reason=?,updated_at=?,completed_at=NULL
               WHERE id=? AND state='running'""",
            (blocked_reason, now, run["id"]),
        ).rowcount
        if updated != 1:
            raise store.GraphStoreConflict(
                f"Run changed while escalating box failures: {run['id']}"
            )
        return "technical_failure_limit"

    next_ordinal = int(conn.execute(
        """SELECT COALESCE(MAX(ordinal),0)+1
           FROM box_attempts_v1 WHERE run_id=? AND box_id=?""",
        (run["id"], attempt["box_id"]),
    ).fetchone()[0])
    now = store._now()
    conn.execute(
        """INSERT INTO box_attempts_v1(
               id,run_id,box_id,ordinal,state,input_work_json,created_at,updated_at
           ) VALUES(?,?,?,?, 'ready',?,?,?)""",
        (
            str(uuid.uuid4()),
            run["id"],
            attempt["box_id"],
            next_ordinal,
            attempt["input_work_json"],
            now,
            now,
        ),
    )
    return "box_retry_ready"


def _apply_box_completed(conn: Any, event: dict[str, Any]) -> str:
    _assert_event_lease(conn, event)
    payload = _completion_payload(event)
    run = store.get_recipe_run_v1(event["run_id"], conn=conn)
    if run is None:
        raise _DiscardEvent("missing_run", "box_completed Run does not exist")
    if run["state"] != "running":
        raise _DiscardEvent(
            "run_not_running",
            f"box_completed Run is {run['state']}",
        )
    attempt_row = conn.execute(
        "SELECT * FROM box_attempts_v1 WHERE id=?",
        (payload["attempt_id"],),
    ).fetchone()
    if attempt_row is None:
        raise _DiscardEvent("missing_attempt", "box_completed attempt does not exist")
    attempt = dict(attempt_row)
    if attempt["run_id"] != run["id"]:
        raise _DiscardEvent(
            "attempt_run_mismatch",
            "box_completed attempt belongs to another Run",
        )
    if attempt["state"] != payload["expected_state"]:
        raise _DiscardEvent(
            "stale_attempt_state",
            "box_completed expected state no longer matches the attempt",
        )

    recipe = store._graph_recipe_for_run(conn, run["id"])
    arrow_index: int | None = None
    arrow: dict[str, Any] | None = None
    for index, candidate in enumerate(recipe.arrows):
        if (
            candidate["from"] == attempt["box_id"]
            and candidate["result"] == payload["result"]
        ):
            arrow_index = index
            arrow = candidate
            break

    if recipe.is_end(attempt["box_id"]) or arrow is not None:
        store.update_box_attempt_v1(
            attempt["id"],
            expected_state=payload["expected_state"],
            state="completed",
            output_work=payload["work"],
            result=payload["result"],
            conn=conn,
        )
    else:
        _complete_unroutable_attempt(conn, attempt, payload)

    if recipe.is_end(attempt["box_id"]):
        now = store._now()
        updated = conn.execute(
            """UPDATE recipe_runs_v1
               SET state='completed',blocked_reason=NULL,updated_at=?,completed_at=?
               WHERE id=? AND state='running'""",
            (now, now, run["id"]),
        ).rowcount
        if updated != 1:
            raise store.GraphStoreConflict(
                f"Run changed while completing End attempt: {run['id']}"
            )
        return "run_completed"

    if arrow is None:
        now = store._now()
        updated = conn.execute(
            """UPDATE recipe_runs_v1
               SET state='paused',blocked_reason='unroutable_result',updated_at=?
               WHERE id=? AND state='running'""",
            (now, run["id"]),
        ).rowcount
        if updated != 1:
            raise store.GraphStoreConflict(
                f"Run changed while pausing unroutable result: {run['id']}"
            )
        return "unroutable_result"

    assert arrow_index is not None
    if recipe.is_rework_arrow(attempt["box_id"], payload["result"]):
        consumed_traversals = int(conn.execute(
            """SELECT COUNT(DISTINCT source_attempt_id)
               FROM route_tokens_v1
               WHERE run_id=? AND arrow_index=? AND state='consumed'""",
            (run["id"], arrow_index),
        ).fetchone()[0])
        if consumed_traversals >= 2:
            return _escalate_rework_limit(
                conn,
                run=run,
                attempt=attempt,
                arrow_index=arrow_index,
                result=payload["result"],
            )

    lineage = _attempt_lineage(conn, attempt)
    input_work = _attempt_input_work(conn, run, attempt)
    preceding_outputs = list(input_work["preceding_outputs"])
    preceding_outputs.append({
        "attempt_id": attempt["id"],
        "box_id": attempt["box_id"],
        "output_work": payload["work"],
    })
    work_refs = {
        "request": run["request_text"],
        "preceding_outputs": preceding_outputs,
    }
    destinations = [str(destination) for destination in arrow["to"]]
    child_lineages = [lineage for _destination in destinations]
    if len(destinations) > 1:
        split_group_id = str(uuid.uuid4())
        branch_ids = [str(uuid.uuid4()) for _destination in destinations]
        now = store._now()
        conn.execute(
            """INSERT INTO split_groups_v1(
                   id,run_id,parent_lineage_json,branch_ids_json,state,created_at
               ) VALUES(?,?,?,?, 'open',?)""",
            (
                split_group_id,
                run["id"],
                json.dumps(
                    lineage,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    branch_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
            ),
        )
        child_lineages = [
            [
                *lineage,
                {
                    "split_group_id": split_group_id,
                    "branch_id": branch_id,
                },
            ]
            for branch_id in branch_ids
        ]

    for destination, child_lineage in zip(destinations, child_lineages):
        store.insert_route_token_v1(
            token_id=str(uuid.uuid4()),
            run_id=run["id"],
            source_attempt_id=attempt["id"],
            arrow_index=arrow_index,
            destination_box_id=destination,
            lineage=child_lineage,
            work_refs=work_refs,
            conn=conn,
        )
    return "routed"


def _escalate_rework_limit(
    conn: Any,
    *,
    run: dict[str, Any],
    attempt: dict[str, Any],
    arrow_index: int,
    result: str,
) -> str:
    previous_source = conn.execute(
        """SELECT DISTINCT token.source_attempt_id,source.ordinal
           FROM route_tokens_v1 AS token
           JOIN box_attempts_v1 AS source
             ON source.id=token.source_attempt_id
            AND source.run_id=token.run_id
           WHERE token.run_id=? AND token.arrow_index=?
             AND token.state='consumed' AND source.box_id=?
           ORDER BY source.ordinal DESC
           LIMIT 1""",
        (run["id"], arrow_index, attempt["box_id"]),
    ).fetchone()
    if previous_source is None:
        raise store.GraphStoreIntegrityError(
            f"rework traversal history disappeared: {run['id']}:{arrow_index}"
        )
    input_work = _attempt_input_work(conn, run, attempt)
    previous_positions = [
        index
        for index, reference in enumerate(input_work["preceding_outputs"])
        if reference["attempt_id"] == previous_source["source_attempt_id"]
    ]
    if not previous_positions:
        raise store.GraphStoreIntegrityError(
            f"rework traversal history is not causal: {run['id']}:{arrow_index}"
        )
    history = []
    for reference in input_work["preceding_outputs"][
        previous_positions[-1] + 1:
    ]:
        referenced_attempt = conn.execute(
            """SELECT result FROM box_attempts_v1
               WHERE id=? AND run_id=? AND state='completed'""",
            (reference["attempt_id"], run["id"]),
        ).fetchone()
        if referenced_attempt is None:
            raise store.GraphStoreIntegrityError(
                f"rework traversal result disappeared: {reference['attempt_id']}"
            )
        history.append({
            "output_work": reference["output_work"],
            "result": referenced_attempt["result"],
        })
    current_attempt = conn.execute(
        """SELECT output_work,result FROM box_attempts_v1
           WHERE id=? AND run_id=? AND state='completed'""",
        (attempt["id"], run["id"]),
    ).fetchone()
    if current_attempt is None:
        raise store.GraphStoreIntegrityError(
            f"rework traversal completion disappeared: {attempt['id']}"
        )
    history.append({
        "output_work": current_attempt["output_work"],
        "result": current_attempt["result"],
    })
    blocked_reason = json.dumps(
        {
            "arrow_index": arrow_index,
            "history": history,
            "result": result,
            "source_box_id": attempt["box_id"],
            "type": "rework_limit",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    now = store._now()
    updated = conn.execute(
        """UPDATE recipe_runs_v1
           SET state='escalated',blocked_reason=?,updated_at=?,completed_at=NULL
           WHERE id=? AND state='running'""",
        (blocked_reason, now, run["id"]),
    ).rowcount
    if updated != 1:
        raise store.GraphStoreConflict(
            f"Run changed while escalating rework: {run['id']}"
        )
    return "rework_limit"


def _complete_unroutable_attempt(
    conn: Any,
    attempt: dict[str, Any],
    payload: dict[str, str],
) -> None:
    now = store._now()
    updated = conn.execute(
        """UPDATE box_attempts_v1
           SET state='completed',output_work=?,result=?,technical_failure=NULL,
               updated_at=?,finished_at=?
           WHERE id=? AND state=?""",
        (
            payload["work"],
            payload["result"],
            now,
            now,
            attempt["id"],
            payload["expected_state"],
        ),
    ).rowcount
    if updated != 1:
        raise store.GraphStoreConflict(
            f"attempt changed while recording unroutable result: {attempt['id']}"
        )


def _attempt_lineage(
    conn: Any,
    attempt: dict[str, Any],
) -> list[dict[str, str]]:
    run = store.get_recipe_run_v1(attempt["run_id"], conn=conn)
    if run is None:
        raise store.GraphStoreIntegrityError(
            f"box attempt Run does not exist: {attempt['id']}"
        )
    expected_input = _attempt_input_work(conn, run, attempt)
    token_ids = expected_input["activating_token_ids"]
    rows = {
        row["id"]: dict(row)
        for row in conn.execute(
            f"""SELECT * FROM route_tokens_v1
                WHERE id IN ({','.join('?' for _ in token_ids)})""",
            token_ids,
        )
    }
    if len(rows) != len(token_ids):
        raise store.GraphStoreIntegrityError(
            f"box attempt activating route token does not exist: {attempt['id']}"
        )
    tokens = [rows[token_id] for token_id in token_ids]
    if any(
        token["run_id"] != attempt["run_id"]
        or token["destination_box_id"] != attempt["box_id"]
        or token["state"] != "consumed"
        for token in tokens
    ):
        raise store.GraphStoreIntegrityError(
            f"box attempt activating route token binding is invalid: {attempt['id']}"
        )

    lineages = [_token_lineage(conn, token) for token in tokens]
    if len(tokens) == 1:
        if _input_work_for_token(conn, run, tokens[0]) != expected_input:
            raise store.GraphStoreIntegrityError(
                "box attempt input disagrees with activating route token: "
                f"{attempt['id']}"
            )
        lineage = lineages[0]
        if lineage and _is_forward_join(
            store._graph_recipe_for_run(conn, attempt["run_id"]),
            attempt["box_id"],
        ):
            for index, frame in enumerate(lineage):
                group = _split_group(
                    conn,
                    run_id=attempt["run_id"],
                    split_group_id=frame["split_group_id"],
                )
                if group["state"] == "closed":
                    if any(
                        _split_group(
                            conn,
                            run_id=attempt["run_id"],
                            split_group_id=suffix["split_group_id"],
                        )["state"] != "closed"
                        for suffix in lineage[index:]
                    ):
                        raise store.GraphStoreIntegrityError(
                            "box attempt join lineage has an open collapsed "
                            f"split group: {attempt['id']}"
                        )
                    return lineage[:index]
        return lineage

    governing_index: int | None = None
    split_group_id: str | None = None
    for index in range(min(len(lineage) for lineage in lineages)):
        group_ids = {
            lineage[index]["split_group_id"] for lineage in lineages
        }
        if len(group_ids) != 1:
            break
        candidate_id = next(iter(group_ids))
        if _split_group(
            conn,
            run_id=attempt["run_id"],
            split_group_id=candidate_id,
        )["state"] == "closed":
            governing_index = index
            split_group_id = candidate_id
            break
    if governing_index is None or split_group_id is None:
        raise store.GraphStoreIntegrityError(
            f"box attempt join lineage is invalid: {attempt['id']}"
        )
    parent_lineage = lineages[0][:governing_index]
    if any(
        lineage[:governing_index] != parent_lineage
        or lineage[governing_index]["split_group_id"] != split_group_id
        or any(
            _split_group(
                conn,
                run_id=attempt["run_id"],
                split_group_id=frame["split_group_id"],
            )["state"] != "closed"
            for frame in lineage[governing_index:]
        )
        for lineage in lineages
    ):
        raise store.GraphStoreIntegrityError(
            f"box attempt join lineage is invalid: {attempt['id']}"
        )
    group = _split_group(
        conn,
        run_id=attempt["run_id"],
        split_group_id=split_group_id,
    )
    if group["state"] != "closed":
        raise store.GraphStoreIntegrityError(
            f"box attempt join split group is not closed: {attempt['id']}"
        )
    if _join_input_work(conn, run, tokens) != expected_input:
        raise store.GraphStoreIntegrityError(
            "box attempt input disagrees with activating route tokens: "
            f"{attempt['id']}"
        )
    return parent_lineage


__all__ = ["start_run", "reconcile_run", "enqueue_event", "apply_events"]
