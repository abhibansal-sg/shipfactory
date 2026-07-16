"""Single-writer, idempotent recipe advancement and reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from shipfactory import store
from .instantiate import recipe_for_instance, revision_vector
from .instantiate import task_key
from .primitives import activate, parse_verdict

TERMINAL = {"done", "skipped", "cancelled", "failed"}
KANBAN_TERMINAL = {"done", "archived", "failed", "cancelled"}
EVENT_TERMINAL = {"applied", "discarded", "failed"}
ACTION_TERMINAL = {"succeeded", "terminal_failed", "abandoned"}
_LEASE_SECONDS = 30

_FINDING_COUNT = re.compile(r"(?im)^\s*(?:finding_count|findings)\s*[:=]\s*(\d+)\s*$")
_FINDING_LINE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:BLOCKER|WARNING)\b")

def advance_key(instance_id: str, recipe_hash: str, step_id: str, activation: int, transition: str, source_id: str) -> str:
    return hashlib.sha256("|".join(map(str, (instance_id, recipe_hash, step_id, activation, transition, source_id))).encode()).hexdigest()

def enqueue(instance_id: str, source: str, payload: dict[str, Any], *, key: str | None = None,
            expected_activation: int | None = None,
            expected_state: str | None = None) -> str:
    """Durably enqueue a hint.  It intentionally performs no flow mutation."""
    store.init_db(); key = key or hashlib.sha256((instance_id + "|" + source + "|" + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO advance_events"
            "(key,instance_id,source,payload_json,state,created_at,expected_activation,expected_state) "
            "VALUES(?,?,?,?, 'pending',?,?,?)",
            (key, instance_id, source, json.dumps(payload, sort_keys=True), store._now(),
             expected_activation, expected_state),
        )
    return key

def startup_guard(config: Any) -> None:
    """Fail closed before recipes run on an incompatible Hermes install/config."""
    if not (getattr(config, "recipes", {}) or {}).get("enabled"):
        return
    from hermes_cli import kanban_db
    if not callable(getattr(kanban_db, "create_blocked_task", None)) or not callable(getattr(kanban_db, "cancel_subtree", None)):
        raise RuntimeError("recipe engine requires kanban create_blocked_task and cancel_subtree APIs")
    try:
        from hermes_cli.config import load_config
        if bool((load_config() or {}).get("kanban", {}).get("auto_decompose", True)):
            raise RuntimeError("recipe engine refuses kanban.auto_decompose=true")
    except ImportError:
        pass

def _latest(db: Any, instance_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute("SELECT s.* FROM recipe_steps s JOIN (SELECT step_id,MAX(activation) activation FROM recipe_steps WHERE instance_id=? GROUP BY step_id) l ON l.step_id=s.step_id AND l.activation=s.activation WHERE s.instance_id=?", (instance_id, instance_id)).fetchall()]

def _instance(db: Any, instance_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone(); return dict(row) if row else None

def _transition(db: Any, instance: dict[str, Any], step: dict[str, Any], state: str, source: str, *, reason: str | None = None, task: str | None = None) -> bool:
    key = advance_key(instance["id"], instance["recipe_hash"], step["step_id"], step["activation"], state, source)
    existing = db.execute("SELECT state FROM advance_events WHERE key=?", (key,)).fetchone()
    if existing and existing["state"] in EVENT_TERMINAL:
        return False
    owner = f"transition:{os.getpid()}"
    db.execute(
        "INSERT OR IGNORE INTO advance_events"
        "(key,instance_id,source,payload_json,state,created_at,lease_owner,lease_until,"
        "attempt_count,expected_activation,expected_state) "
        "VALUES(?,?,?,?, 'leased',?,?,?,?,?,?)",
        (key, instance["id"], source, "{}", store._now(), owner,
         (datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)).isoformat(),
         1, step["activation"], step["state"]),
    )
    output_revision = None
    if state == "done" and step["primitive"] == "agent_task":
        output_revision = int(db.execute(
            "SELECT COALESCE(MAX(output_revision),0)+1 FROM recipe_steps WHERE instance_id=?",
            (instance["id"],),
        ).fetchone()[0])
    db.execute(
        "UPDATE recipe_steps SET state=?,blocked_reason=?,kanban_task_id=COALESCE(?,kanban_task_id),"
        "output_revision=COALESCE(output_revision,?),updated_at=? "
        "WHERE instance_id=? AND step_id=? AND activation=?",
        (
            state, reason, task, output_revision, store._now(), instance["id"],
            step["step_id"], step["activation"],
        ),
    )
    db.execute(
        "UPDATE advance_events SET state='applied',applied_at=?,outcome=?,"
        "lease_owner=NULL,lease_until=NULL WHERE key=?",
        (store._now(), f"step:{step['step_id']}:{step['activation']}:{state}", key),
    )
    return True


def _action_key(logical_key: str, attempt: int) -> str:
    """Return a fresh stable key for one external-action attempt."""
    return hashlib.sha256(f"{logical_key}|attempt|{attempt}".encode()).hexdigest()


def _plan_action(db: Any, *, logical_key: str, kind: str, payload: dict[str, Any],
                 instance_id: str | None = None, step_id: str | None = None,
                 activation: int | None = None) -> str:
    """Durably plan the first attempt for a logical external effect."""
    key = _action_key(logical_key, 1)
    db.execute(
        "INSERT OR IGNORE INTO action_intents"
        "(key,logical_key,attempt,instance_id,step_id,activation,kind,payload_json,state,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'planned',?)",
        (key, logical_key, 1, instance_id, step_id, activation, kind,
         json.dumps(payload, sort_keys=True), store._now()),
    )
    return key


def plan_worker_transition(*, run_id: int, task_id: str, board: str | None,
                           result: str, summary: str,
                           process_start_token: str | None,
                           task_attempt_id: int | None) -> str:
    """Journal one reaped worker's board transition as a recoverable effect."""
    store.init_db()
    logical_key = hashlib.sha256(
        f"run:{int(run_id)}|task:{task_id}|transition:{result}".encode()
    ).hexdigest()
    with store._connect() as db:
        return _plan_action(
            db, logical_key=logical_key, kind="worker_task_transition",
            payload={"run_id": int(run_id), "task_id": task_id, "board": board,
                     "result": result, "summary": summary,
                     "process_start_token": process_start_token,
                     "task_attempt_id": (
                         int(task_attempt_id) if task_attempt_id is not None else None
                     )},
        )

def _admit(db: Any, instance: dict[str, Any], recipe: dict[str, Any], step: dict[str, Any],
           profile: dict[str, Any], profile_name: str,
           board_day_token_ceiling: int) -> str | None:
    allowance = int(profile["token_allowance"]); budgets = recipe["budgets"]; day = datetime.now(timezone.utc).date().isoformat()
    v2 = recipe.get("schema") == "shipfactory.recipe/v2"
    exhausted = "budget_exhausted" if v2 else "activation_fuse"
    if instance["activation_count"] + 1 > budgets["max_activations"]:
        return f"{exhausted}:max_activations" if v2 else exhausted
    count = db.execute("SELECT COUNT(*) FROM recipe_steps WHERE instance_id=? AND step_id=? AND primitive IN ('agent_task','review_gate')", (instance["id"], step["step_id"])).fetchone()[0]
    step_cap = (
        budgets["step_activation_caps"][step["step_id"]]
        if v2 else budgets["max_step_activations"]
    )
    if count > step_cap:
        return (
            f"budget_exhausted:step_activation_cap:{step['step_id']}"
            if v2 else "activation_fuse"
        )
    if instance["tokens_charged"] + allowance > budgets["max_tokens"]:
        return "budget_exhausted:max_tokens" if v2 else "instance_budget"
    if v2:
        pool_limit = budgets["token_pools"].get(profile_name)
        if pool_limit is None:
            return f"budget_exhausted:unknown_token_pool:{profile_name}"
        pool_charged = int(db.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM budget_charges "
            "WHERE instance_id=? AND token_pool=?",
            (instance["id"], profile_name),
        ).fetchone()[0])
        if pool_charged + allowance > int(pool_limit):
            return f"budget_exhausted:token_pool:{profile_name}"
    charge_key = advance_key(instance["id"], instance["recipe_hash"], step["step_id"], step["activation"], "admit", str(step["activation"]))
    before = db.total_changes
    if not store.admit_budget_charge(
        db, key=charge_key, board=instance["board"], utc_day=day,
        instance_id=instance["id"], step_id=step["step_id"],
        activation=step["activation"], tokens=allowance,
        ceiling=board_day_token_ceiling,
        token_pool=profile_name if v2 else None,
    ):
        return "board_day_budget"
    if db.total_changes > before: db.execute("UPDATE recipe_instances SET activation_count=activation_count+1,tokens_charged=tokens_charged+?,updated_at=? WHERE id=?", (allowance, store._now(), instance["id"]))
    return None

def _summary(db: Any, instance: dict[str, Any]) -> str:
    states = [x["state"] for x in _latest(db, instance["id"])]
    if instance["status"] in {"cancelling", "cancelled", "failed", "done"}: return instance["status"]
    if any(x == "failed" for x in states): return "failed"
    if any(x == "blocked" for x in states): return "blocked"
    if any(x == "running" or x == "ready" for x in states): return "running"
    # notify waiting remains running; human controls are distinguished.
    rows = _latest(db, instance["id"])
    if any(x["state"] == "waiting" and x["primitive"] == "approval_gate" for x in rows): return "waiting_gate"
    if any(x["state"] == "waiting" and x["primitive"] == "wait_for_event" for x in rows): return "waiting_event"
    if all(x in {"done", "skipped"} for x in states): return "done"
    return "running"

def _verdict_finding_count(body: str) -> int:
    """Extract a deterministic finding total, or -1 when the body has none."""
    text = str(body or "").strip()
    try:
        structured = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        structured = None
    if isinstance(structured, dict):
        explicit = structured.get("finding_count")
        if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0:
            return explicit
        findings = structured.get("findings")
        if isinstance(findings, list):
            return len(findings)
    match = _FINDING_COUNT.search(text)
    if match:
        return int(match.group(1))
    fallback = len(_FINDING_LINE.findall(text))
    return fallback if fallback else -1

def _review_stalled(db: Any, instance_id: str, step: dict[str, Any], count: int) -> bool:
    previous = db.execute(
        "SELECT finding_count FROM recipe_steps WHERE instance_id=? AND step_id=? "
        "AND activation<? ORDER BY activation DESC LIMIT 1",
        (instance_id, step["step_id"], step["activation"]),
    ).fetchone()
    return bool(previous and previous["finding_count"] is not None and count >= 0
                and int(previous["finding_count"]) >= 0 and count >= int(previous["finding_count"]))


def _step_change_set_workspace(
    conn: Any, latest: dict[str, dict[str, Any]], definition: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Resolve a v2 step's declared change-set producer worktree and its owner task.

    Shared by verification scheduling and review-approval binding so both
    resolve "the candidate's own worktree" identically (finding #1,
    verification adversarial lane).
    """
    from hermes_cli import kanban_db
    workspace = None
    owner_task_id = None
    fallback_workspace = None
    fallback_owner = None
    for declared_input in definition.get("inputs", []):
        producer = latest.get(declared_input["from"])
        if producer and producer.get("kanban_task_id"):
            producer_task = kanban_db.get_task(conn, producer["kanban_task_id"])
            candidate_workspace = getattr(producer_task, "workspace_path", None)
            if candidate_workspace:
                fallback_workspace = fallback_workspace or candidate_workspace
                fallback_owner = fallback_owner or producer["kanban_task_id"]
                if declared_input["kind"] == "change-set":
                    workspace = candidate_workspace
                    owner_task_id = producer["kanban_task_id"]
                    break
    return workspace or fallback_workspace, owner_task_id or fallback_owner


def _review_approval_blocker(db: Any, instance_id: str,
                             definition: dict[str, Any], *,
                             verdict_body: str = "", recipe: dict[str, Any] | None = None,
                             conn: Any = None, latest: dict[str, dict[str, Any]] | None = None,
                             defs: dict[str, dict[str, Any]] | None = None) -> str | None:
    """Return a factory-enforced reason that forbids a review approval."""
    if definition.get("primitive") != "review_gate":
        return None
    from shipfactory.artifacts import input_artifacts, task_spec_has_clarifications
    for artifact in input_artifacts(db, instance_id, definition):
        if artifact["kind"] == "task-spec" and task_spec_has_clarifications(artifact):
            return "clarifications_nonempty"
    defs = defs or ({item["id"]: item for item in recipe["steps"]} if recipe else {})
    ancestors: set[str] = set()
    def collect(node: str) -> None:
        for parent in defs.get(node, {}).get("needs", []):
            if parent not in ancestors:
                ancestors.add(parent)
                collect(parent)
    collect(definition["id"])
    # Evidence-bound review (§2.4.8): an approval that follows a verification
    # step must be bound to the exact live sealed bundle, never model prose
    # alone (finding #3, verification adversarial lane -- #9 and #18).
    verification_producers = [
        needed for needed in ancestors
        if defs.get(needed, {}).get("primitive") == "verification"
    ]
    for producer_id in verification_producers:
        bundle_row = db.execute(
            "SELECT * FROM evidence_bundles WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1",
            (instance_id, producer_id),
        ).fetchone()
        if bundle_row is None:
            return "evidence_missing"
        bundle = dict(bundle_row)
        from shipfactory.verification import (
            CommitBindingError, EvidenceInvariantError, assert_commit_binding,
            verify_evidence_bundle,
        )
        try:
            verify_evidence_bundle(bundle["id"], db=db)
        except EvidenceInvariantError as exc:
            return f"evidence_invariant:{exc}"
        if bundle["state"] != "done":
            return "verification_not_passed"
        try:
            from shipfactory.verification import _assert_workspace_owner
            if bundle.get("workspace_path"):
                _assert_workspace_owner({
                    "workspace": bundle["workspace_path"],
                    "workspace_owner_task_id": bundle.get("workspace_owner_task_id"),
                    "workspace_owner_activation": bundle.get("workspace_owner_activation"),
                    "workspace_owner_run_id": bundle.get("workspace_owner_run_id"),
                })
                assert_commit_binding(bundle["workspace_path"], bundle["head_sha"], bundle["tree_sha"])
        except CommitBindingError as exc:
            return f"candidate_mutated_after_verification:{exc}"
    # Bind the task itself to the exact sealed inputs Factory opened.  A hash
    # copied into model prose is not evidence that those bytes were supplied.
    if (recipe is not None and recipe.get("schema") == "shipfactory.recipe/v2"
            and conn is not None and latest is not None):
        from hermes_cli import kanban_db
        from .primitives import build_review_input_context
        try:
            review_context, review_digest = build_review_input_context(
                db, _instance(db, instance_id), recipe, definition,
            )
        except Exception as exc:
            return f"review_inputs_invalid:{exc}"
        step_row = latest.get(definition["id"])
        task = (
            kanban_db.get_task(conn, step_row["kanban_task_id"])
            if step_row and step_row.get("kanban_task_id") else None
        )
        marker = f"SHIPFACTORY_REVIEW_INPUT_SHA256: {review_digest}"
        task_body = str(task.body or "") if task is not None else ""
        if marker not in task_body or review_context not in task_body:
            return "review_inputs_not_bound"
    # Reviewer/builder provider independence (finding #3): a differently
    # named reviewer seat configured identically to the builder seat is not
    # an independent review.
    change_set_producer_id = next((item["from"] for step_id in [definition["id"], *ancestors]
        for item in defs.get(step_id, {}).get("inputs", []) if item.get("kind") == "change-set"), None)
    if change_set_producer_id and defs.get(change_set_producer_id, {}).get("primitive") == "agent_task":
        builder_seat = defs[change_set_producer_id]["params"].get("seat")
        reviewer_seat = definition["params"].get("seat")
        if builder_seat and reviewer_seat:
            from shipfactory.config import load_seats, reviewer_shares_builder_provider
            try:
                cfg = load_seats()
                shared = reviewer_shares_builder_provider(cfg, builder_seat, reviewer_seat)
            except Exception as exc:
                return f"reviewer_provider_unresolved:{exc}"
            if shared:
                return "reviewer_shares_builder_provider"
    return None

def _result_one_liner(value: str | None) -> str | None:
    in_frontmatter = False
    saw_frontmatter = False
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "---":
            in_frontmatter = not in_frontmatter if not saw_frontmatter or in_frontmatter else False
            saw_frontmatter = True
            continue
        if in_frontmatter or line.startswith(("#", "```", "SHIPFACTORY_VERDICT:")):
            continue
        return line.strip("* ")[:240]
    return None


def _bind_text(value: str, parameters: dict[str, Any]) -> str:
    for name, item in parameters.items():
        value = value.replace("${" + name + "}", "" if item is None else str(item))
    return value

def _evidence_text(value: Any, limit: int = 1000) -> str | None:
    if value in (None, "", {}, []):
        return None
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return " ".join(value.split())[:limit] or None


def _commit_hash(*values: Any) -> str | None:
    def explicit(value: Any) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if "commit" in str(key).lower() and isinstance(item, str):
                    match = re.search(r"\b[0-9a-fA-F]{7,40}\b", item)
                    if match:
                        return match.group(0)
            for item in value.values():
                found = explicit(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = explicit(item)
                if found:
                    return found
        return None
    for value in values:
        found = explicit(value)
        if found:
            return found
    for value in values:
        text = _evidence_text(value) or ""
        match = re.search(r"(?i)\b(?:commit|revision|sha)\s*[:#@=]?\s*([0-9a-f]{7,40})\b", text)
        if match:
            return match.group(1)
    return None


def _test_counts(*values: Any) -> list[str]:
    found: list[str] = []
    labels = {"passed", "failed", "skipped", "xfailed", "xpassed", "error", "errors"}

    def add(item: str) -> None:
        if item not in found:
            found.append(item)

    def structured(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                label = str(key).lower().replace("tests_", "").replace("test_", "")
                if label in labels and isinstance(item, int) and not isinstance(item, bool):
                    add(f"{item} {label}")
                else:
                    structured(item)
        elif isinstance(value, list):
            for item in value:
                structured(item)

    for value in values:
        structured(value)
        text = _evidence_text(value, limit=5000) or ""
        for count, label in re.findall(
            r"(?i)\b(\d+)\s+(passed|failed|skipped|xfailed|xpassed|errors?)\b", text
        ):
            add(f"{count} {label.lower()}")
    return found


def _verdict_text(result: str | None, metadata: Any) -> str | None:
    if isinstance(metadata, dict):
        for key in ("verdict", "outcome", "body"):
            if key in metadata and _evidence_text(metadata[key]):
                return _evidence_text(metadata[key])
    lines = [line.strip() for line in str(result or "").splitlines() if line.strip()]
    if lines and lines[-1].startswith("SHIPFACTORY_VERDICT:"):
        try:
            payload = json.loads(lines[-1].split(":", 1)[1].strip())
        except (ValueError, json.JSONDecodeError):
            return _evidence_text(lines[-1])
        return _evidence_text(payload.get("body") or payload.get("outcome"))
    return _evidence_text(result)


def _resume_note(db: Any, conn: Any, instance: dict[str, Any], recipe: dict[str, Any],
                 definition: dict[str, Any], task_id: str) -> None:
    """Refresh one parked gate with its task-run evidence case file."""
    from hermes_cli import kanban_db
    if conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? "
        "AND body LIKE 'CONTINUE-HERE%Evidence-Bundle: v2%' LIMIT 1",
        (task_id,),
    ).fetchone():
        return
    defs = {item["id"]: item for item in recipe["steps"]}
    ancestors: set[str] = set()
    def collect(node: str) -> None:
        for parent in defs[node]["needs"]:
            if parent not in ancestors:
                ancestors.add(parent)
                collect(parent)
    collect(definition["id"])
    summaries: list[str] = []
    for upstream_id in [item["id"] for item in recipe["steps"] if item["id"] in ancestors]:
        row = db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND state='done' "
            "ORDER BY activation DESC LIMIT 1",
            (instance["id"], upstream_id),
        ).fetchone()
        if row and row["kanban_task_id"]:
            task = kanban_db.get_task(conn, row["kanban_task_id"])
            run = conn.execute(
                "SELECT summary,metadata FROM task_runs WHERE task_id=? "
                "ORDER BY COALESCE(ended_at,started_at) DESC,id DESC LIMIT 1",
                (row["kanban_task_id"],),
            ).fetchone()
            summary = run["summary"] if run else None
            try:
                metadata = json.loads(run["metadata"]) if run and run["metadata"] else None
            except (TypeError, json.JSONDecodeError):
                metadata = run["metadata"] if run else None
            result = task.result if task else None
            one_liner = _result_one_liner(summary) or _result_one_liner(result)
            summaries.append(f"- {upstream_id}: {one_liner or 'completed; no prose summary'}")
            commit = _commit_hash(metadata, summary, result)
            if commit:
                summaries.append(f"  Commit: {commit}")
            tests = _test_counts(metadata, summary, result)
            if tests:
                summaries.append(f"  Tests: {', '.join(tests)}")
            if defs[upstream_id]["primitive"] == "review_gate":
                verdict = _verdict_text(result, metadata)
                if verdict:
                    summaries.append(f"  Verdict: {verdict}")
            metadata_text = _evidence_text(metadata)
            if metadata_text:
                summaries.append(f"  Run metadata: {metadata_text}")
    children = sorted(item["id"] for item in recipe["steps"] if definition["id"] in item["needs"])
    unblocks = ", ".join(children) if children else "recipe completion"
    if definition["primitive"] == "approval_gate":
        awaited = f"Approval required: {definition['params']['instructions']} This unblocks {unblocks}."
        next_action = f"Record approve or reject for instance {instance['id']} step {definition['id']}."
    else:
        awaited = f"Event required: {definition['params']['event']}. This unblocks {unblocks}."
        next_action = f"Emit the matching {definition['params']['event']} event for instance {instance['id']} step {definition['id']}."
    latest = {item["step_id"]: item for item in _latest(db, instance["id"])}
    chain = " -> ".join(
        f"{item['id']} ({latest[item['id']]['state']})" for item in recipe["steps"]
    )
    body = "\n".join([
        "CONTINUE-HERE",
        "Evidence-Bundle: v2",
        f"Instance: {instance['id']}",
        f"Step: {definition['id']}",
        "Status: blocked / needs_input",
        f"Updated: {store._now()}",
        "",
        "## Where We Are",
        f"The recipe is parked at {definition['id']} pending operator input.",
        "",
        "## Done",
        *(summaries or ["- No completed upstream summary was available."]),
        "",
        "## Step Chain",
        chain,
        "",
        "## Budget",
        f"Tokens charged: {instance['tokens_charged']} / {recipe['budgets']['max_tokens']}",
        "",
        "## Left",
        f"- Resume {unblocks} after this gate is consumed.",
        "",
        "## Decisions and Why",
        "None recorded in this gate note.",
        "",
        "## Blockers",
        awaited,
        "",
        "## Next Action",
        next_action,
    ])
    task = kanban_db.get_task(conn, task_id)
    task_body = task.body if task else definition["params"].get("instructions", "")
    marker = "\n\n---\n\nCONTINUE-HERE\nEvidence-Bundle: v2"
    task_body = task_body.split(marker, 1)[0]
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET body=? WHERE id=?",
            (f"{task_body.rstrip()}\n\n---\n\n{body}", task_id),
        )
    kanban_db.add_comment(conn, task_id, "shipfactory", body)

def _consume_resume_note(conn: Any, task_id: str | None) -> None:
    """Mark one parked note consumed without adding a Factory table."""
    from hermes_cli import kanban_db
    if not task_id:
        return
    has_note = conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'CONTINUE-HERE%' LIMIT 1",
        (task_id,),
    ).fetchone()
    consumed = conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'RESUMED %' LIMIT 1",
        (task_id,),
    ).fetchone()
    if has_note and not consumed:
        kanban_db.add_comment(conn, task_id, "shipfactory", f"RESUMED {store._now()}")

def _resolve_target_step(db: Any, instance_id: str, recipe: dict[str, Any], target: str) -> str:
    """Map a verdict's target to a recipe step id, accepting kanban task ids.

    Review workers are prompted with kanban TASK ids (their context and the
    summary frontmatter both carry ``t_...``), so verdicts routinely cite the
    task id of the step they want reworked rather than the recipe's abstract
    step id (finding #25b, t_10fdf585 targeting t_3b0e86d6). Accept both:
    return *target* unchanged when it is a known step id; otherwise look up
    which step activation created that kanban task and return its step id.
    Unknown targets return unchanged so ``_invalidate_cone`` raises its
    existing precise error.
    """
    if any(step["id"] == target for step in recipe["steps"]):
        return target
    row = db.execute(
        "SELECT step_id FROM recipe_steps WHERE instance_id=? AND kanban_task_id=? "
        "ORDER BY activation DESC LIMIT 1",
        (instance_id, target),
    ).fetchone()
    return row["step_id"] if row else target


def _rejecting_gate_task(db: Any, conn: Any, instance_id: str, recipe: dict[str, Any], step_id: str) -> str | None:
    """Find the review-gate kanban task whose verdict sent *step_id* to rework.

    Rework activations otherwise inherit only their recipe-DAG ``needs``
    parents, so the verifier's cited findings never reach the rework worker's
    context — it rebuilds from the original instructions blind (finding #26,
    t_1082ec9b). Returns the most recent changes-requested gate task whose
    verdict targets *step_id*, or ``None``.
    """
    from hermes_cli import kanban_db

    rows = db.execute(
        "SELECT kanban_task_id FROM recipe_steps WHERE instance_id=? AND primitive='review_gate' "
        "AND state='blocked' AND blocked_reason='changes_requested' AND kanban_task_id IS NOT NULL "
        "ORDER BY updated_at DESC, activation DESC",
        (instance_id,),
    ).fetchall()
    for row in rows:
        task = kanban_db.get_task(conn, row["kanban_task_id"])
        if task is None or not task.result:
            continue
        try:
            verdict = parse_verdict(task.result)
        except ValueError:
            continue
        if verdict["outcome"] != "request_changes":
            continue
        if _resolve_target_step(db, instance_id, recipe, verdict["target_step"]) == step_id:
            return row["kanban_task_id"]
    return None


def _invalidate_cone(db: Any, instance: dict[str, Any], recipe: dict[str, Any], target: str, rejecting_step: str, source: str) -> None:
    """Insert (never overwrite) a new activation cone through a rejecting gate."""
    defs = {x["id"]: x for x in recipe["steps"]}
    if target not in defs or defs[target]["primitive"] != "agent_task":
        raise ValueError("review change target must be an upstream agent_task")
    if recipe.get("schema") == "shipfactory.recipe/v2":
        declared_targets = {
            item["from"] for item in defs[rejecting_step].get("inputs", [])
            if defs[item["from"]]["primitive"] == "agent_task"
        }
        if declared_targets and target not in declared_targets:
            raise ValueError("review change target is not its declared artifact producer")
    parents = {item["id"]: set(item["needs"]) for item in recipe["steps"]}
    def upstream(node: str) -> set[str]:
        return parents[node] | set().union(*(upstream(x) for x in parents[node])) if parents[node] else set()
    if target not in upstream(rejecting_step): raise ValueError("review target is not transitive upstream")
    # A reviewed producer may have advanced its worktree from base_sha to
    # head_sha.  Rework is the point where that head legitimately becomes the
    # instance base: every artifact in the new cone must be rebuilt against it.
    target_artifact = db.execute(
        "SELECT head_sha FROM artifacts WHERE instance_id=? AND step_id=? "
        "AND state='sealed' AND head_sha IS NOT NULL "
        "ORDER BY activation DESC,sealed_at DESC LIMIT 1",
        (instance["id"], target),
    ).fetchone()
    if target_artifact and target_artifact["head_sha"] != instance.get("base_sha"):
        now = store._now()
        db.execute(
            "UPDATE recipe_instances SET base_sha=?,updated_base_at=?,updated_at=? WHERE id=?",
            (target_artifact["head_sha"], now, now, instance["id"]),
        )
        instance["base_sha"] = target_artifact["head_sha"]
    # cone is every node on/after target that reaches the rejecting gate.
    children = {name: set() for name in defs}
    for name, needs in parents.items():
        for parent in needs: children[parent].add(name)
    descendants: set[str] = set()
    def down(node: str):
        if node not in descendants:
            descendants.add(node)
            for child in children[node]: down(child)
    down(target)
    cone = {node for node in descendants if node == rejecting_step or rejecting_step in descendants and node in upstream(rejecting_step) | {rejecting_step}}
    # The preceding expression intentionally retains target->review paths;
    # include downstream review ancestors deterministically.
    cone = {node for node in defs if target == node or target in upstream(node)} & {node for node in defs if node == rejecting_step or node in upstream(rejecting_step)}
    now = store._now()
    for node in sorted(cone):
        current = db.execute("SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1", (instance["id"], node)).fetchone()
        if not current: continue
        activation = int(current["activation"]) + 1
        db.execute("INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (instance["id"], node, activation, defs[node]["primitive"], "pending", now, now))


def _fresh_activation(db: Any, instance: dict[str, Any], definition: dict[str, Any],
                      step: dict[str, Any], source: str) -> bool:
    """Insert one clean activation after a missing/mismatched board task."""
    key = advance_key(
        instance["id"], instance["recipe_hash"], step["step_id"],
        step["activation"], "reactivate", source,
    )
    if db.execute(
        "SELECT 1 FROM advance_events WHERE key=? AND state IN ('applied','discarded','failed')", (key,)
    ).fetchone():
        return False
    owner = f"transition:{os.getpid()}"
    db.execute(
        "INSERT OR IGNORE INTO advance_events"
        "(key,instance_id,source,payload_json,state,created_at,lease_owner,lease_until,"
        "attempt_count,expected_activation,expected_state) "
        "VALUES(?,?,?,?, 'leased',?,?,?,?,?,?)",
        (key, instance["id"], source, "{}", store._now(), owner,
         (datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)).isoformat(),
         1, step["activation"], step["state"]),
    )
    activation = int(step["activation"]) + 1
    now = store._now()
    db.execute(
        "INSERT OR IGNORE INTO recipe_steps"
        "(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
        "VALUES(?,?,?,?, 'pending',?,?)",
        (instance["id"], step["step_id"], activation, definition["primitive"], now, now),
    )
    inserted = bool(db.execute("SELECT changes()").fetchone()[0])
    db.execute(
        "UPDATE advance_events SET state='applied',applied_at=?,outcome='fresh_activation',"
        "lease_owner=NULL,lease_until=NULL WHERE key=?",
        (store._now(), key),
    )
    return inserted

def reconcile(conn: Any, instance_id: str, *, profiles: dict[str, dict[str, Any]] | None = None,
              board_day_token_ceiling: int = 10**18,
              verification_profiles: dict[str, dict[str, Any]] | None = None,
              environment_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Idempotently reconcile every nonterminal activation with kanban/outbox."""
    from hermes_cli import kanban_db
    profiles = profiles or {"standard": {"max_runtime_seconds": 1800, "max_retries": 2, "token_allowance": 50000}}
    verification_profiles = verification_profiles or {}
    environment_config = environment_config or {}
    with store._connect() as db:
        initial = _instance(db, instance_id)
        if not initial: raise ValueError("unknown recipe instance")
        max_passes = max(4, 4 * len(recipe_for_instance(initial).document["steps"]))
    # Reach a local fixpoint so one restart pass can observe a completion and
    # activate its dependent. Kanban mutations remain idempotent by task key.
    status = "running"
    for _ in range(max_passes):
        changed = False
        with store._connect() as db:
            instance = _instance(db, instance_id)
            if not instance: raise ValueError("unknown recipe instance")
            if instance["status"] in {"cancelling", "cancelled"}:
                return {"instance_id": instance_id, "status": instance["status"]}
            recipe = recipe_for_instance(instance).document
            params = json.loads(instance["parameters_json"])
            defs = {x["id"]: x for x in recipe["steps"]}

            # Observe external task state before dependency readiness.
            for step in _latest(db, instance_id):
                if (step["state"] == "running_verification"
                        and defs[step["step_id"]]["primitive"] == "verification"):
                    from shipfactory.verification import verify_evidence_bundle
                    bundle = db.execute(
                        "SELECT * FROM evidence_bundles WHERE instance_id=? AND step_id=? "
                        "AND activation=?",
                        (instance_id, step["step_id"], int(step["activation"])),
                    ).fetchone()
                    if bundle and bundle["state"] in {"done", "blocked", "failed"}:
                        try:
                            verify_evidence_bundle(bundle["id"], db=db)
                        except Exception as exc:
                            changed |= _transition(
                                db, instance, step, "failed", f"evidence:{bundle['id']}",
                                reason=f"evidence_invariant: {exc}",
                            )
                        else:
                            target = "done" if bundle["state"] == "done" else bundle["state"]
                            if target == "done":
                                db.execute(
                                    "UPDATE recipe_steps SET output_artifact_set_hash=?,updated_at=? "
                                    "WHERE instance_id=? AND step_id=? AND activation=?",
                                    (hashlib.sha256(
                                        f"evidence-bundle:{bundle['bundle_sha256']}".encode()
                                     ).hexdigest(), store._now(), instance_id,
                                     step["step_id"], int(step["activation"])),
                                )
                            changed |= _transition(
                                db, instance, step, target, f"evidence:{bundle['id']}",
                                reason=bundle["invalid_reason"],
                            )
                    continue
                # notify steps have no kanban task — observe outbox delivery
                # instead (shakedown finding #19: notify parked in waiting
                # forever; nothing transitioned it after delivery).
                if step["state"] == "waiting" and defs[step["step_id"]]["primitive"] == "notify" and not step["kanban_task_id"]:
                    okey = task_key(instance["id"], instance["recipe_hash"], step["step_id"], step["activation"])
                    row = db.execute("SELECT state FROM outbox WHERE key=?", (okey,)).fetchone()
                    if row and row["state"] == "delivered":
                        changed |= _transition(db, instance, dict(step), "done", f"outbox:{okey[-12:]}")
                    elif row and row["state"] == "failed":
                        changed |= _transition(db, instance, dict(step), "blocked", f"outbox:{okey[-12:]}", reason="notify_delivery_failed")
                    continue
                if step["state"] not in {"pending", "ready", "running", "waiting", "blocked"} or not step["kanban_task_id"]:
                    continue
                definition = defs[step["step_id"]]
                if step["state"] == "waiting" and step["primitive"] in {"approval_gate", "wait_for_event"}:
                    _resume_note(db, conn, instance, recipe, definition, step["kanban_task_id"])
                task = kanban_db.get_task(conn, step["kanban_task_id"])
                if task is None:
                    changed |= _fresh_activation(
                        db, instance, definition, step,
                        f"kanban:missing:{step['kanban_task_id']}",
                    )
                elif task.status == "done":
                    if recipe.get("schema") == "shipfactory.recipe/v2":
                        from shipfactory.artifacts import (
                            ArtifactValidationError,
                            artifact_set_hash,
                            output_artifacts,
                        )
                        try:
                            outputs = output_artifacts(
                                db, instance_id, step["step_id"],
                                int(step["activation"]), definition,
                            )
                        except ArtifactValidationError as exc:
                            changed |= _transition(
                                db, instance, step, "blocked", f"kanban:{task.id}",
                                reason=f"worker_failed: {exc}",
                            )
                            continue
                        db.execute(
                            "UPDATE recipe_steps SET output_artifact_set_hash=?,updated_at=? "
                            "WHERE instance_id=? AND step_id=? AND activation=?",
                            (
                                artifact_set_hash(outputs), store._now(), instance_id,
                                step["step_id"], step["activation"],
                            ),
                        )
                    if step["primitive"] == "review_gate":
                        try:
                            verdict = parse_verdict(task.result or "")
                            if verdict["outcome"] == "approve":
                                approval_blocker = _review_approval_blocker(
                                    db, instance_id, definition,
                                    verdict_body=verdict.get("body", ""), defs=defs,
                                    recipe=recipe, conn=conn,
                                    latest={x["step_id"]: x for x in _latest(db, instance_id)},
                                )
                                if approval_blocker:
                                    changed |= _transition(
                                        db, instance, step, "blocked",
                                        f"kanban:{task.id}", reason=approval_blocker,
                                    )
                                    db.execute(
                                        "UPDATE recipe_instances SET status='blocked',"
                                        "blocked_reason=?,updated_at=? WHERE id=?",
                                        (approval_blocker, store._now(), instance_id),
                                    )
                                    continue
                            if verdict["outcome"] == "request_changes":
                                finding_count = _verdict_finding_count(verdict["body"])
                                db.execute(
                                    "UPDATE recipe_steps SET finding_count=?,updated_at=? "
                                    "WHERE instance_id=? AND step_id=? AND activation=?",
                                    (finding_count, store._now(), instance_id, step["step_id"], step["activation"]),
                                )
                                if _review_stalled(db, instance_id, step, finding_count):
                                    changed |= _transition(
                                        db, instance, step, "blocked", f"kanban:{task.id}",
                                        reason="review_stall",
                                    )
                                    db.execute(
                                        "UPDATE recipe_instances SET status='blocked',blocked_reason='review_stall',updated_at=? WHERE id=?",
                                        (store._now(), instance_id),
                                    )
                                    continue
                                _invalidate_cone(
                                    db, instance, recipe,
                                    _resolve_target_step(db, instance_id, recipe, verdict["target_step"]),
                                    step["step_id"], f"kanban:{task.id}",
                                )
                                changed |= _transition(db, instance, step, "blocked", f"kanban:{task.id}", reason="changes_requested")
                                continue
                        except ValueError as exc:
                            changed |= _transition(db, instance, step, "blocked", f"kanban:{task.id}", reason=str(exc))
                            continue
                    if step["primitive"] in {"approval_gate", "wait_for_event"}:
                        _consume_resume_note(conn, step["kanban_task_id"])
                    changed |= _transition(db, instance, step, "done", f"kanban:{task.id}")
                elif task.status == "blocked" and step["primitive"] in {"agent_task", "review_gate"}:
                    visible_reason = "worker_blocked"
                    if recipe.get("schema") == "shipfactory.recipe/v2":
                        visible_reason = str(
                            getattr(task, "blocked_reason", "") or visible_reason
                        )
                    changed |= _transition(
                        db, instance, step, "blocked", f"kanban:{task.id}",
                        reason=visible_reason,
                    )
                elif task.status in KANBAN_TERMINAL:
                    changed |= _fresh_activation(
                        db, instance, definition, step,
                        f"kanban:{task.id}:{task.status}",
                    )

            latest = {x["step_id"]: x for x in _latest(db, instance_id)}
            for step_id, step in list(latest.items()):
                if step["state"] != "pending": continue
                if all(latest[parent]["state"] in {"done", "skipped"} for parent in defs[step_id]["needs"]):
                    if recipe.get("schema") == "shipfactory.recipe/v2":
                        from shipfactory.artifacts import (
                            ArtifactMissing,
                            ArtifactStale,
                            artifact_set_hash,
                            input_artifacts,
                        )
                        try:
                            inputs = input_artifacts(db, instance_id, defs[step_id])
                        except ArtifactStale:
                            changed |= _transition(
                                db, instance, step, "blocked", "artifacts",
                                reason="artifact_stale",
                            )
                            continue
                        except ArtifactMissing:
                            changed |= _transition(
                                db, instance, step, "blocked", "artifacts",
                                reason="artifact_missing",
                            )
                            continue
                        db.execute(
                            "UPDATE recipe_steps SET input_artifact_set_hash=?,updated_at=? "
                            "WHERE instance_id=? AND step_id=? AND activation=?",
                            (
                                artifact_set_hash(inputs), store._now(), instance_id,
                                step_id, step["activation"],
                            ),
                        )
                        step["input_artifact_set_hash"] = artifact_set_hash(inputs)
                    if step["primitive"] in {"review_gate", "approval_gate"}:
                        vector = revision_vector(db, instance_id, step, recipe)
                        db.execute(
                            "UPDATE recipe_steps SET input_revision_hash=? WHERE instance_id=? AND step_id=? AND activation=?",
                            (vector, instance_id, step_id, step["activation"]),
                        )
                        step["input_revision_hash"] = vector
                    changed |= _transition(db, instance, step, "ready", "reconcile")

            latest = {x["step_id"]: x for x in _latest(db, instance_id)}
            for sid, step in latest.items():
                if step["state"] != "ready": continue
                definition = defs[sid]; primitive = definition["primitive"]
                instance = _instance(db, instance_id)
                if primitive == "verification":
                    from shipfactory.verification import (
                        load_verification_manifest,
                        load_verification_manifest_if_present,
                    )
                    profile_name = _bind_text(definition["params"]["profile"], params)
                    profile = verification_profiles.get(profile_name)
                    if profile is None:
                        changed |= _transition(
                            db, instance, step, "failed", "profile",
                            reason="missing verification profile",
                        )
                        continue
                    workspace, workspace_owner_task_id = _step_change_set_workspace(
                        conn, latest, definition,
                    )
                    if not workspace:
                        changed |= _transition(
                            db, instance, step, "failed", "workspace",
                            reason="verification candidate workspace missing",
                        )
                        continue
                    owner_step = (
                        db.execute(
                            "SELECT activation,producer_run_id FROM recipe_steps "
                            "WHERE kanban_task_id=?",
                            (workspace_owner_task_id,),
                        ).fetchone()
                        if workspace_owner_task_id else None
                    )
                    if (owner_step is None or owner_step["producer_run_id"] is None):
                        changed |= _transition(
                            db, instance, step, "failed", "workspace",
                            reason="verification exact producer task/activation/run is missing",
                        )
                        continue
                    workspace_owner_activation = int(owner_step["activation"])
                    workspace_owner_run_id = int(owner_step["producer_run_id"])
                    change_set = None
                    required_requirement_ids: set[str] = set()
                    surface_documents: list[dict[str, Any]] = []
                    for declared_input in definition.get("inputs", []):
                        artifact = db.execute(
                            "SELECT * FROM artifacts WHERE instance_id=? AND step_id=? "
                            "AND kind=? AND state='sealed' ORDER BY activation DESC LIMIT 1",
                            (instance_id, declared_input["from"], declared_input["kind"]),
                        ).fetchone()
                        if artifact and declared_input["kind"] == "change-set":
                            change_set = dict(artifact)
                        if artifact and declared_input["kind"] == "task-spec":
                            from shipfactory.artifacts import artifact_document
                            spec = artifact_document(dict(artifact))
                            surface_documents.append(spec)
                            required_requirement_ids.update(
                                item["id"] for item in spec.get("requirements", [])
                                if isinstance(item, dict) and isinstance(item.get("id"), str)
                            )
                        if artifact and declared_input["kind"] == "plan":
                            from shipfactory.artifacts import artifact_document
                            surface_documents.append(artifact_document(dict(artifact)))
                    try:
                        head_sha = (
                            change_set.get("head_sha") if change_set else subprocess.check_output(
                                ["git", "rev-parse", "HEAD"], cwd=workspace, text=True,
                                stderr=subprocess.PIPE, timeout=10,
                            ).strip()
                        )
                        tree_sha = (
                            change_set.get("repo_tree_sha") if change_set else subprocess.check_output(
                                ["git", "rev-parse", "HEAD^{tree}"], cwd=workspace, text=True,
                                stderr=subprocess.PIPE, timeout=10,
                            ).strip()
                        )
                        manifest_relpath = _bind_text(
                            definition["params"]["manifest"], params,
                        )
                        protected = load_verification_manifest(
                            workspace, instance["base_sha"], manifest_relpath,
                            verify_worktree_copy=False,
                        )
                        candidate = load_verification_manifest_if_present(
                            workspace, head_sha, manifest_relpath,
                            required_requirement_ids=required_requirement_ids,
                        )
                        from shipfactory.verification import (
                            classify_required_surface, surface_paths_from_documents,
                        )
                        diff_paths = subprocess.check_output(
                            ["git", "diff", "--name-only", instance["base_sha"], head_sha],
                            cwd=workspace, text=True, stderr=subprocess.PIPE, timeout=10,
                        ).splitlines()
                        surface_paths = sorted(set(diff_paths + surface_paths_from_documents(
                            *surface_documents,
                        )))
                        required_surface = classify_required_surface(
                            surface_paths,
                            model_risk_surface=profile.get("model_risk_surface"),
                        )
                    except Exception as exc:
                        changed |= _transition(
                            db, instance, step, "failed", "verification_manifest",
                            reason=f"evidence_invariant: {exc}",
                        )
                        continue
                    logical_key = hashlib.sha256(
                        f"{instance_id}|{sid}|{step['activation']}|verification".encode()
                    ).hexdigest()
                    _plan_action(
                        db, logical_key=logical_key, kind="verification_run",
                        payload={
                            "instance_id": instance_id, "step_id": sid,
                            "activation": int(step["activation"]),
                            "input_revision_hash": step.get("input_artifact_set_hash") or "none",
                            "base_sha": instance["base_sha"], "head_sha": head_sha,
                            "tree_sha": tree_sha, "workspace": str(workspace),
                            "workspace_owner_task_id": workspace_owner_task_id,
                            "workspace_owner_activation": workspace_owner_activation,
                            "workspace_owner_run_id": workspace_owner_run_id,
                            "required_surface": required_surface,
                            "model_risk_surface": profile.get("model_risk_surface"),
                            "manifest_relpath": manifest_relpath,
                            "manifest_blob_sha": (candidate or protected).blob_sha,
                            "candidate_manifest_blob_sha": (
                                candidate.blob_sha if candidate is not None else None
                            ),
                            "protected_manifest_blob_sha": protected.blob_sha,
                            "required_requirement_ids": sorted(required_requirement_ids),
                            "profile": profile, "environment": "app",
                            "environment_config": environment_config,
                        },
                        instance_id=instance_id, step_id=sid,
                        activation=int(step["activation"]),
                    )
                    changed |= _transition(
                        db, instance, step, "running_verification", "activate"
                    )
                    continue
                if primitive in {"agent_task", "review_gate"}:
                    profile_name = _bind_text(
                        definition["params"]["execution_profile"], params,
                    )
                    profile = profiles.get(profile_name)
                    if not profile:
                        changed |= _transition(db, instance, step, "failed", "profile", reason="missing execution profile")
                        continue
                    fuse = _admit(
                        db, instance, recipe, step, profile, profile_name,
                        int(board_day_token_ceiling),
                    )
                    if fuse:
                        changed |= _transition(db, instance, step, "blocked", "fuse", reason=fuse)
                        db.execute("UPDATE recipe_instances SET blocked_reason=? WHERE id=?", (fuse, instance_id))
                        continue
                parents = [latest[parent]["kanban_task_id"] for parent in definition["needs"] if latest[parent]["kanban_task_id"]]
                if primitive == "agent_task" and int(step["activation"]) > 1:
                    # Finding #26: hand the rework worker the rejecting verdict.
                    gate_task = _rejecting_gate_task(db, conn, instance_id, recipe, sid)
                    if gate_task and gate_task not in parents:
                        parents.append(gate_task)
                task = activate(conn, instance, recipe, definition, step, params, parents, db=db)
                state = "running" if primitive in {"agent_task", "review_gate"} else "waiting"
                changed |= _transition(db, instance, step, state, "activate", task=task)
                if primitive in {"approval_gate", "wait_for_event"} and task:
                    _resume_note(db, conn, instance, recipe, definition, task)

            instance = _instance(db, instance_id)
            status = _summary(db, instance)
            db.execute("UPDATE recipe_instances SET status=?,updated_at=? WHERE id=?", (status, store._now(), instance_id))
            if status == "done":
                kanban_db.complete_task(conn, instance["collector_task_id"], summary="recipe complete")
            db.commit()
        if not changed:
            break
    else:
        raise RuntimeError("recipe reconciliation did not reach a fixpoint")
    return {"instance_id": instance_id, "status": status}

def _action_matches_board(db: Any, row: dict[str, Any], board: str | None) -> bool:
    if board is None:
        return True
    if row.get("instance_id"):
        instance = db.execute(
            "SELECT board FROM recipe_instances WHERE id=?", (row["instance_id"],)
        ).fetchone()
        return bool(instance and instance["board"] == board)
    try:
        return json.loads(row["payload_json"]).get("board") == board
    except (TypeError, json.JSONDecodeError):
        return False


def _new_action_attempt(db: Any, row: dict[str, Any]) -> str:
    """Insert, never overwrite, the next attempt for a retryable action."""
    attempt = int(row["attempt"]) + 1
    key = _action_key(row["logical_key"], attempt)
    db.execute(
        "INSERT OR IGNORE INTO action_intents"
        "(key,logical_key,attempt,instance_id,step_id,activation,kind,payload_json,state,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'planned',?)",
        (key, row["logical_key"], attempt, row.get("instance_id"), row.get("step_id"),
         row.get("activation"), row["kind"], row["payload_json"], store._now()),
    )
    return key


def _claim_action(*, owner: str, board: str | None, kinds: set[str] | None,
                  now: str) -> dict[str, Any] | None:
    """Claim one ready action in a short Factory transaction."""
    store.init_db()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        expired = [dict(row) for row in db.execute(
            "SELECT * FROM action_intents WHERE state='leased' AND lease_until<=? "
            "ORDER BY lease_until,key", (now,),
        ).fetchall()]
        for row in expired:
            db.execute(
                "UPDATE action_intents SET state='retryable_failed',finished_at=?,"
                "last_error='action lease expired',lease_owner=NULL,lease_until=NULL "
                "WHERE key=? AND state='leased'",
                (now, row["key"]),
            )
            if row["kind"] == "notification_delivery":
                # DOUBLE-SEND RISK, deliberate policy: the transport has no
                # probe or idempotency token, so a lease that expired after
                # `hermes send` fired but before recording WILL resend on
                # retry. A duplicate notification is annoying; a silently
                # dropped one hides a parked gate from the operator. We
                # choose duplicates, and record the risk on the intent so
                # the audit trail says so.
                db.execute(
                    "UPDATE action_intents SET last_error="
                    "'action lease expired; retry may double-send (no transport probe)' "
                    "WHERE key=?",
                    (row["key"],),
                )
                db.execute(
                    "UPDATE outbox SET state='pending',lease_owner=NULL,lease_until=NULL "
                    "WHERE key=? AND state='leased'",
                    (json.loads(row["payload_json"])["outbox_key"],),
                )

        retryable = [dict(row) for row in db.execute(
            "SELECT a.* FROM action_intents a WHERE a.state='retryable_failed' "
            "AND NOT EXISTS (SELECT 1 FROM action_intents newer "
            "WHERE newer.logical_key=a.logical_key AND newer.attempt>a.attempt) "
            "ORDER BY a.created_at,a.key"
        ).fetchall()]
        for row in retryable:
            if not _action_matches_board(db, row, board):
                continue
            if kinds is not None and row["kind"] not in kinds:
                continue
            if row["kind"] == "notification_delivery":
                outbox_key = json.loads(row["payload_json"])["outbox_key"]
                due = db.execute(
                    "SELECT 1 FROM outbox WHERE key=? AND state='pending' AND next_attempt_at<=?",
                    (outbox_key, now),
                ).fetchone()
                if not due:
                    continue
            _new_action_attempt(db, row)

        candidates = [dict(row) for row in db.execute(
            "SELECT * FROM action_intents WHERE state='planned' ORDER BY created_at,key"
        ).fetchall()]
        selected = None
        for row in candidates:
            if kinds is not None and row["kind"] not in kinds:
                continue
            if not _action_matches_board(db, row, board):
                continue
            if row["kind"] == "notification_delivery":
                outbox_key = json.loads(row["payload_json"])["outbox_key"]
                due = db.execute(
                    "SELECT 1 FROM outbox WHERE key=? AND state='pending' AND next_attempt_at<=?",
                    (outbox_key, now),
                ).fetchone()
                if not due:
                    continue
            selected = row
            break
        if selected is None:
            return None
        lease_until = (
            datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)
        ).isoformat()
        changed = db.execute(
            "UPDATE action_intents SET state='leased',lease_owner=?,lease_until=?,started_at=? "
            "WHERE key=? AND state='planned'",
            (owner, lease_until, now, selected["key"]),
        ).rowcount
        if changed != 1:
            return None
        if selected["kind"] == "notification_delivery":
            outbox_key = json.loads(selected["payload_json"])["outbox_key"]
            changed = db.execute(
                "UPDATE outbox SET state='leased',lease_owner=?,lease_until=? "
                "WHERE key=? AND state='pending' AND next_attempt_at<=?",
                (owner, lease_until, outbox_key, now),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"outbox {outbox_key} could not be leased")
        selected.update({"lease_owner": owner, "lease_until": lease_until, "state": "leased"})
        return selected


def _current_action_target(row: dict[str, Any]) -> bool:
    """Return whether an unperformed step-bound action is still current."""
    if not row.get("instance_id") or not row.get("step_id"):
        return True
    with store._connect() as db:
        step = db.execute(
            "SELECT activation,state FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1",
            (row["instance_id"], row["step_id"]),
        ).fetchone()
    return bool(
        step and int(step["activation"]) == int(row["activation"])
        and step["state"] in {"waiting", "running_verification"}
    )


def _execute_action(conn: Any, row: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Probe then perform one external effect with no Factory transaction held."""
    from hermes_cli import kanban_db

    payload = json.loads(row["payload_json"])
    kind = row["kind"]
    if kind == "worker_task_transition":
        task_id = payload["task_id"]
        desired = payload["result"]
        factory_run = store.run_row(int(payload["run_id"]))
        task_attempt_id = payload.get("task_attempt_id")
        identity_mismatch = (
            factory_run is None
            or factory_run["task_id"] != task_id
            or factory_run.get("board") != payload.get("board")
            or factory_run.get("process_start_token") != payload.get("process_start_token")
            or factory_run.get("task_attempt_id") != task_attempt_id
            or task_attempt_id is None
        )
        if identity_mismatch:
            return "abandoned", {
                "task_id": task_id,
                "probe": "run_identity_mismatch",
                "run_id": payload.get("run_id"),
            }, "worker transition run identity missing or mismatched"
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return "terminal_failed", {"task_id": task_id}, "kanban task missing"
        status = task.status
        if desired != "done" and status == "blocked":
            return "succeeded", {"task_id": task_id, "probe": "already_blocked"}, None
        if status in KANBAN_TERMINAL:
            if (desired == "done" and status == "done") or (
                desired != "done" and status == "blocked"
            ):
                return "succeeded", {"task_id": task_id, "probe": f"already_{status}"}, None
            return "abandoned", {"task_id": task_id, "probe": f"terminal_{status}"}, None
        if task.current_run_id != int(task_attempt_id):
            return "abandoned", {
                "task_id": task_id,
                "probe": "task_attempt_mismatch",
                "expected_task_attempt_id": int(task_attempt_id),
                "observed_task_attempt_id": task.current_run_id,
            }, "worker transition belongs to a superseded task attempt"
        if desired == "done":
            try:
                changed = kanban_db.complete_task(
                    conn, task_id, result=payload["summary"], summary=payload["summary"],
                    expected_run_id=int(task_attempt_id),
                )
            except TypeError:
                changed = kanban_db.complete_task(
                    conn, task_id, summary=payload["summary"],
                    expected_run_id=int(task_attempt_id),
                )
            expected = "done"
        else:
            changed = kanban_db.block_task(
                conn, task_id, reason=payload["summary"],
                expected_run_id=int(task_attempt_id),
            )
            expected = "blocked"
        verified = kanban_db.get_task(conn, task_id)
        if not changed or not verified or verified.status != expected:
            return "retryable_failed", {
                "task_id": task_id, "transition_return": bool(changed),
                "observed_status": getattr(verified, "status", None),
            }, f"kanban {expected} transition was not verified"
        conn.commit()
        return "succeeded", {"task_id": task_id, "probe": f"set_{expected}"}, None
    if kind in {"approval_gate_completion", "triage_root_completion"}:
        task_id = payload["task_id"]
        task = kanban_db.get_task(conn, task_id)
        if task and task.status == "done":
            return "succeeded", {"task_id": task_id, "probe": "already_done"}, None
        if kind == "approval_gate_completion" and not _current_action_target(row):
            return "abandoned", {"task_id": task_id, "probe": "stale_activation"}, None
        if kind == "triage_root_completion":
            links = conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id=?", (task_id,)
            ).fetchall()
            parents = [kanban_db.get_task(conn, link["parent_id"]) for link in links]
            if not links or not all(parent and parent.status == "done" for parent in parents):
                return "abandoned", {"task_id": task_id, "probe": "parents_not_done"}, None
        if task is None:
            return "terminal_failed", {"task_id": task_id}, "kanban task missing"
        if kind == "approval_gate_completion" and task.status == "blocked":
            kanban_db.unblock_task(conn, task_id)
        completed = kanban_db.complete_task(conn, task_id, summary=payload["summary"])
        verified = kanban_db.get_task(conn, task_id)
        if not completed or not verified or verified.status != "done":
            return "retryable_failed", {"task_id": task_id, "complete_return": bool(completed)}, "kanban completion was not verified"
        # Durability before recording: the effect must survive a crash that
        # happens before the outcome row is written. Without this commit the
        # caller's open kanban transaction rolls the completion back on
        # process death and the recovery probe finds a still-blocked task —
        # the exact crash-ambiguity this journal exists to remove.
        conn.commit()
        return "succeeded", {"task_id": task_id, "probe": "completed"}, None
    if kind == "notification_delivery":
        outbox_key = payload["outbox_key"]
        with store._connect() as db:
            outbox = db.execute("SELECT * FROM outbox WHERE key=?", (outbox_key,)).fetchone()
        if outbox and outbox["state"] == "delivered":
            return "succeeded", {"outbox_key": outbox_key, "probe": "already_delivered"}, None
        if outbox is None:
            return "terminal_failed", {"outbox_key": outbox_key}, "outbox row missing"
        try:
            subprocess.run(
                ["hermes", "send", "--to", outbox["target"], outbox["message"]],
                check=True, capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:
            return "retryable_failed", {
                "outbox_key": outbox_key,
                "duplicate_risk": "transport_has_no_probe_or_idempotency_token",
            }, str(exc)[:500]
        return "succeeded", {
            "outbox_key": outbox_key,
            "probe": "sent",
            "duplicate_risk": "transport_has_no_probe_or_idempotency_token",
        }, None
    if kind == "verification_run":
        if not _current_action_target(row):
            return "abandoned", {"probe": "stale_activation"}, None
        from shipfactory.verification import run_action
        result = run_action(payload)
        if result["status"] == "pending":
            return "retryable_failed", result, str(result.get("reason") or "verification pending")
        return "succeeded", result, None
    return "terminal_failed", {}, f"unknown action kind {kind}"


def _record_action_outcome(row: dict[str, Any], state: str,
                           result: dict[str, Any], error: str | None) -> None:
    """Record an action result and make a failed logical effect retryable."""
    now = store._now()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE action_intents SET state=?,finished_at=?,result_json=?,last_error=?,"
            "lease_owner=NULL,lease_until=NULL WHERE key=? AND state='leased' AND lease_owner=?",
            (state, now, json.dumps(result, sort_keys=True), error, row["key"], row["lease_owner"]),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"lost action lease for {row['key']}")
        if row["kind"] == "notification_delivery":
            outbox_key = json.loads(row["payload_json"])["outbox_key"]
            outbox = db.execute("SELECT attempts FROM outbox WHERE key=?", (outbox_key,)).fetchone()
            attempts = int(outbox["attempts"] if outbox else 0) + 1
            if state == "succeeded":
                db.execute(
                    "UPDATE outbox SET state='delivered',attempts=?,delivered_at=?,last_error=NULL,"
                    "lease_owner=NULL,lease_until=NULL WHERE key=?",
                    (attempts, now, outbox_key),
                )
            elif attempts >= 8 or state == "terminal_failed":
                db.execute(
                    "UPDATE outbox SET state='failed',attempts=?,last_error=?,"
                    "lease_owner=NULL,lease_until=NULL WHERE key=?",
                    (attempts, error, outbox_key),
                )
                if state == "retryable_failed":
                    db.execute(
                        "UPDATE action_intents SET state='terminal_failed' WHERE key=?",
                        (row["key"],),
                    )
            else:
                due = (
                    datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** attempts))
                ).isoformat()
                db.execute(
                    "UPDATE outbox SET state='pending',attempts=?,next_attempt_at=?,last_error=?,"
                    "lease_owner=NULL,lease_until=NULL WHERE key=?",
                    (attempts, due, error, outbox_key),
                )
        if row["kind"] == "worker_task_transition" and state == "succeeded":
            payload = json.loads(row["payload_json"])
            if payload.get("result") == "done":
                db.execute(
                    "UPDATE recipe_steps SET producer_run_id=?,updated_at=? "
                    "WHERE kanban_task_id=?",
                    (int(payload["run_id"]), now, str(payload["task_id"])),
                )
        if state == "retryable_failed" and row["kind"] != "notification_delivery":
            _new_action_attempt(db, row)


def run_action_intents(conn: Any, *, board: str | None = None,
                       kinds: set[str] | None = None, limit: int = 100) -> int:
    """Run leased external effects outside Factory write transactions."""
    # Transaction-scope guard: _execute_action commits the kanban connection
    # at the effect boundary (durability before outcome recording). If the
    # caller hands us a connection with an open write transaction, that
    # commit would flush the caller's unrelated in-flight work early —
    # refuse instead of silently widening the commit scope.
    if conn is not None and getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "run_action_intents requires a transaction-clean connection; "
            "caller has an open write transaction"
        )
    owner = f"action:{os.getpid()}:{uuid.uuid4().hex}"
    succeeded = 0
    for _ in range(limit):
        row = _claim_action(owner=owner, board=board, kinds=kinds, now=store._now())
        if row is None:
            break
        try:
            state, result, error = _execute_action(conn, row)
        except Exception as exc:
            state, result, error = "retryable_failed", {}, str(exc)[:500]
        _record_action_outcome(row, state, result, error)
        if state == "succeeded":
            succeeded += 1
        elif state == "retryable_failed":
            # Leave the fresh attempt for a later daemon tick; a tight retry
            # loop can amplify an external outage and defeats bounded backoff.
            break
    return succeeded


def deliver_outbox(conn: Any = None, *, board: str | None = None,
                   now: str | None = None) -> int:
    """Lease and deliver notifications without holding a Factory transaction."""
    if conn is None:
        from hermes_cli import kanban_db
        conn = kanban_db.connect(board=board)
        try:
            return run_action_intents(
                conn, board=board, kinds={"notification_delivery"}
            )
        finally:
            conn.close()
    return run_action_intents(conn, board=board, kinds={"notification_delivery"})

def reconcile_root_collectors(conn: Any, *, board: str | None = None) -> int:
    """Explicitly finish triage root collectors once every sibling collector is done.

    A root is never left ready simply because kanban dependency propagation saw
    a parent; the selection-scoped key makes restart reconciliation harmless.
    """
    from hermes_cli import kanban_db
    planned = 0
    with store._connect() as db:
        query = (
            "SELECT id,board,root_collector_task_id FROM triage_selections "
            "WHERE root_collector_task_id IS NOT NULL"
        )
        args: tuple[Any, ...] = ()
        if board is not None:
            query += " AND board=?"
            args = (board,)
        rows = [dict(r) for r in db.execute(query, args).fetchall()]
        for selection in rows:
            root = selection["root_collector_task_id"]
            links = conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (root,)).fetchall()
            if not links: continue
            tasks = [kanban_db.get_task(conn, row["parent_id"]) for row in links]
            if not all(task and task.status == "done" for task in tasks): continue
            logical_key = hashlib.sha256(
                f"{selection['id']}|complete_root|siblings_done".encode()
            ).hexdigest()
            before = db.total_changes
            _plan_action(
                db, logical_key=logical_key, kind="triage_root_completion",
                payload={"task_id": root, "summary": "all sibling recipe collectors complete",
                         "board": selection.get("board")},
            )
            planned += int(db.total_changes > before)
    completed = run_action_intents(
        conn, board=board, kinds={"triage_root_completion"}
    )
    return completed

def event(instance_id: str, step_id: str, payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not isinstance(payload.get("type"), str): raise ValueError("event payload requires string id and type")
    with store._connect() as db:
        step = db.execute(
            "SELECT activation,state FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1", (instance_id, step_id),
        ).fetchone()
    return enqueue(
        instance_id, "external_event", {"step_id": step_id, "payload": payload},
        key=hashlib.sha256(f"{instance_id}|{step_id}|{payload['id']}".encode()).hexdigest(),
        expected_activation=int(step["activation"]) if step else None,
        expected_state=step["state"] if step else None,
    )

def gate_decision(
    instance_id: str, step_id: str, decision: str, reason: str = "", *,
    activation: int | None = None, revision_hash: str | None = None,
    evidence_bundle_hash: str | None = None, nonce: str | None = None,
    actor_kind: str = "operator", actor_id: str = "local-operator",
    channel: str = "internal",
) -> str:
    """Persist and queue a human gate decision without applying it.

    Dashboard, CLI, and phone callers supply the complete tuple.  The fallback
    capture exists for trusted in-process compatibility callers; it still
    records the resolved tuple in ``gate_decisions`` before enqueuing.
    """
    from shipfactory.decisions import current_binding, record_decision

    if activation is None or revision_hash is None or nonce is None:
        with store._connect() as db:
            binding = current_binding(db, instance_id, step_id)
        activation = binding["activation"]
        revision_hash = binding["revision_hash"]
        evidence_bundle_hash = binding["evidence_bundle_hash"]
        nonce = nonce or uuid.uuid4().hex
    row = record_decision(
        instance_id=instance_id, step_id=step_id, activation=int(activation),
        revision_hash=revision_hash, evidence_bundle_hash=evidence_bundle_hash,
        nonce=nonce, decision=decision, actor_kind=actor_kind, actor_id=actor_id,
        channel=channel, reason=reason,
    )
    return str(row["advance_event_key"])

def release_review_stall(instance_id: str, step_id: str, reason: str) -> str:
    """Queue an audited release for a recoverable blocked review gate.

    ``clarifications_nonempty`` uses the same enqueue-only operator surface as
    ``review_stall``.  Applying that decision creates a fresh spec producer
    activation; it never treats the blocked approval as permission to proceed.
    """
    if not str(reason).strip():
        raise ValueError("operator release requires a reason")
    with store._connect() as db:
        step = db.execute(
            "SELECT activation,state,primitive,blocked_reason FROM recipe_steps "
            "WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
            (instance_id, step_id),
        ).fetchone()
    recoverable = {"review_stall", "clarifications_nonempty"}
    if (not step or step["primitive"] != "review_gate" or step["state"] != "blocked"
            or step["blocked_reason"] not in recoverable):
        raise ValueError("review step is not parked for operator-recoverable review block")
    return enqueue(
        instance_id,
        "operator_release",
        {"step_id": step_id, "reason": str(reason).strip()},
        expected_activation=int(step["activation"]), expected_state=step["state"],
    )


def _claim_event(*, owner: str, board: str | None) -> dict[str, Any] | None:
    """Lease exactly one pending event under ``BEGIN IMMEDIATE``."""
    store.init_db()
    now = store._now()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE advance_events SET state='pending',lease_owner=NULL,lease_until=NULL,"
            "outcome='lease_expired' WHERE state='leased' AND lease_until<=?",
            (now,),
        )
        query = (
            "SELECT e.* FROM advance_events e LEFT JOIN recipe_instances i ON i.id=e.instance_id "
            "WHERE e.state='pending'"
        )
        args: tuple[Any, ...] = ()
        if board is not None:
            query += " AND i.board=?"
            args = (board,)
        query += " ORDER BY e.created_at,e.key LIMIT 1"
        row = db.execute(query, args).fetchone()
        if row is None:
            return None
        lease_until = (
            datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)
        ).isoformat()
        changed = db.execute(
            "UPDATE advance_events SET state='leased',lease_owner=?,lease_until=?,"
            "attempt_count=attempt_count+1 WHERE key=? AND state='pending'",
            (owner, lease_until, row["key"]),
        ).rowcount
        if changed != 1:
            return None
        value = dict(row)
        value.update({"state": "leased", "lease_owner": owner, "lease_until": lease_until,
                      "attempt_count": int(row["attempt_count"]) + 1})
        return value


def _finish_event(db: Any, row: dict[str, Any], state: str, outcome: str,
                  error: str | None = None) -> None:
    if state not in EVENT_TERMINAL:
        raise ValueError(f"invalid terminal event state {state}")
    changed = db.execute(
        "UPDATE advance_events SET state=?,outcome=?,last_error=?,applied_at=?,"
        "lease_owner=NULL,lease_until=NULL WHERE key=? AND state='leased' AND lease_owner=?",
        (state, outcome, error, store._now(), row["key"], row["lease_owner"]),
    ).rowcount
    if changed != 1:
        raise RuntimeError(f"lost advance-event lease for {row['key']}")


def _matching_step(db: Any, instance_id: str, payload: dict[str, Any],
                   row: dict[str, Any]) -> Any:
    step = db.execute(
        "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
        "ORDER BY activation DESC LIMIT 1", (instance_id, payload.get("step_id")),
    ).fetchone()
    if step is None:
        return None
    if row.get("expected_activation") is not None and int(step["activation"]) != int(row["expected_activation"]):
        return None
    if row.get("expected_state") is not None and step["state"] != row["expected_state"]:
        return None
    return step


def _consume_gate_decision(db: Any, decision_id: str, *, stale_reason: str | None = None) -> None:
    """Mark the persisted statement consumed, retaining any stale explanation."""
    if stale_reason:
        row = db.execute(
            "SELECT reason FROM gate_decisions WHERE id=?", (decision_id,),
        ).fetchone()
        prior = str(row["reason"] or "") if row else ""
        reason = f"stale: {stale_reason}" + (f"; operator reason: {prior}" if prior else "")
        db.execute(
            "UPDATE gate_decisions SET consumed_at=?,reason=? WHERE id=? AND consumed_at IS NULL",
            (store._now(), reason[:2000], decision_id),
        )
    else:
        db.execute(
            "UPDATE gate_decisions SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
            (store._now(), decision_id),
        )


def _bound_gate_step(db: Any, instance_id: str, payload: dict[str, Any],
                     row: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Revalidate the durable decision against current activation/evidence."""
    decision_id = payload.get("decision_id")
    if not isinstance(decision_id, str):
        return None, "persisted gate decision is missing"
    decision = db.execute(
        "SELECT * FROM gate_decisions WHERE id=? AND advance_event_key=?",
        (decision_id, row["key"]),
    ).fetchone()
    if decision is None:
        return None, "persisted gate decision is missing"
    step = db.execute(
        "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
        "ORDER BY activation DESC LIMIT 1",
        (instance_id, decision["step_id"]),
    ).fetchone()
    if step is None or step["primitive"] != "approval_gate" or step["state"] != "waiting":
        return None, "approval gate is no longer waiting"
    if int(step["activation"]) != int(decision["activation"]):
        return None, "activation changed"
    if step["input_revision_hash"] != decision["revision_hash"]:
        return None, "revision hash changed"
    try:
        from shipfactory.decisions import current_binding
        binding = current_binding(db, instance_id, decision["step_id"])
    except Exception as exc:
        return None, f"current binding cannot be verified: {exc}"
    if (binding.get("evidence_bundle_hash") or None) != (
        decision["evidence_bundle_hash"] or None
    ):
        return None, "evidence bundle hash changed"
    if payload.get("decision") != decision["decision"]:
        return None, "event decision differs from persisted decision"
    return step, None


def _apply_claimed_event(conn: Any, row: dict[str, Any]) -> None:
    """Consume one leased event, producing intents but no external commands."""
    try:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("event payload is not an object")
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            leased = db.execute(
                "SELECT 1 FROM advance_events WHERE key=? AND state='leased' AND lease_owner=?",
                (row["key"], row["lease_owner"]),
            ).fetchone()
            if not leased:
                return
            instance = _instance(db, row["instance_id"])
            if not instance:
                _finish_event(db, row, "discarded", "instance_missing")
                return
            if instance["status"] in {"cancelling", "cancelled"}:
                _finish_event(db, row, "discarded", f"instance_{instance['status']}")
                return
            if row["source"] == "gate_decision":
                step, stale_reason = _bound_gate_step(db, instance["id"], payload, row)
                if stale_reason is not None:
                    decision_id = payload.get("decision_id")
                    if isinstance(decision_id, str):
                        _consume_gate_decision(db, decision_id, stale_reason=stale_reason)
                    _finish_event(db, row, "discarded", f"stale_gate_decision:{stale_reason}")
                    return
            else:
                step = _matching_step(db, instance["id"], payload, row)
                if step is None:
                    _finish_event(db, row, "discarded", "stale_or_nonmatching_activation")
                    return
            if row["source"] == "external_event":
                defs = {item["id"]: item for item in recipe_for_instance(instance).document["steps"]}
                definition = defs.get(payload.get("step_id"))
                if (step["state"] != "waiting" or not definition
                        or definition["primitive"] != "wait_for_event"
                        or definition["params"]["event"] != payload.get("payload", {}).get("type")):
                    _finish_event(db, row, "discarded", "event_does_not_match_wait")
                    return
                _transition(db, instance, dict(step), "done", payload["payload"]["id"])
                _finish_event(db, row, "applied", "wait_completed")
                # Resume-note comments are a kanban effect. Commit the Factory
                # event first so no external command runs under its write txn.
                db.commit()
                _consume_resume_note(conn, step["kanban_task_id"])
                return
            if row["source"] == "gate_decision":
                decision_id = str(payload["decision_id"])
                if payload.get("decision") == "approve":
                    logical_key = hashlib.sha256(
                        f"{row['key']}|approval_gate_completion".encode()
                    ).hexdigest()
                    action_key = _plan_action(
                        db, logical_key=logical_key, kind="approval_gate_completion",
                        payload={"task_id": step["kanban_task_id"],
                                 "summary": "operator approved", "board": instance["board"]},
                        instance_id=instance["id"], step_id=step["step_id"],
                        activation=int(step["activation"]),
                    )
                    _consume_gate_decision(db, decision_id)
                    _finish_event(db, row, "applied", f"action_intent:{action_key}")
                    return
                if payload.get("decision") == "reject":
                    reason = str(payload.get("reason") or "operator_rejected")
                    _transition(db, instance, dict(step), "blocked", "operator_rejected", reason=reason)
                    db.execute(
                        "UPDATE recipe_instances SET status='blocked',blocked_reason=?,updated_at=? WHERE id=?",
                        (reason, store._now(), instance["id"]),
                    )
                    _consume_gate_decision(db, decision_id)
                    _finish_event(db, row, "applied", "gate_rejected")
                    return
                _finish_event(db, row, "discarded", "unknown_gate_decision")
                return
            if row["source"] == "operator_release":
                recoverable = {"review_stall", "clarifications_nonempty"}
                if (step["primitive"] != "review_gate" or step["state"] != "blocked"
                        or step["blocked_reason"] not in recoverable):
                    _finish_event(db, row, "discarded", "review_gate_not_recoverable")
                    return
                recipe = recipe_for_instance(instance).document
                blocked_reason = step["blocked_reason"]
                if blocked_reason == "review_stall":
                    from hermes_cli import kanban_db
                    task = kanban_db.get_task(conn, step["kanban_task_id"])
                    verdict = parse_verdict(task.result if task else "")
                    if verdict["outcome"] != "request_changes":
                        raise ValueError("review stall has no rejecting verdict")
                    target = _resolve_target_step(
                        db, instance["id"], recipe, verdict["target_step"],
                    )
                else:
                    definition = next(
                        item for item in recipe["steps"] if item["id"] == step["step_id"]
                    )
                    producers = [
                        item["from"] for item in definition.get("inputs", [])
                        if item.get("kind") == "task-spec" and item.get("required", False)
                    ]
                    if len(producers) != 1:
                        raise ValueError(
                            "clarifications block has no unique task-spec producer"
                        )
                    target = producers[0]
                _invalidate_cone(
                    db, instance, recipe, target,
                    step["step_id"], f"operator_release:{row['key']}",
                )
                _transition(
                    db, instance, dict(step), "blocked", f"operator_release:{row['key']}",
                    reason="changes_requested",
                )
                db.execute(
                    "UPDATE recipe_instances SET status='running',blocked_reason=NULL,updated_at=? WHERE id=?",
                    (store._now(), instance["id"]),
                )
                _finish_event(db, row, "applied", f"{blocked_reason}_released")
                return
            _finish_event(db, row, "discarded", f"unknown_source:{row['source']}")
    except Exception as exc:
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            leased = db.execute(
                "SELECT 1 FROM advance_events WHERE key=? AND state='leased' AND lease_owner=?",
                (row["key"], row["lease_owner"]),
            ).fetchone()
            if leased:
                _finish_event(db, row, "failed", "event_application_failed", str(exc)[:500])

def apply_events(conn: Any, *, profiles: dict[str, dict[str, Any]] | None = None,
                 board: str | None = None,
                 board_day_token_ceiling: int = 10**18,
                 verification_profiles: dict[str, dict[str, Any]] | None = None,
                 environment_config: dict[str, Any] | None = None) -> int:
    """Lease queued events one at a time, consume them, and reconcile instances."""
    count = 0
    owner = f"event:{os.getpid()}:{uuid.uuid4().hex}"
    for _ in range(100):
        row = _claim_event(owner=owner, board=board)
        if row is None:
            break
        _apply_claimed_event(conn, row)
        count += 1
    with store._connect() as db:
        query = (
            "SELECT id FROM recipe_instances WHERE status IN "
            "('running','waiting_gate','waiting_event','blocked','cancelling')"
        )
        args: tuple[Any, ...] = ()
        if board is not None:
            query += " AND board=?"
            args = (board,)
        ids = [r[0] for r in db.execute(query, args).fetchall()]
    run_action_intents(
        conn, board=board,
        kinds={"approval_gate_completion"},
    )
    for ident in ids:
        reconcile(
            conn, ident, profiles=profiles,
            board_day_token_ceiling=board_day_token_ceiling,
            verification_profiles=verification_profiles,
            environment_config=environment_config,
        )
    ran_verification = run_action_intents(
        conn, board=board, kinds={"verification_run"},
    )
    if ran_verification:
        for ident in ids:
            reconcile(
                conn, ident, profiles=profiles,
                board_day_token_ceiling=board_day_token_ceiling,
                verification_profiles=verification_profiles,
                environment_config=environment_config,
            )
    return count

def cancel(conn: Any, instance_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Validate, fence, then atomically archive internal tasks keeping collector blocked."""
    from hermes_cli import kanban_db
    with store._connect() as db:
        instance = _instance(db, instance_id)
        if not instance: raise ValueError("unknown recipe instance")
        steps = _latest(db, instance_id); task_ids = [x["kanban_task_id"] for x in steps if x["kanban_task_id"]]
        report = {"instance_id": instance_id, "workers": [], "nonterminal_steps": [x["step_id"] for x in steps if x["state"] not in TERMINAL], "suppressed": task_ids, "collector": instance["collector_task_id"]}
        if dry_run: return report
        prior_status = instance["status"]
    selected = task_ids + [instance["collector_task_id"]]
    placeholders = ",".join("?" for _ in selected)
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})", selected,
    ).fetchall()
    present = {row["id"] for row in rows}
    missing = [task_id for task_id in selected if task_id not in present]
    if missing:
        refused = f"unknown task id(s): {', '.join(missing)}"
        return {**report, "status": prior_status, "refused": refused}
    # Re-read and fence in one Factory transaction after every validation that
    # can reject the command. A later live-worker refusal is cancellation
    # progress and intentionally leaves this fence in place.
    with store._connect() as db:
        current = _instance(db, instance_id)
        current_steps = _latest(db, instance_id)
        current_ids = [x["kanban_task_id"] for x in current_steps if x["kanban_task_id"]]
        if not current or current["collector_task_id"] != instance["collector_task_id"] or current_ids != task_ids:
            return {**report, "status": prior_status, "refused": "recipe instance changed during cancel validation"}
        db.execute(
            "UPDATE recipe_instances SET status='cancelling',updated_at=? WHERE id=?",
            (store._now(), instance_id),
        )
    # Factory-owned workers have their own process group and durable OS identity.
    from shipfactory.spawn import _process_start_token
    for record in store.nonterminal_runs():
        pid = record.get("pid")
        if (record["task_id"] in task_ids and pid
                and record.get("process_start_token")
                and _process_start_token(int(pid)) == record["process_start_token"]):
            try: os.killpg(int(pid), 15)
            except ProcessLookupError: pass
    result = kanban_db.cancel_subtree(conn, selected, keep_blocked=[instance["collector_task_id"]])
    if result.get("refused"): return {**report, "status": "cancelling", "refused": result["refused"]}
    with store._connect() as db:
        db.execute("UPDATE recipe_steps SET state='cancelled',updated_at=? WHERE instance_id=? AND state NOT IN ('done','skipped','failed')", (store._now(), instance_id)); db.execute("UPDATE recipe_instances SET status='cancelled',blocked_reason='recipe_cancelled',updated_at=? WHERE id=?", (store._now(), instance_id))
    return {**report, "status": "cancelled", "cancel": result}
