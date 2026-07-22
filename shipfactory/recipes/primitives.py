"""Recipe primitive activation rules and strict review verdict parsing."""
from __future__ import annotations

import base64
import json
import re
import hashlib
import subprocess
from pathlib import Path
from typing import Any

from shipfactory import store
from shipfactory.policy import citation_ok
from .instantiate import task_key

_VERDICT = re.compile(r"^SHIPFACTORY_VERDICT:\s*(\{.*\})\s*$")
VERDICT_CONTRACT_V2 = "shipfactory.verdict/v2"
# A v2 finding location is one strict file.ext:line(-line) citation. The
# extension is any short alphanumeric suffix rather than an allowlist:
# finding #72 (first-light) — a reviewer legitimately cited message.txt:1,
# the exact file under change, and the enumerated-extension gate rejected
# the verdict. The gate's job is forcing one concrete repository location,
# not curating file types.
_FINDING_LOCATION = re.compile(
    r"[A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,8}:\d+(?:-\d+)?"
)


def _upstream_ids(recipe: dict[str, Any], step_id: str) -> list[str]:
    defs = {item["id"]: item for item in recipe["steps"]}
    found: set[str] = set()

    def visit(node: str) -> None:
        for parent in defs[node].get("needs", []):
            if parent not in found:
                found.add(parent)
                visit(parent)

    visit(step_id)
    return [item["id"] for item in recipe["steps"] if item["id"] in found]


def build_review_input_context(
    db: Any, instance: dict[str, Any], recipe: dict[str, Any], step_def: dict[str, Any],
) -> tuple[str, str]:
    """Open and reverify every transitive sealed input for a review task."""
    from shipfactory.artifacts import artifact_document
    from shipfactory.verification import (
        _canonical, _evidence_root, assert_commit_binding, verify_evidence_bundle,
    )

    upstream = _upstream_ids(recipe, step_def["id"])
    snapshot: dict[str, Any] = {
        "schema": "shipfactory.review-input/v1",
        "instance_id": instance["id"], "review_step_id": step_def["id"],
        "input_artifact_set_hash": None,
        "artifacts": [], "evidence_bundles": [], "exact_diff": None,
    }
    current_step = db.execute(
        "SELECT input_artifact_set_hash FROM recipe_steps WHERE instance_id=? "
        "AND step_id=? ORDER BY activation DESC LIMIT 1",
        (instance["id"], step_def["id"]),
    ).fetchone()
    if current_step is not None:
        snapshot["input_artifact_set_hash"] = current_step["input_artifact_set_hash"]
    artifacts_by_kind: dict[str, dict[str, Any]] = {}
    artifact_kinds = sorted({"task-spec", "plan"} | {
        item["kind"] for node in [step_def["id"], *upstream]
        for item in next(
            definition for definition in recipe["steps"] if definition["id"] == node
        ).get("inputs", [])
        if item.get("kind") != "evidence-bundle" and item.get("from") in upstream
    })
    for kind in artifact_kinds:
        if not upstream:
            continue
        placeholders = ",".join("?" for _ in upstream)
        row = db.execute(
            f"SELECT * FROM artifacts WHERE instance_id=? AND step_id IN ({placeholders}) "
            "AND kind=? AND state='sealed' ORDER BY activation DESC,sealed_at DESC LIMIT 1",
            (instance["id"], *upstream, kind),
        ).fetchone()
        if row is None:
            continue
        artifact = dict(row)
        document = artifact_document(artifact)
        sealed_bytes = Path(artifact["sealed_path"]).read_bytes()
        snapshot["artifacts"].append({
            "id": artifact["id"], "kind": kind, "sha256": artifact["sha256"],
            "activation": int(artifact["activation"]), "document": document,
            "sealed_bytes_b64": base64.b64encode(sealed_bytes).decode("ascii"),
            "sealed_size_bytes": len(sealed_bytes),
        })
        artifacts_by_kind[kind] = artifact
    if {"task-spec", "plan"} <= set(artifacts_by_kind):
        plan_document = artifact_document(artifacts_by_kind["plan"])
        if plan_document.get("task_spec_sha256") != artifacts_by_kind["task-spec"]["sha256"]:
            raise ValueError("review plan is not bound to the selected sealed task-spec")
    for producer_id in upstream:
        definition = next(item for item in recipe["steps"] if item["id"] == producer_id)
        if definition["primitive"] != "verification":
            continue
        rows = db.execute(
            "SELECT * FROM evidence_bundles WHERE instance_id=? AND step_id=? "
            "AND sealed_at IS NOT NULL ORDER BY activation,sealed_at",
            (instance["id"], producer_id),
        ).fetchall()
        if not rows:
            raise ValueError(f"review evidence is missing for {producer_id}")
        for row in rows:
            bundle = verify_evidence_bundle(row["id"], db=db)
            bundle_path = _evidence_root(
                bundle["instance_id"], bundle["step_id"], int(bundle["activation"]),
            ) / "bundle.json"
            sealed_bytes = bundle_path.read_bytes()
            snapshot["evidence_bundles"].append({
                "id": bundle["id"], "activation": int(bundle["activation"]),
                "sha256": bundle["bundle_sha256"],
                "sealed_bytes_b64": base64.b64encode(sealed_bytes).decode("ascii"),
                "sealed_size_bytes": len(sealed_bytes),
                "document": json.loads(sealed_bytes),
            })
    change_inputs = [
        item for node in [step_def["id"], *upstream]
        for item in next(
            definition for definition in recipe["steps"] if definition["id"] == node
        ).get("inputs", [])
        if item.get("kind") == "change-set" and item.get("from") in upstream
    ]
    if change_inputs:
        producer_id = change_inputs[0]["from"]
        producer_step = db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1",
            (instance["id"], producer_id),
        ).fetchone()
        if (producer_step is None or not producer_step["kanban_task_id"]
                or producer_step["producer_run_id"] is None):
            raise ValueError("review change-set exact producer task/run identity is missing")
        activation = int(producer_step["activation"])
        run_id = int(producer_step["producer_run_id"])
        run_row = db.execute(
            "SELECT * FROM runs WHERE id=? AND task_id=? AND recipe_activation=?",
            (run_id, str(producer_step["kanban_task_id"]), activation),
        ).fetchone()
        run = dict(run_row) if run_row is not None else None
        if run is None or not run.get("workspace_path"):
            raise ValueError("review exact producer run/workspace is missing")
        if not snapshot["evidence_bundles"]:
            raise ValueError("review change-set has no transitive verification identity")
        verified = snapshot["evidence_bundles"][-1]["document"]
        workspace = Path(run["workspace_path"])
        assert_commit_binding(workspace, verified["head_sha"], verified["tree_sha"])
        diff_bytes = subprocess.check_output(
            ["git", "diff", "--binary", verified["base_sha"], verified["head_sha"]],
            cwd=workspace, stderr=subprocess.PIPE, timeout=30,
        )
        snapshot["exact_diff"] = {
            "producer_step_id": producer_id, "producer_task_id": producer_step["kanban_task_id"],
            "producer_activation": activation, "producer_run_id": run_id,
            "workspace_path": str(workspace.resolve()),
            "base_sha": verified["base_sha"], "head_sha": verified["head_sha"],
            "tree_sha": verified["tree_sha"],
            "sha256": hashlib.sha256(diff_bytes).hexdigest(), "size_bytes": len(diff_bytes),
            "bytes_b64": base64.b64encode(diff_bytes).decode("ascii"),
        }
    raw = _canonical(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    body = (
        "\n\n## Factory-sealed review inputs\n"
        "The JSON below was opened and integrity-checked by Factory immediately before task creation.\n"
        f"SHIPFACTORY_REVIEW_INPUT_SHA256: {digest}\n"
        "```json\n" + raw.decode("utf-8") + "\n```"
    )
    return body, digest

def _verdict_payload(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    match = _VERDICT.fullmatch(lines[-1]) if lines else None
    if not match: raise ValueError("review final line must be SHIPFACTORY_VERDICT JSON")
    try: verdict = json.loads(match.group(1))
    except json.JSONDecodeError as exc: raise ValueError("invalid SHIPFACTORY_VERDICT JSON") from exc
    if not isinstance(verdict, dict) or verdict.get("outcome") not in {"approve", "request_changes"} or not isinstance(verdict.get("body"), str) or not citation_ok(verdict["body"]): raise ValueError("invalid review verdict")
    return verdict


def parse_verdict(text: str) -> dict[str, Any]:
    verdict = _verdict_payload(text)
    if verdict["outcome"] == "approve" and set(verdict) != {"outcome", "body"}: raise ValueError("approve verdict has unknown fields")
    if verdict["outcome"] == "request_changes" and (set(verdict) != {"outcome", "target_step", "body"} or not isinstance(verdict.get("target_step"), str)): raise ValueError("invalid request_changes verdict")
    return verdict


def _v2_error(detail: str) -> ValueError:
    """Return a v2 contract error under one stable machine-matchable prefix."""
    return ValueError(f"verdict_contract: {detail}")


def parse_verdict_v2(
    text: str, recipe: dict[str, Any], step_def: dict[str, Any],
) -> dict[str, Any]:
    """Parse the structured fail-closed shipfactory.verdict/v2 sentinel."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    match = _VERDICT.fullmatch(lines[-1]) if lines else None
    if not match: raise _v2_error("review final line must be SHIPFACTORY_VERDICT JSON")
    try: verdict = json.loads(match.group(1))
    except json.JSONDecodeError as exc: raise _v2_error("invalid SHIPFACTORY_VERDICT JSON") from exc
    if not isinstance(verdict, dict): raise _v2_error("verdict must be a JSON object")
    outcome = verdict.get("outcome")
    if outcome not in {"approve", "request_changes"}: raise _v2_error("outcome must be approve or request_changes")
    expected = {"schema", "outcome", "clean", "findings", "summary"}
    if outcome == "request_changes":
        expected = expected | {"target_step"}
    if set(verdict) != expected: raise _v2_error("verdict keys must exactly match the v2 schema")
    if verdict["schema"] != VERDICT_CONTRACT_V2: raise _v2_error(f"schema must be {VERDICT_CONTRACT_V2}")
    if not isinstance(verdict["clean"], bool) or verdict["clean"] != (outcome == "approve"): raise _v2_error("clean must be a bool equal to outcome==approve")
    summary = verdict["summary"]
    if not isinstance(summary, str) or not summary.strip(): raise _v2_error("summary must be a nonempty string")
    findings = verdict["findings"]
    if not isinstance(findings, list): raise _v2_error("findings must be a list")
    if outcome == "approve" and findings: raise _v2_error("approve requires an empty findings list")
    if outcome == "request_changes" and not findings: raise _v2_error("request_changes requires at least one finding")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"severity", "location", "summary"}: raise _v2_error("finding keys must be exactly severity, location, summary")
        if finding["severity"] not in {"blocker", "warning"}: raise _v2_error("finding severity must be blocker or warning")
        if not isinstance(finding["summary"], str) or not finding["summary"].strip(): raise _v2_error("finding summary must be a nonempty string")
        if not isinstance(finding["location"], str) or not _FINDING_LOCATION.fullmatch(finding["location"]): raise _v2_error("finding location must cite one file.ext:line repository location")
    if outcome == "request_changes":
        target = verdict["target_step"]
        if not isinstance(target, str) or not target.strip(): raise _v2_error("target_step must be a nonempty string")
        # Kanban task ids (t_...) stay accepted: the advancer maps them to a
        # step id via _resolve_target_step, and an unknown id still fails
        # closed in _invalidate_cone (finding #25b).  Only a named recipe
        # step outside the Factory-owned target set is rejected here.
        allowed = review_verdict_targets(recipe, step_def)
        if any(step["id"] == target for step in recipe["steps"]) and target not in allowed:
            raise _v2_error("target_step is not a legal rework target")
    # Downstream consumers (resume notes, finding-count fallback) read
    # verdict["body"]; synthesize it from the structured fields.
    body_lines = [summary.strip()] + [
        f"{finding['severity'].upper()} {finding['location']} — {finding['summary'].strip()}"
        for finding in findings
    ]
    return {**verdict, "body": "\n".join(body_lines)}


def review_verdict_targets(
    recipe: dict[str, Any], step_def: dict[str, Any],
) -> list[str]:
    """Return Factory-owned legal rework targets for one v2 review."""
    defs = {item["id"]: item for item in recipe["steps"]}
    change_set_targets = list(dict.fromkeys(
        item["from"] for item in step_def.get("inputs", [])
        if item.get("kind") == "change-set"
    ))
    declared_agent_targets = list(dict.fromkeys(
        item["from"] for item in step_def.get("inputs", [])
        if item.get("from") in defs and defs[item["from"]]["primitive"] == "agent_task"
    ))
    allowed = change_set_targets or declared_agent_targets or [
        step_id for step_id in _upstream_ids(recipe, step_def["id"])
        if defs[step_id]["primitive"] == "agent_task"
    ]
    if not allowed:
        raise ValueError(f"review gate {step_def['id']} has no valid request_changes target")
    return allowed


def parse_verdict_for_review(
    text: str, recipe: dict[str, Any], step_def: dict[str, Any],
) -> dict[str, Any]:
    """Parse a verdict, deriving only an omitted, unambiguous Factory target."""
    if recipe.get("verdict_contract") == VERDICT_CONTRACT_V2:
        return parse_verdict_v2(text, recipe, step_def)
    try:
        return parse_verdict(text)
    except ValueError as exc:
        if str(exc) != "invalid request_changes verdict":
            raise
        verdict = _verdict_payload(text)
        allowed = review_verdict_targets(recipe, step_def)
        if (verdict.get("outcome") != "request_changes"
                or set(verdict) != {"outcome", "body"} or len(allowed) != 1):
            raise exc
        return {**verdict, "target_step": allowed[0]}


def _review_verdict_contract(recipe: dict[str, Any], step_def: dict[str, Any]) -> str:
    """Render the exact parser contract and valid rework targets for one v2 review."""
    allowed = review_verdict_targets(recipe, step_def)
    if recipe.get("verdict_contract") == VERDICT_CONTRACT_V2:
        approve = json.dumps({
            "schema": VERDICT_CONTRACT_V2, "outcome": "approve", "clean": True,
            "findings": [], "summary": "Clean pass; no findings.",
        }, separators=(",", ":"))
        request_changes = json.dumps({
            "schema": VERDICT_CONTRACT_V2, "outcome": "request_changes", "clean": False,
            "target_step": allowed[0],
            "findings": [{
                "severity": "blocker", "location": "path/to/file.py:1",
                "summary": "describe the concrete finding",
            }],
            "summary": "One blocker must be fixed before approval.",
        }, separators=(",", ":"))
        return (
            "\n\n## Factory review verdict contract (shipfactory.verdict/v2)\n"
            "Emit exactly one of these parser-valid forms as a single line before the mandatory "
            "SHIPFACTORY_RESULT line. Do not wrap it in a Markdown fence. Do not emit prose instead "
            "of this JSON.\n"
            f"- Approve: `SHIPFACTORY_VERDICT: {approve}`\n"
            f"- Request changes: `SHIPFACTORY_VERDICT: {request_changes}`\n"
            f"Allowed request_changes target_step values: {', '.join(allowed)}. "
            "Use the exact recipe step id, not a title or invented name.\n"
            "Rules: schema must be shipfactory.verdict/v2; clean is true exactly when the outcome "
            "is approve; approve requires findings to be []; request_changes requires target_step "
            "plus at least one finding. Every finding carries exactly severity (blocker or "
            "warning), location (one concrete `path/to/file.py:1` or `path/to/file.py:1-9` "
            "repository citation), and a nonempty summary. Keep the verdict JSON on one physical "
            "line."
        )
    approve = json.dumps({
        "outcome": "approve",
        "body": "APPROVE - clean pass; no findings.",
    }, separators=(",", ":"))
    request_changes = json.dumps({
        "outcome": "request_changes",
        "target_step": allowed[0],
        "body": "path/to/file.py:1 - describe the concrete finding",
    }, separators=(",", ":"))
    return (
        "\n\n## Factory review verdict contract\n"
        "Emit exactly one of these parser-valid forms as a single line before the mandatory "
        "SHIPFACTORY_RESULT line. Do not wrap it in a Markdown fence. Do not emit prose instead "
        "of this JSON.\n"
        f"- Approve: `SHIPFACTORY_VERDICT: {approve}`\n"
        f"- Request changes: `SHIPFACTORY_VERDICT: {request_changes}`\n"
        f"Allowed request_changes target_step values: {', '.join(allowed)}. "
        "Use the exact recipe step id, not a title or invented name.\n"
        "An approve body must contain APPROVE plus an explicit clean-pass/no-findings phrase. "
        "A request_changes body must cite at least one concrete `path/to/file.py:1`-style "
        "repository location. Keep the verdict JSON on one physical line."
    )

def activate(conn: Any, instance: dict[str, Any], recipe: dict[str, Any], step_def: dict[str, Any], step: dict[str, Any], parameters: dict[str, Any], parents: list[str], db: Any = None, body_suffix: str = "") -> str | None:
    """Perform one idempotent primitive mutation and return its task id if any."""
    from hermes_cli import kanban_db
    primitive, params = step_def["primitive"], step_def["params"]
    def render(value: Any) -> Any:
        if not isinstance(value, str): return value
        for name, val in parameters.items(): value = value.replace("${" + name + "}", "" if val is None else str(val))
        return value
    key = task_key(instance["id"], instance["recipe_hash"], step["step_id"], step["activation"])
    title, body = render(step_def["title"]), render(params.get("instructions", params.get("message", "")))
    if (primitive in {"agent_task", "review_gate"} and db is not None
            and recipe.get("schema") == "shipfactory.recipe/v2"
            and step_def.get("inputs")):
        review_context, _digest = build_review_input_context(db, instance, recipe, step_def)
        body += review_context
    if (primitive in {"agent_task", "review_gate"}
            and recipe.get("schema") == "shipfactory.recipe/v2"
            and step_def.get("outputs")):
        from shipfactory.artifact_contracts import artifact_output_contract

        lines = ["\n\n## Factory output contract"]
        for output in step_def["outputs"]:
            kind, schema, path = output["kind"], output["schema"], output["path"]
            if kind == "change-set":
                lines.append(
                    f"- `{kind}` (`{schema}`) at `{path}`: Factory generates this "
                    "artifact after your successful exit. Do not write or modify this path."
                )
            else:
                lines.append(
                    f"- Write the artifact `{kind}` to the exact relative path `{path}`. "
                    f"Its JSON must validate as `{schema}`."
                )
                lines.extend(("", artifact_output_contract(schema)))
        lines.append(
            "Create the parent `.shipfactory-output/` directory when needed. "
            "A chat response is not an artifact; the file must exist before you report success."
        )
        body += "\n".join(lines)
    if primitive == "review_gate" and recipe.get("schema") == "shipfactory.recipe/v2":
        body += _review_verdict_contract(recipe, step_def)
    if body_suffix:
        # Rejection context from a step with no kanban task to inherit
        # (verification rework feedback, finding #95).
        body += body_suffix
    if primitive in {"agent_task", "review_gate"}:
        # board= must be explicit: create_task's default_workdir inheritance
        # falls back to get_current_board() (the GLOBAL current board), which
        # poisons workspace_path with another board's workdir when the factory
        # board isn't current (shakedown finding #11).
        return kanban_db.create_task(conn, title=title, body=body, assignee=render(params["seat"]), workspace_kind=render(params["workspace"]), board=instance.get("board"), parents=parents, idempotency_key=key, max_runtime_seconds=int(params.get("max_runtime_seconds", 1800)), max_retries=int(params.get("max_retries", 2)))
    if primitive == "approval_gate":
        return kanban_db.create_blocked_task(conn, title=title, body=body, parents=parents, idempotency_key=key, board=instance.get("board"), block_kind="needs_input", reason="approval_required")
    if primitive == "wait_for_event":
        return kanban_db.create_blocked_task(conn, title=title, body=f"Waiting for event: {render(params['event'])}", parents=parents, idempotency_key=key, board=instance.get("board"), block_kind="needs_input", reason="event_wait")
    if primitive == "notify":
        # Reuse the caller's open factory-db handle when provided — opening a
        # second connection here deadlocks against reconcile()'s held write
        # txn on the same file (shakedown finding #17: 'database is locked').
        if db is not None:
            db.execute("INSERT OR IGNORE INTO outbox(key,target,message,state,attempts,next_attempt_at) VALUES(?,?,?,'pending',0,?)", (key, render(params["target"]), body, store._now()))
            from .advancer import _plan_action
            _plan_action(
                db, logical_key=key, kind="notification_delivery",
                payload={"outbox_key": key, "board": instance.get("board")},
                instance_id=instance["id"], step_id=step["step_id"],
                activation=int(step["activation"]),
            )
        else:
            with store._connect() as fresh:
                fresh.execute("INSERT OR IGNORE INTO outbox(key,target,message,state,attempts,next_attempt_at) VALUES(?,?,?,'pending',0,?)", (key, render(params["target"]), body, store._now()))
                from .advancer import _plan_action
                _plan_action(
                    fresh, logical_key=key, kind="notification_delivery",
                    payload={"outbox_key": key, "board": instance.get("board")},
                    instance_id=instance["id"], step_id=step["step_id"],
                    activation=int(step["activation"]),
                )
        return None
    if primitive == "verification":
        # The advancer journals this non-model action directly; it has no
        # kanban task, seat, executor, or model activation.
        return None
    raise RuntimeError(f"unknown primitive {primitive}")


__all__ = ["parse_verdict", "activate", "build_review_input_context"]
