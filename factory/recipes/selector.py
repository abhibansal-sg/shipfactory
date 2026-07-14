"""VENDORED FROM: ~/Developer/products/hermes-mobile/hermes_cli/kanban_decompose.py

Fork SHA: e20f12f68084bf7da3358d828c3a01be509eb0a5
Recipe-engine deltas: ranked candidate output; chosen id@version and
parameters/skip_steps; strict fail-closed graph validation (never repair
references); durable Factory DB leases; ``no_recipe_match`` parking;
documented assumptions and bounded clarification parking.

Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


RECIPE_SELECTOR_PROMPT = """You are the Factory recipe selector.

Return one JSON object with an exact `nodes` array. Every node contains:
id, title, body, needs, ranked_candidates, chosen, parameters, skip_steps,
assumptions, and needs_clarification. Document low-impact informed defaults
in assumptions. Put at most three material unresolved questions in
needs_clarification, prioritized scope > security/privacy > UX > technical.

When no published recipe is needed, set chosen to null and provide exactly
one assignee_seat parameter naming a seat from the supplied roster. Factory
will bind title, body, and assignee_seat to its bare-task recipe.

Task sizing targets one seat-context-comfortable agent_task: about 0-3 files
touched. At 4-6 files, prefer splitting the node. At 7+ files, split it or
record a specific justification in assumptions. Never send unresolved
[NEEDS CLARIFICATION: <specific question>] markers into instantiated work.
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_orchestrator_profile(cfg: dict) -> str:
    """Resolve which profile owns the root/orchestration task after fan-out.

    Falls back to the active default profile when ``kanban.orchestrator_profile``
    is unset, so a task is never stranded for lack of an orchestrator.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    # Fall back to the active default profile.
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _resolve_default_assignee(cfg: dict) -> str:
    """Resolve which profile catches child tasks the orchestrator can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry is ``{name, description, has_description}``. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.
    """
    roster: list[dict] = []
    valid: set[str] = set()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, valid
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
        valid.add(p.name)
    return roster, valid


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        lines.append(f"  - {entry['name']}{tag}: {entry['description']}")
    return "\n".join(lines)


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: str,
    valid_names: set[str],
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    roster, valid_names = _build_roster()

    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    try:
        client, model = get_text_auxiliary_client("kanban_decomposer")
    except Exception as exc:
        logger.debug("decompose: get_text_auxiliary_client failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    if client is None or not model:
        return DecomposeOutcome(task_id, False, "no auxiliary client configured")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    fanout = bool(parsed.get("fanout"))
    audit_author = author or _profile_author()

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None
        assignee_val = None
        if not task.assignee:
            assignee_val = _normalize_assignee_choice(
                parsed.get("assignee"),
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        with kb.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=assignee_val,
                author=audit_author,
            )
        if not ok:
            return DecomposeOutcome(
                task_id, False, "task moved out of triage before promotion",
            )
        return DecomposeOutcome(
            task_id, True, "single task (no fanout)",
            fanout=False, new_title=title_val,
        )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(
            task_id, False, "decomposer returned fanout=true with empty tasks list",
        )

    # Rewrite invalid assignees to the default fallback. Never leave a
    # task with assignee=None — the user explicitly does not want that.
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}] is not an object",
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}].title is missing or empty",
            )
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        if (
            isinstance(assignee, str)
            and assignee.strip()
            and assignee.strip() not in valid_names
        ):
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        # Clean parent indices: drop non-int and out-of-range.
        clean_parents = [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx]
        children.append({
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": chosen,
            "parents": clean_parents,
        })

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    if child_ids is None:
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
        )

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]


# Recipe selector service ---------------------------------------------------
# The donor's public LLM transport is intentionally retained above.  Recipe
# routing has a distinct, stricter contract and does not use its repair paths.
from .loader import RecipeError


class SelectionNeedsClarification(RecipeError):
    """Validated nodes that must be parked before any instantiation."""

    def __init__(self, nodes: list[dict], markers: list[str]):
        super().__init__("selector output requires clarification parking")
        self.nodes = nodes
        self.markers = markers


def run_selection(task, library, *, seats: dict[str, object], max_tokens: int = 5_000,
                  timeout: int = 180) -> dict:
    """Call the configured auxiliary model with the recipe/seat manifest."""
    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
        client, model = get_text_auxiliary_client("factory_recipe_selector")
    except Exception as exc:
        raise RuntimeError("selector auxiliary client unavailable") from exc
    if client is None or not model:
        raise RuntimeError("selector auxiliary client unavailable")
    manifest = library.active_manifest()
    roster = [
        {"name": name, **{
            field: getattr(seat, field)
            for field in ("profile", "role", "reports_to")
            if getattr(seat, field, None) is not None
        }}
        for name, seat in sorted(seats.items())
    ]
    request = {
        "source_task": {"id": task.id, "title": task.title or "", "body": task.body or ""},
        "active_recipes": manifest,
        "seats": roster,
    }
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": RECIPE_SELECTOR_PROMPT},
            {"role": "user", "content": json.dumps(request, sort_keys=True)},
        ],
        temperature=0.2,
        max_tokens=int(max_tokens),
        timeout=timeout,
        extra_body=get_auxiliary_extra_body() or None,
    )
    try:
        raw = response.choices[0].message.content or ""
    except Exception:
        raw = ""
    parsed = _extract_json_blob(raw)
    if parsed is None:
        raise RecipeError("selector returned malformed JSON")
    return parsed


def validate_selection(selection: object, library, *, seats: set[str], profiles: set[str]) -> list[dict]:
    """Validate selector nodes exactly; invalid references are rejected, never dropped."""
    from .loader import bind_parameters, validate
    if not isinstance(selection, dict) or set(selection) != {"nodes"} or not isinstance(selection["nodes"], list):
        raise RecipeError("selector output must be {'nodes': [...]}")
    nodes = selection["nodes"]
    ids: set[str] = set()
    clarification_markers: list[str] = []
    def add_marker(marker: str) -> None:
        if marker.strip() not in clarification_markers:
            clarification_markers.append(marker.strip())
    for index, node in enumerate(nodes):
        required = {"id", "title", "body", "needs", "ranked_candidates", "chosen", "parameters", "skip_steps", "assumptions", "needs_clarification"}
        if not isinstance(node, dict) or set(node) != required or not isinstance(node["id"], str) or not node["id"] or node["id"] in ids or not isinstance(node["title"], str) or not isinstance(node["body"], str) or not isinstance(node["needs"], list) or not isinstance(node["ranked_candidates"], list) or not isinstance(node["parameters"], dict) or not isinstance(node["skip_steps"], list) or not isinstance(node["assumptions"], list) or not all(isinstance(item, str) and item.strip() for item in node["assumptions"]) or not isinstance(node["needs_clarification"], list) or not all(isinstance(item, str) and item.strip() for item in node["needs_clarification"]):
            raise RecipeError(f"invalid selector node {index}")
        for marker in node["needs_clarification"]:
            add_marker(marker)
        for marker in re.findall(r"\[NEEDS CLARIFICATION:\s*[^\]]+\]", node["body"], re.IGNORECASE):
            add_marker(marker)
        if len(clarification_markers) > 3:
            raise RecipeError("selector output exceeds 3 clarification markers")
        ids.add(node["id"])
        for candidate in node["ranked_candidates"]:
            if not isinstance(candidate, dict) or set(candidate) != {"id", "score", "reason"} or not isinstance(candidate["id"], str) or not isinstance(candidate["score"], (int, float)) or not isinstance(candidate["reason"], str): raise RecipeError("invalid ranked candidate")
        chosen = node["chosen"]
        if chosen is None:
            if (
                set(node["parameters"]) != {"assignee_seat"}
                or node["parameters"]["assignee_seat"] not in seats
            ):
                raise RecipeError("bare task requires a configured assignee_seat")
        else:
            if not isinstance(chosen, str): raise RecipeError("chosen recipe must be id@version or null")
            recipe = library.get(chosen)
            if recipe.document["status"] != "active": raise RecipeError("deprecated recipe choice")
            validate(recipe.document, seats=seats, profiles=profiles)
            bind_parameters(
                recipe, node["parameters"], node["skip_steps"],
                seats=seats, profiles=profiles,
            )
    for node in nodes:
        if any(not isinstance(ref, str) or ref not in ids or ref == node["id"] for ref in node["needs"]):
            raise RecipeError("invalid selector sibling dependency")
    # detect sibling graph cycle without normalizing it
    by_id = {node["id"]: node for node in nodes}; seen, active = set(), set()
    def walk(node_id):
        if node_id in active: raise RecipeError("selector dependency cycle")
        if node_id not in seen:
            active.add(node_id)
            for parent in by_id[node_id]["needs"]: walk(parent)
            active.remove(node_id); seen.add(node_id)
    for node_id in by_id: walk(node_id)
    if clarification_markers:
        raise SelectionNeedsClarification(nodes, clarification_markers)
    return nodes


def validate_or_park_selection(conn, source_task_id: str, selection: object, library,
                               *, seats: set[str], profiles: set[str]) -> list[dict]:
    """Validate output and park unresolved questions before instantiation."""
    try:
        return validate_selection(selection, library, seats=seats, profiles=profiles)
    except SelectionNeedsClarification as pending:
        markers = pending.markers
    task = kb.get_task(conn, source_task_id)
    if task is None:
        raise ValueError("unknown selector source task")
    if task.status == "triage":
        if not kb.specify_triage_task(conn, source_task_id, author="factory-selector"):
            raise RuntimeError("selector source moved before clarification park")
        task = kb.get_task(conn, source_task_id)
    reason = "needs_clarification: " + json.dumps(markers, ensure_ascii=False)
    if not task or task.status not in {"ready", "running"} or not kb.block_task(
        conn, source_task_id, kind="needs_input", reason=reason,
    ):
        raise RuntimeError("selector source could not be parked for clarification")
    return []


def lease_source_task(source_task_id: str, board: str, *, seconds: int = 120) -> str | None:
    """Acquire a Factory-db lease before any selector/model invocation."""
    from datetime import datetime, timedelta, timezone
    from factory import store
    import uuid
    now = datetime.now(timezone.utc); lease = (now + timedelta(seconds=seconds)).isoformat(); selection_id = str(uuid.uuid4())
    store.init_db()
    with store._connect() as db:
        row = db.execute("SELECT id,lease_until FROM triage_selections WHERE source_task_id=?", (source_task_id,)).fetchone()
        if row and row["lease_until"] and row["lease_until"] > now.isoformat(): return None
        db.execute("INSERT INTO triage_selections(id,source_task_id,board,lease_until,ranked_json,outcome,created_at,updated_at) VALUES(?,?,?,?,?,'leased',?,?) ON CONFLICT(source_task_id) DO UPDATE SET lease_until=excluded.lease_until,outcome='leased',updated_at=excluded.updated_at", (selection_id if not row else row["id"], source_task_id, board, lease, "[]", now.isoformat(), now.isoformat()))
        return selection_id if not row else row["id"]


def park_no_recipe_match(conn, source_task_id: str, reasons: list[dict]) -> None:
    """Park rather than guessing when no valid recipe was selected."""
    from hermes_cli import kanban_db
    kanban_db.block_task(conn, source_task_id, kind="needs_input", reason="no_recipe_match: " + json.dumps(reasons, sort_keys=True))
