"""Human-only, revision-bound approval decisions and phone action tokens.

This module never applies a gate.  A successful decision transaction records
the operator's statement and enqueues one ``advance_events`` row; the daemon is
still the only recipe-state writer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from shipfactory import store


MAX_PHONE_TOKEN_SECONDS = 10 * 60
_TOKEN_SCHEMA = "shipfactory.gate-action/v1"


class DecisionConflict(ValueError):
    """The submitted decision no longer names the current waiting revision."""


class DecisionTokenError(ValueError):
    """A phone action token is malformed, forged, or expired."""


def _required_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(_required_text("nonce", nonce).encode("utf-8")).hexdigest()


def _current_evidence(db: Any, instance_id: str) -> tuple[str | None, str | None]:
    row = db.execute(
        "SELECT id,bundle_sha256 FROM evidence_bundles "
        "WHERE instance_id=? AND state='done' AND bundle_sha256 IS NOT NULL "
        "ORDER BY activation DESC,sealed_at DESC,id DESC LIMIT 1",
        (instance_id,),
    ).fetchone()
    if row is None:
        return None, None
    # A DB hash alone is not authority. Re-read and hash-bind the sealed bundle
    # so replacement between notification and click becomes a conflict.
    from shipfactory.verification import verify_evidence_bundle

    try:
        verified = verify_evidence_bundle(row["id"], db=db)
    except Exception as exc:
        raise DecisionConflict(f"current evidence bundle cannot be verified: {exc}") from exc
    return str(verified["id"]), str(verified["bundle_sha256"])


def current_binding(db: Any, instance_id: str, step_id: str) -> dict[str, Any]:
    """Return the exact tuple currently eligible for a human decision."""
    instance = db.execute(
        "SELECT * FROM recipe_instances WHERE id=?", (instance_id,),
    ).fetchone()
    step = db.execute(
        "SELECT step_id,activation,primitive,state,input_revision_hash FROM recipe_steps "
        "WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1",
        (instance_id, step_id),
    ).fetchone()
    if instance is None or step is None:
        raise DecisionConflict("unknown approval gate")
    if step["primitive"] != "approval_gate" or step["state"] != "waiting":
        raise DecisionConflict("approval gate is not waiting")
    revision = step["input_revision_hash"]
    if not isinstance(revision, str) or not revision:
        raise DecisionConflict("approval gate has no current revision binding")
    try:
        from shipfactory.recipes.instantiate import recipe_for_instance
        recipe = recipe_for_instance(dict(instance), db=db).document
    except Exception as exc:
        raise DecisionConflict(
            f"approval gate recipe policy cannot be verified: {exc}"
        ) from exc
    details: dict[str, Any] = {
        "task_spec_sha256": None, "plan_sha256": None,
        "change_set_sha256": None, "review_story_sha256": None,
        "candidate_commit_sha": None, "candidate_tree_sha": None,
    }
    if recipe.get("schema") != "shipfactory.recipe/v2":
        # Legacy gates predate typed input declarations and revision-vector
        # rederivation. Preserve their original evidence-bound behavior.
        evidence_id, evidence_hash = _current_evidence(db, instance_id)
    else:
        try:
            definition = next(item for item in recipe["steps"] if item["id"] == step_id)
            from shipfactory.artifacts import artifact_document, input_artifacts, rederive_change_set
            from shipfactory.recipes.instantiate import revision_vector

            resolved = input_artifacts(db, instance_id, definition)
            recomputed = revision_vector(db, instance_id, dict(step), recipe)
        except Exception as exc:
            raise DecisionConflict(f"approval gate inputs cannot be verified: {exc}") from exc
        if recomputed != revision:
            raise DecisionConflict("approval gate revision binding is stale")

        evidence_id = evidence_hash = None
        for item in resolved:
            kind = item["kind"]
            if kind == "evidence-bundle":
                evidence_id, evidence_hash = str(item["id"]), str(item["sha256"])
                continue
            if kind == "task-spec":
                details["task_spec_sha256"] = str(item["sha256"])
            elif kind == "plan":
                details["plan_sha256"] = str(item["sha256"])
            elif kind == "review-story":
                details["review_story_sha256"] = str(item["sha256"])
            elif kind == "change-set":
                details["change_set_sha256"] = str(item["sha256"])
                document = artifact_document(item)
                details["candidate_commit_sha"] = document["head_sha"]
                details["candidate_tree_sha"] = document["tree_sha"]
                run = db.execute(
                    "SELECT workspace_path FROM runs WHERE id=?", (item.get("run_id"),),
                ).fetchone()
                if run is None or not run["workspace_path"]:
                    raise DecisionConflict("approval change-set producer workspace is unavailable")
                try:
                    live = rederive_change_set(
                        run["workspace_path"], base_sha=document["base_sha"],
                        allowed_paths=document["allowed_paths"],
                    )
                except Exception as exc:
                    raise DecisionConflict(
                        f"approval candidate commit/tree cannot be verified: {exc}"
                    ) from exc
                if live != document:
                    raise DecisionConflict("approval candidate change-set changed after review")
    return {
        "instance_id": instance_id,
        "step_id": step_id,
        "activation": int(step["activation"]),
        "revision_hash": revision,
        "evidence_bundle_id": evidence_id,
        "evidence_bundle_hash": evidence_hash,
        "policy_hash": str(instance["recipe_hash"]),
        **details,
    }


def _same_tuple(row: Any, submitted: dict[str, Any], decision: str) -> bool:
    return all((row[key] or None) == (submitted.get(key) or None) for key in (
        "instance_id", "step_id", "evidence_bundle_hash",
    )) and int(row["activation"]) == int(submitted["activation"]) and (
        row["revision_hash"] == submitted["revision_hash"]
        and row["decision"] == decision
    )


def _assert_current(current: dict[str, Any], submitted: dict[str, Any]) -> None:
    for key in (
        "instance_id", "step_id", "activation", "revision_hash",
        "evidence_bundle_hash",
    ):
        expected = current.get(key) or None
        actual = submitted.get(key) or None
        if key == "activation":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != expected:
            raise DecisionConflict(
                f"stale gate decision: {key} does not match current state"
            )


def record_decision(
    *, instance_id: str, step_id: str, activation: int,
    revision_hash: str, evidence_bundle_hash: str | None, nonce: str,
    decision: str, actor_kind: str, actor_id: str, channel: str,
    reason: str = "",
) -> dict[str, Any]:
    """Atomically persist a bound operator decision and its queued event.

    A replay of the exact nonce/tuple is a no-op returning the first durable
    row. Reusing a nonce for any different tuple is a conflict.
    """
    if decision not in {"approve", "reject"}:
        raise ValueError("gate decision must be approve or reject")
    actor_kind = _required_text("actor_kind", actor_kind)
    if actor_kind not in {"operator", "human"}:
        raise ValueError("gate decisions require a human operator actor_kind")
    actor_id = _required_text("actor_id", actor_id)
    channel = _required_text("channel", channel)
    revision_hash = _required_text("revision_hash", revision_hash)
    nonce_digest = _nonce_hash(nonce)
    submitted = {
        "instance_id": _required_text("instance", instance_id),
        "step_id": _required_text("step", step_id),
        "activation": int(activation),
        "revision_hash": revision_hash,
        "evidence_bundle_hash": evidence_bundle_hash or None,
    }
    store.init_db()
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        replay = db.execute(
            "SELECT * FROM gate_decisions WHERE nonce_hash=? ORDER BY created_at,id LIMIT 1",
            (nonce_digest,),
        ).fetchone()
        if replay is not None:
            if _same_tuple(replay, submitted, decision):
                return dict(replay) | {"replayed": True}
            raise DecisionConflict("nonce was already used for a different decision")

        latest = db.execute(
            "SELECT activation FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "ORDER BY activation DESC LIMIT 1",
            (submitted["instance_id"], submitted["step_id"]),
        ).fetchone()
        if latest is not None and int(latest["activation"]) != submitted["activation"]:
            raise DecisionConflict("stale gate decision: activation does not match current state")
        current = current_binding(db, submitted["instance_id"], submitted["step_id"])
        _assert_current(current, submitted)
        already_bound = db.execute(
            "SELECT * FROM gate_decisions WHERE instance_id=? AND step_id=? AND activation=? "
            "AND revision_hash=? AND COALESCE(evidence_bundle_hash,'')=COALESCE(?,'') "
            "ORDER BY created_at,id LIMIT 1",
            (
                submitted["instance_id"], submitted["step_id"], submitted["activation"],
                revision_hash, submitted["evidence_bundle_hash"],
            ),
        ).fetchone()
        if already_bound is not None:
            if already_bound["decision"] == decision:
                return dict(already_bound) | {"replayed": True}
            raise DecisionConflict("a different decision is already recorded for this gate tuple")
        ident = uuid.uuid4().hex
        event_key = hashlib.sha256(f"gate-decision|{ident}".encode()).hexdigest()
        payload = {
            "decision_id": ident,
            "step_id": submitted["step_id"],
            "decision": decision,
            "reason": str(reason or ""),
            "activation": submitted["activation"],
            "revision_hash": revision_hash,
            "evidence_bundle_hash": submitted["evidence_bundle_hash"],
        }
        now = store._now()
        db.execute(
            "INSERT INTO gate_decisions"
            "(id,instance_id,step_id,activation,revision_hash,evidence_bundle_id,"
            "evidence_bundle_hash,actor_kind,actor_id,channel,decision,reason,nonce_hash,"
            "policy_hash,created_at,advance_event_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident, submitted["instance_id"], submitted["step_id"],
                submitted["activation"], revision_hash,
                current["evidence_bundle_id"], submitted["evidence_bundle_hash"],
                actor_kind, actor_id, channel, decision, str(reason or "") or None,
                nonce_digest, current["policy_hash"], now, event_key,
            ),
        )
        db.execute(
            "INSERT INTO advance_events"
            "(key,instance_id,source,payload_json,state,created_at,expected_activation,expected_state) "
            "VALUES(?,?, 'gate_decision',?,'pending',?,?,'waiting')",
            (
                event_key, submitted["instance_id"],
                json.dumps(payload, sort_keys=True), now, submitted["activation"],
            ),
        )
        row = db.execute("SELECT * FROM gate_decisions WHERE id=?", (ident,)).fetchone()
        return dict(row) | {"replayed": False}


def _key_path() -> Path:
    return store._db_path().parent / "keys" / "gate-decision-hmac.key"


def _signing_key() -> bytes:
    """Create/read the Factory-only signing key without following symlinks."""
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    created = False
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DecisionTokenError("decision signing key is not a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise DecisionTokenError("decision signing key must have mode 0600")
        if created:
            data = os.urandom(32)
            os.write(fd, data)
            os.fsync(fd)
        else:
            data = os.read(fd, 4096)
        if len(data) < 32:
            raise DecisionTokenError("decision signing key is too short")
        return data
    finally:
        os.close(fd)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise DecisionTokenError("malformed decision token") from exc


def issue_phone_token(
    *, instance_id: str, step_id: str, activation: int,
    revision_hash: str, evidence_bundle_hash: str | None, decision: str,
    nonce: str | None = None, ttl_seconds: int = MAX_PHONE_TOKEN_SECONDS,
    now: int | None = None,
) -> str:
    """Issue a signed action token only for the current waiting gate tuple."""
    if decision not in {"approve", "reject"}:
        raise ValueError("gate decision must be approve or reject")
    ttl = int(ttl_seconds)
    if ttl < 1 or ttl > MAX_PHONE_TOKEN_SECONDS:
        raise ValueError("phone decision token lifetime must be 1..600 seconds")
    store.init_db()
    submitted = {
        "instance_id": instance_id, "step_id": step_id, "activation": int(activation),
        "revision_hash": revision_hash,
        "evidence_bundle_hash": evidence_bundle_hash or None,
    }
    with store._connect() as db:
        current = current_binding(db, instance_id, step_id)
        _assert_current(current, submitted)
    issued = int(time.time() if now is None else now)
    payload = {
        "schema": _TOKEN_SCHEMA, "instance": instance_id, "step": step_id,
        "activation": int(activation), "revision_hash": revision_hash,
        "evidence_bundle_hash": evidence_bundle_hash or None,
        "nonce": nonce or uuid.uuid4().hex, "decision": decision,
        "issued_at": issued, "expires_at": issued + ttl,
        "policy_hash": current["policy_hash"],
        "task_spec_sha256": current["task_spec_sha256"],
        "plan_sha256": current["plan_sha256"],
        "change_set_sha256": current["change_set_sha256"],
        "review_story_sha256": current["review_story_sha256"],
        "candidate_commit_sha": current["candidate_commit_sha"],
        "candidate_tree_sha": current["candidate_tree_sha"],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_signing_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def consume_phone_token(
    token: str, *, actor_kind: str, actor_id: str, channel: str = "telegram",
    reason: str = "", now: int | None = None,
) -> dict[str, Any]:
    """Authenticate a phone click and enqueue its human decision once."""
    try:
        encoded, encoded_signature = token.split(".", 1)
    except (AttributeError, ValueError) as exc:
        raise DecisionTokenError("malformed decision token") from exc
    raw = _unb64(encoded)
    signature = _unb64(encoded_signature)
    expected = hmac.new(_signing_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise DecisionTokenError("decision token signature is invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecisionTokenError("malformed decision token payload") from exc
    required = {
        "schema", "instance", "step", "activation", "revision_hash",
        "evidence_bundle_hash", "nonce", "decision", "issued_at", "expires_at",
        "policy_hash", "task_spec_sha256", "plan_sha256", "change_set_sha256",
        "review_story_sha256", "candidate_commit_sha", "candidate_tree_sha",
    }
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema") != _TOKEN_SCHEMA:
        raise DecisionTokenError("malformed decision token payload")
    current_time = int(time.time() if now is None else now)
    try:
        issued = int(payload["issued_at"])
        expires = int(payload["expires_at"])
    except (TypeError, ValueError) as exc:
        raise DecisionTokenError("malformed decision token lifetime") from exc
    if expires <= issued or expires - issued > MAX_PHONE_TOKEN_SECONDS:
        raise DecisionTokenError("decision token lifetime exceeds ten minutes")
    if current_time < issued:
        raise DecisionTokenError("decision token is not yet valid")
    if current_time > expires:
        raise DecisionTokenError("decision token has expired")
    return record_decision(
        instance_id=payload["instance"], step_id=payload["step"],
        activation=payload["activation"], revision_hash=payload["revision_hash"],
        evidence_bundle_hash=payload["evidence_bundle_hash"], nonce=payload["nonce"],
        decision=payload["decision"], actor_kind=actor_kind, actor_id=actor_id,
        channel=channel, reason=reason,
    )


__all__ = [
    "DecisionConflict", "DecisionTokenError", "MAX_PHONE_TOKEN_SECONDS",
    "consume_phone_token", "current_binding", "issue_phone_token", "record_decision",
]
