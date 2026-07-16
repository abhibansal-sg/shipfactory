"""SF-11 durable decision binding and phone-action adversarial cases."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from shipfactory import decisions, store, verification
from shipfactory.recipes import advancer
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import load_library
from shipfactory.spawn import _worker_environment


PROFILES = {"standard": {"max_runtime_seconds": 10, "max_retries": 1, "token_allowance": 1}}
GIT_ENV = {
    **os.environ, "GIT_AUTHOR_NAME": "SF11 Test", "GIT_AUTHOR_EMAIL": "sf11@example.invalid",
    "GIT_COMMITTER_NAME": "SF11 Test", "GIT_COMMITTER_EMAIL": "sf11@example.invalid",
}


def _gate(tmp_path: Path, conn, instance_id: str = "bound") -> dict:
    library = tmp_path / f"recipes-{instance_id}"
    library.mkdir()
    (library / "gate.yaml").write_text(f"""schema: shipfactory.recipe/v1
id: gate-{instance_id}
version: 1
status: active
description: bound gate fixture
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 2, max_tokens: 10}}
steps:
  - id: approve
    primitive: approval_gate
    title: Approve
    needs: []
    optional: false
    params: {{approvers: [operator], instructions: approve}}
""", encoding="utf-8")
    recipe = load_library(library).get(f"gate-{instance_id}@1")
    instantiate(conn, board="test", recipe=recipe, parameters={}, instance_id=instance_id)
    advancer.reconcile(conn, instance_id, profiles=PROFILES)
    with store._connect() as db:
        return decisions.current_binding(db, instance_id, "approve")


def _token(binding: dict, *, nonce: str = "phone-nonce", decision: str = "approve") -> str:
    return decisions.issue_phone_token(
        instance_id=binding["instance_id"], step_id=binding["step_id"],
        activation=binding["activation"], revision_hash=binding["revision_hash"],
        evidence_bundle_hash=binding["evidence_bundle_hash"], decision=decision,
        nonce=nonce,
    )


def _new_activation(instance_id: str, revision: str = "f" * 64) -> None:
    with store._connect() as db:
        now = store._now()
        db.execute(
            "INSERT INTO recipe_steps"
            "(instance_id,step_id,activation,primitive,state,input_revision_hash,created_at,updated_at) "
            "VALUES(?, 'approve',2,'approval_gate','waiting',?,?,?)",
            (instance_id, revision, now, now),
        )


def _evidence(tmp_path: Path, instance_id: str, revision: str) -> dict:
    repo = tmp_path / f"evidence-{instance_id}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".shipfactory").mkdir()
    (repo / ".shipfactory" / "verification.yaml").write_text(
        """schema: shipfactory.verification/v1
cases:
  - id: approval-flow
    requirement_ids: [REQ-1]
    driver: command
    argv: [python3, -c, "print('ok')"]
    oracle: {type: exit_code, equals: 0}
capture: {video: false, trace: false, screenshots: on-failure}
""", encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "evidence"], cwd=repo, env=GIT_ENV, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    manifest = verification.load_verification_manifest(repo, head)
    bundle_id = verification._bundle_id(instance_id, "verify", 1)
    verification._insert_bundle(
        bundle_id=bundle_id, instance_id=instance_id, step_id="verify", activation=1,
        input_revision_hash=revision, base_sha=head, head_sha=head, tree_sha=tree,
        environment_session_id=None, manifest=manifest,
    )
    now = store._now()
    verification._record_case(
        bundle_id=bundle_id, case_id="approval-flow", attempt=1,
        case={"requirement_ids": ["REQ-1"], "oracle": {"type": "exit_code", "equals": 0}},
        status="passed", item_ids=[], started_at=now, ended_at=now,
    )
    return verification._seal_bundle(
        bundle_id, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["approval-flow"],
    )


def test_old_phone_link_after_rework_conflicts_without_event(tmp_path, kanban_conn):
    binding = _gate(tmp_path, kanban_conn, "old-link")
    token = _token(binding)
    _new_activation("old-link")
    with pytest.raises(decisions.DecisionConflict, match="activation"):
        decisions.consume_phone_token(
            token, actor_kind="operator", actor_id="alice", channel="telegram",
        )
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE source='gate_decision'").fetchone()[0] == 0


def test_cross_instance_phone_link_tamper_fails_signature(tmp_path, kanban_conn):
    binding = _gate(tmp_path, kanban_conn, "signed-a")
    token = _token(binding)
    encoded, signature = token.split(".", 1)
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    payload = json.loads(raw)
    payload["instance"] = "signed-b"
    tampered = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode() + "." + signature
    with pytest.raises(decisions.DecisionTokenError, match="signature"):
        decisions.consume_phone_token(
            tampered, actor_kind="operator", actor_id="alice", channel="telegram",
        )


def test_phone_nonce_replay_returns_recorded_decision_and_one_event(tmp_path, kanban_conn):
    binding = _gate(tmp_path, kanban_conn, "replay")
    token = _token(binding)
    first = decisions.consume_phone_token(
        token, actor_kind="operator", actor_id="alice", channel="telegram",
    )
    second = decisions.consume_phone_token(
        token, actor_kind="operator", actor_id="alice", channel="telegram",
    )
    assert first["id"] == second["id"] and second["replayed"] is True
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE source='gate_decision'").fetchone()[0] == 1
        actor = db.execute("SELECT actor_kind,actor_id,channel FROM gate_decisions").fetchone()
        assert tuple(actor) == ("operator", "alice", "telegram")


def test_bundle_replaced_after_notification_conflicts_before_click(tmp_path, kanban_conn):
    binding = _gate(tmp_path, kanban_conn, "bundle-swap")
    bundle = _evidence(tmp_path, "bundle-swap", binding["revision_hash"])
    with store._connect() as db:
        current = decisions.current_binding(db, "bundle-swap", "approve")
    token = _token(current, nonce="bundle-swap-nonce")
    root = store._db_path().parent / "runs" / "bundle-swap" / "verify" / "1" / "evidence"
    path = root / "bundle.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace('"phase_b_eligible":true', '"phase_b_eligible":false'),
        encoding="utf-8",
    )
    with pytest.raises(decisions.DecisionConflict, match="cannot be verified"):
        decisions.consume_phone_token(
            token, actor_kind="operator", actor_id="alice", channel="telegram",
        )
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 0
    assert bundle["bundle_sha256"] == current["evidence_bundle_hash"]


def test_valid_activation_one_decision_is_stale_when_two_waits_at_tick(tmp_path, kanban_conn):
    binding = _gate(tmp_path, kanban_conn, "tick-stale")
    row = decisions.record_decision(
        instance_id="tick-stale", step_id="approve", activation=1,
        revision_hash=binding["revision_hash"], evidence_bundle_hash=None,
        nonce="tick-nonce", decision="approve", actor_kind="operator",
        actor_id="alice", channel="dashboard",
    )
    _new_activation("tick-stale")
    advancer.apply_events(kanban_conn, profiles=PROFILES, board="test")
    with store._connect() as db:
        event = db.execute(
            "SELECT state,outcome FROM advance_events WHERE key=?",
            (row["advance_event_key"],),
        ).fetchone()
        persisted = db.execute(
            "SELECT consumed_at,reason FROM gate_decisions WHERE id=?", (row["id"],),
        ).fetchone()
    assert event["state"] == "discarded" and "activation changed" in event["outcome"]
    assert persisted["consumed_at"] and persisted["reason"].startswith("stale:")


def test_phone_key_is_0600_expiring_and_excluded_from_worker_environment(
    tmp_path, kanban_conn, monkeypatch,
):
    binding = _gate(tmp_path, kanban_conn, "key-isolation")
    with pytest.raises(ValueError, match="1..600"):
        decisions.issue_phone_token(
            instance_id="key-isolation", step_id="approve", activation=1,
            revision_hash=binding["revision_hash"], evidence_bundle_hash=None,
            decision="approve", ttl_seconds=601,
        )
    token = _token(binding)
    key_path = store._db_path().parent / "keys" / "gate-decision-hmac.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    monkeypatch.setenv("SHIPFACTORY_DECISION_KEY", str(key_path))
    real_home = os.environ["HERMES_HOME"]
    env = _worker_environment(tmp_path / "worker", board="test", task_id="task")
    assert "SHIPFACTORY_DECISION_KEY" not in env
    assert env["HERMES_HOME"] != real_home
    assert str(key_path) not in env.values()
    with pytest.raises(decisions.DecisionTokenError, match="expired"):
        decisions.consume_phone_token(
            token, actor_kind="operator", actor_id="alice", now=10**12,
        )


def test_gate_decision_migration_is_normative_sql():
    store.init_db()
    with store._connect() as db:
        columns = [row["name"] for row in db.execute("PRAGMA table_info(gate_decisions)")]
        version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        indexes = [row["name"] for row in db.execute("PRAGMA index_list(gate_decisions)")]
    assert version == 11
    assert columns == [
        "id", "instance_id", "step_id", "activation", "revision_hash",
        "evidence_bundle_id", "evidence_bundle_hash", "actor_kind", "actor_id",
        "channel", "decision", "reason", "nonce_hash", "policy_hash", "created_at",
        "consumed_at", "advance_event_key",
    ]
    assert indexes  # includes the normative UNIQUE advance_event_key auto-index
