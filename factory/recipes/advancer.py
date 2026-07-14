"""Single-writer, idempotent recipe advancement and reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from factory import store
from .instantiate import recipe_for_instance, revision_vector
from .primitives import activate, parse_verdict

TERMINAL = {"done", "skipped", "cancelled", "failed"}

_FINDING_COUNT = re.compile(r"(?im)^\s*(?:finding_count|findings)\s*[:=]\s*(\d+)\s*$")
_FINDING_LINE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:BLOCKER|WARNING)\b")

def advance_key(instance_id: str, recipe_hash: str, step_id: str, activation: int, transition: str, source_id: str) -> str:
    return hashlib.sha256("|".join(map(str, (instance_id, recipe_hash, step_id, activation, transition, source_id))).encode()).hexdigest()

def enqueue(instance_id: str, source: str, payload: dict[str, Any], *, key: str | None = None) -> str:
    """Durably enqueue a hint.  It intentionally performs no flow mutation."""
    store.init_db(); key = key or hashlib.sha256((instance_id + "|" + source + "|" + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
    with store._connect() as db:
        db.execute("INSERT OR IGNORE INTO advance_events(key,instance_id,source,payload_json,state,created_at) VALUES(?,?,?,?, 'pending',?)", (key, instance_id, source, json.dumps(payload, sort_keys=True), store._now()))
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
    if existing and existing["state"] == "applied": return False
    db.execute("INSERT OR IGNORE INTO advance_events(key,instance_id,source,payload_json,state,created_at) VALUES(?,?,?,?, 'pending',?)", (key, instance["id"], source, "{}", store._now()))
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
    db.execute("UPDATE advance_events SET state='applied',applied_at=? WHERE key=?", (store._now(), key))
    return True

def _admit(db: Any, instance: dict[str, Any], recipe: dict[str, Any], step: dict[str, Any], profile: dict[str, Any]) -> str | None:
    allowance = int(profile["token_allowance"]); budgets = recipe["budgets"]; day = datetime.now(timezone.utc).date().isoformat()
    if instance["activation_count"] + 1 > budgets["max_activations"]: return "activation_fuse"
    count = db.execute("SELECT COUNT(*) FROM recipe_steps WHERE instance_id=? AND step_id=? AND primitive IN ('agent_task','review_gate')", (instance["id"], step["step_id"])).fetchone()[0]
    if count > budgets["max_step_activations"]: return "activation_fuse"
    if instance["tokens_charged"] + allowance > budgets["max_tokens"]: return "instance_budget"
    daily = db.execute("SELECT COALESCE(SUM(tokens),0) FROM budget_charges WHERE board=? AND utc_day=?", (instance["board"], day)).fetchone()[0]
    # Config may be absent in direct API tests; a published recipe's max is still enforced.
    ceiling = int((os.environ.get("FACTORY_BOARD_DAY_TOKEN_CEILING") or 10**18))
    if daily + allowance > ceiling: return "board_day_budget"
    charge_key = advance_key(instance["id"], instance["recipe_hash"], step["step_id"], step["activation"], "admit", str(step["activation"]))
    db.execute("INSERT OR IGNORE INTO budget_charges(key,board,utc_day,instance_id,step_id,activation,tokens,created_at) VALUES(?,?,?,?,?,?,?,?)", (charge_key, instance["board"], day, instance["id"], step["step_id"], step["activation"], allowance, store._now()))
    if db.execute("SELECT changes()").fetchone()[0]: db.execute("UPDATE recipe_instances SET activation_count=activation_count+1,tokens_charged=tokens_charged+?,updated_at=? WHERE id=?", (allowance, store._now(), instance["id"]))
    return None

def _summary(db: Any, instance: dict[str, Any]) -> str:
    states = [x["state"] for x in _latest(db, instance["id"])]
    if instance["status"] in {"cancelling", "cancelled", "failed", "done"}: return instance["status"]
    if any(x in {"blocked", "failed"} for x in states): return "blocked"
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
        if in_frontmatter or line.startswith(("#", "```", "FACTORY_VERDICT:")):
            continue
        return line.strip("* ")[:240]
    return None

def _resume_note(db: Any, conn: Any, instance: dict[str, Any], recipe: dict[str, Any],
                 definition: dict[str, Any], task_id: str) -> None:
    """Attach one ephemeral continue-here comment to a newly parked gate."""
    from hermes_cli import kanban_db
    if conn.execute(
        "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'CONTINUE-HERE%' LIMIT 1",
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
    for upstream_id in sorted(ancestors):
        row = db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND state='done' "
            "ORDER BY activation DESC LIMIT 1",
            (instance["id"], upstream_id),
        ).fetchone()
        if row and row["kanban_task_id"]:
            task = kanban_db.get_task(conn, row["kanban_task_id"])
            one_liner = _result_one_liner(task.result if task else None)
            if one_liner:
                summaries.append(f"- {upstream_id}: {one_liner}")
    children = sorted(item["id"] for item in recipe["steps"] if definition["id"] in item["needs"])
    unblocks = ", ".join(children) if children else "recipe completion"
    if definition["primitive"] == "approval_gate":
        awaited = f"Approval required: {definition['params']['instructions']} This unblocks {unblocks}."
        next_action = f"Record approve or reject for instance {instance['id']} step {definition['id']}."
    else:
        awaited = f"Event required: {definition['params']['event']}. This unblocks {unblocks}."
        next_action = f"Emit the matching {definition['params']['event']} event for instance {instance['id']} step {definition['id']}."
    body = "\n".join([
        "CONTINUE-HERE",
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
    kanban_db.add_comment(conn, task_id, "factory", body)

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
        kanban_db.add_comment(conn, task_id, "factory", f"RESUMED {store._now()}")

def _invalidate_cone(db: Any, instance: dict[str, Any], recipe: dict[str, Any], target: str, rejecting_step: str, source: str) -> None:
    """Insert (never overwrite) a new activation cone through a rejecting gate."""
    defs = {x["id"]: x for x in recipe["steps"]}
    if target not in defs or defs[target]["primitive"] != "agent_task":
        raise ValueError("review change target must be an upstream agent_task")
    parents = {item["id"]: set(item["needs"]) for item in recipe["steps"]}
    def upstream(node: str) -> set[str]:
        return parents[node] | set().union(*(upstream(x) for x in parents[node])) if parents[node] else set()
    if target not in upstream(rejecting_step): raise ValueError("review target is not transitive upstream")
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

def reconcile(conn: Any, instance_id: str, *, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Idempotently reconcile every nonterminal activation with kanban/outbox."""
    from hermes_cli import kanban_db
    profiles = profiles or {"standard": {"max_runtime_seconds": 1800, "max_retries": 2, "token_allowance": 50000}}
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
                if step["state"] not in {"running", "waiting"} or not step["kanban_task_id"]:
                    continue
                task = kanban_db.get_task(conn, step["kanban_task_id"])
                if task and task.status == "done":
                    if step["primitive"] == "review_gate":
                        try:
                            verdict = parse_verdict(task.result or "")
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
                                _invalidate_cone(db, instance, recipe, verdict["target_step"], step["step_id"], f"kanban:{task.id}")
                                changed |= _transition(db, instance, step, "blocked", f"kanban:{task.id}", reason="changes_requested")
                                continue
                        except ValueError as exc:
                            changed |= _transition(db, instance, step, "blocked", f"kanban:{task.id}", reason=str(exc))
                            continue
                    if step["primitive"] in {"approval_gate", "wait_for_event"}:
                        _consume_resume_note(conn, step["kanban_task_id"])
                    changed |= _transition(db, instance, step, "done", f"kanban:{task.id}")
                elif task and task.status == "blocked" and step["primitive"] in {"agent_task", "review_gate"}:
                    changed |= _transition(db, instance, step, "blocked", f"kanban:{task.id}", reason="worker_blocked")

            latest = {x["step_id"]: x for x in _latest(db, instance_id)}
            for step_id, step in list(latest.items()):
                if step["state"] != "pending": continue
                if all(latest[parent]["state"] in {"done", "skipped"} for parent in defs[step_id]["needs"]):
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
                if primitive in {"agent_task", "review_gate"}:
                    profile = profiles.get(definition["params"]["execution_profile"])
                    if not profile:
                        changed |= _transition(db, instance, step, "failed", "profile", reason="missing execution profile")
                        continue
                    fuse = _admit(db, instance, recipe, step, profile)
                    if fuse:
                        changed |= _transition(db, instance, step, "blocked", "fuse", reason=fuse)
                        db.execute("UPDATE recipe_instances SET blocked_reason=? WHERE id=?", (fuse, instance_id))
                        continue
                parents = [latest[parent]["kanban_task_id"] for parent in definition["needs"] if latest[parent]["kanban_task_id"]]
                task = activate(conn, instance, recipe, definition, step, params, parents, db=db)
                state = "running" if primitive in {"agent_task", "review_gate"} else "waiting"
                if primitive in {"approval_gate", "wait_for_event"} and task:
                    _resume_note(db, conn, instance, recipe, definition, task)
                changed |= _transition(db, instance, step, state, "activate", task=task)

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

def deliver_outbox(*, now: str | None = None) -> int:
    """Deliver queued notifications with bounded exponential backoff and no model."""
    now = now or store._now(); delivered = 0
    with store._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT 50", (now,)).fetchall()]
        for row in rows:
            try:
                subprocess.run(["hermes", "send", "--to", row["target"], row["message"]], check=True, capture_output=True, text=True, timeout=30)
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                # Bounded backoff; terminal delivery failure remains auditable.
                if attempts >= 8: db.execute("UPDATE outbox SET state='failed',attempts=?,last_error=? WHERE key=?", (attempts, str(exc)[:500], row["key"]))
                else:
                    from datetime import timedelta
                    due = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** attempts))).isoformat()
                    db.execute("UPDATE outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE key=?", (attempts, due, str(exc)[:500], row["key"]))
            else:
                db.execute("UPDATE outbox SET state='delivered',attempts=attempts+1,delivered_at=? WHERE key=?", (store._now(), row["key"])); delivered += 1
    return delivered

def reconcile_root_collectors(conn: Any) -> int:
    """Explicitly finish triage root collectors once every sibling collector is done.

    A root is never left ready simply because kanban dependency propagation saw
    a parent; the selection-scoped key makes restart reconciliation harmless.
    """
    from hermes_cli import kanban_db
    completed = 0
    with store._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT id,root_collector_task_id FROM triage_selections WHERE root_collector_task_id IS NOT NULL").fetchall()]
        for selection in rows:
            root = selection["root_collector_task_id"]
            links = conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (root,)).fetchall()
            if not links: continue
            tasks = [kanban_db.get_task(conn, row["parent_id"]) for row in links]
            if not all(task and task.status == "done" for task in tasks): continue
            key = hashlib.sha256(f"{selection['id']}|complete_root|siblings_done".encode()).hexdigest()
            if db.execute("SELECT 1 FROM advance_events WHERE key=? AND state='applied'", (key,)).fetchone(): continue
            db.execute("INSERT OR REPLACE INTO advance_events(key,instance_id,source,payload_json,state,created_at,applied_at) VALUES(?,?,?,'{}','applied',?,?)", (key, None, "root_collector", store._now(), store._now()))
            kanban_db.complete_task(conn, root, summary="all sibling recipe collectors complete"); completed += 1
    return completed

def event(instance_id: str, step_id: str, payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not isinstance(payload.get("type"), str): raise ValueError("event payload requires string id and type")
    return enqueue(instance_id, "external_event", {"step_id": step_id, "payload": payload}, key=hashlib.sha256(f"{instance_id}|{step_id}|{payload['id']}".encode()).hexdigest())

def gate_decision(instance_id: str, step_id: str, decision: str, reason: str = "") -> str:
    """Queue a human gate decision for the recipe engine's single writer."""
    if decision not in {"approve", "reject"}:
        raise ValueError("gate decision must be approve or reject")
    return enqueue(
        instance_id,
        "gate_decision",
        {"step_id": step_id, "decision": decision, "reason": reason},
    )

def release_review_stall(instance_id: str, step_id: str, reason: str) -> str:
    """Queue an audited operator release for a parked review-stall gate."""
    if not str(reason).strip():
        raise ValueError("operator release requires a reason")
    return enqueue(
        instance_id,
        "operator_release",
        {"step_id": step_id, "reason": str(reason).strip()},
    )

def apply_events(conn: Any, *, profiles: dict[str, dict[str, Any]] | None = None) -> int:
    """Claim queued events, apply only matching waits, then reconcile all instances."""
    from hermes_cli import kanban_db
    count = 0
    with store._connect() as db:
        events = [dict(r) for r in db.execute("SELECT * FROM advance_events WHERE state='pending' ORDER BY created_at LIMIT 100").fetchall()]
        for row in events:
            payload = json.loads(row["payload_json"]); instance = _instance(db, row["instance_id"])
            if not instance or instance["status"] in {"cancelling", "cancelled"}: db.execute("UPDATE advance_events SET state='applied',applied_at=? WHERE key=?", (store._now(), row["key"])); continue
            if row["source"] == "external_event":
                step = db.execute("SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1", (instance["id"], payload["step_id"])).fetchone()
                defs = {x["id"]: x for x in recipe_for_instance(instance).document["steps"]}
                if step and step["state"] == "waiting" and defs[payload["step_id"]]["primitive"] == "wait_for_event" and defs[payload["step_id"]]["params"]["event"] == payload["payload"]["type"]:
                    _consume_resume_note(conn, step["kanban_task_id"])
                    _transition(db, instance, dict(step), "done", payload["payload"]["id"])
            elif row["source"] == "gate_decision":
                step = db.execute("SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1", (instance["id"], payload.get("step_id"))).fetchone()
                if step and step["primitive"] == "approval_gate" and step["state"] == "waiting":
                    if payload.get("decision") == "approve":
                        # The blocked kanban task is the canonical approval
                        # signal; reconciliation observes its completion.
                        kanban_db.complete_task(conn, step["kanban_task_id"], summary="operator approved")
                    elif payload.get("decision") == "reject":
                        reason = str(payload.get("reason") or "operator_rejected")
                        _transition(db, instance, dict(step), "blocked", "operator_rejected", reason=reason)
                        db.execute("UPDATE recipe_instances SET status='blocked',blocked_reason=?,updated_at=? WHERE id=?", (reason, store._now(), instance["id"]))
            elif row["source"] == "operator_release":
                step = db.execute(
                    "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
                    (instance["id"], payload.get("step_id")),
                ).fetchone()
                if (step and step["primitive"] == "review_gate" and step["state"] == "blocked"
                        and step["blocked_reason"] == "review_stall"):
                    task = kanban_db.get_task(conn, step["kanban_task_id"])
                    verdict = parse_verdict(task.result if task else "")
                    if verdict["outcome"] != "request_changes":
                        raise ValueError("review stall has no rejecting verdict")
                    recipe = recipe_for_instance(instance).document
                    _invalidate_cone(
                        db, instance, recipe, verdict["target_step"], step["step_id"],
                        f"operator_release:{row['key']}",
                    )
                    _transition(
                        db, instance, dict(step), "blocked", f"operator_release:{row['key']}",
                        reason="changes_requested",
                    )
                    db.execute(
                        "UPDATE recipe_instances SET status='running',blocked_reason=NULL,updated_at=? WHERE id=?",
                        (store._now(), instance["id"]),
                    )
            db.execute("UPDATE advance_events SET state='applied',applied_at=? WHERE key=?", (store._now(), row["key"])); count += 1
        ids = [r[0] for r in db.execute("SELECT id FROM recipe_instances WHERE status NOT IN ('done','failed','cancelled')").fetchall()]; db.commit()
    for ident in ids: reconcile(conn, ident, profiles=profiles)
    return count

def cancel(conn: Any, instance_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Fence, terminate processes, then atomically archive internal tasks keeping collector blocked."""
    from hermes_cli import kanban_db
    with store._connect() as db:
        instance = _instance(db, instance_id)
        if not instance: raise ValueError("unknown recipe instance")
        steps = _latest(db, instance_id); task_ids = [x["kanban_task_id"] for x in steps if x["kanban_task_id"]]
        report = {"instance_id": instance_id, "workers": [], "nonterminal_steps": [x["step_id"] for x in steps if x["state"] not in TERMINAL], "suppressed": task_ids, "collector": instance["collector_task_id"]}
        if dry_run: return report
        db.execute("UPDATE recipe_instances SET status='cancelling',updated_at=? WHERE id=?", (store._now(), instance_id)); db.commit()
    # Factory-owned workers have their own process group and can be signalled by reap records.
    from factory.spawn import _RUNNING
    for record in list(_RUNNING.values()):
        if record["task_id"] in task_ids:
            try: os.killpg(record["proc"].pid, 15)
            except ProcessLookupError: pass
    result = kanban_db.cancel_subtree(conn, task_ids + [instance["collector_task_id"]], keep_blocked=[instance["collector_task_id"]])
    if result.get("refused"): return {**report, "status": "cancelling", "refused": result["refused"]}
    with store._connect() as db:
        db.execute("UPDATE recipe_steps SET state='cancelled',updated_at=? WHERE instance_id=? AND state NOT IN ('done','skipped','failed')", (store._now(), instance_id)); db.execute("UPDATE recipe_instances SET status='cancelled',blocked_reason='recipe_cancelled',updated_at=? WHERE id=?", (store._now(), instance_id))
    return {**report, "status": "cancelled", "cancel": result}
