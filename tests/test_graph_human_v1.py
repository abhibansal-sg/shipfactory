from __future__ import annotations

import json
import sqlite3

import pytest

from shipfactory import decisions
from shipfactory import graph_runner
from shipfactory import store
from shipfactory.graph_recipe import validate


RECIPE = validate({
    "name": "human-box-recipe",
    "start": "start",
    "boxes": [
        {"id": "start", "name": "Start", "who": "worker", "instructions": "Work."},
        {"id": "approve", "name": "Approve", "who": "human", "instructions": "Decide."},
        {"id": "end", "name": "End", "who": "worker", "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "start", "result": "done", "to": ["approve"]},
        {"from": "approve", "result": "approved", "to": ["end"]},
        {"from": "approve", "result": "rejected", "to": ["start"]},
    ],
})

NOT_HUMAN_RECIPE = validate({
    "name": "not-human-box-recipe",
    "start": "start",
    "boxes": [
        {"id": "start", "name": "Start", "who": "worker", "instructions": "Work.", "end": True},
    ],
    "arrows": [],
})


def _create_run(*, run_id: str = "run", launch_key: str = "launch", recipe=RECIPE):
    return store.create_recipe_run_v1(
        run_id=run_id,
        project_id="project",
        board="board",
        recipe_name=recipe.name,
        recipe_hash=recipe.hash,
        recipe_snapshot_json=recipe.canonical_json,
        request_text="request",
        workspace_path="/workspace",
        launch_key=launch_key,
    )


def _seed_route_token(*, token_id="seed", run_id="run", box_id="approve"):
    now = store._now()
    work_refs = json.dumps({"request": "request", "preceding_outputs": []})
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id FROM route_tokens_v1 "
            "WHERE run_id=? AND source_attempt_id IS NULL "
            "AND arrow_index IS NULL AND destination_box_id=?",
            (run_id, box_id),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        db.execute(
            "INSERT INTO route_tokens_v1"
            "(id,run_id,source_attempt_id,arrow_index,destination_box_id,"
            "lineage_json,work_refs_json,state,created_at,consumed_at) "
            "VALUES(?,?,NULL,NULL,?,'[]',?,'consumed',?,?)",
            (token_id, run_id, box_id, work_refs, now, now),
        )
        return token_id


def _insert_attempt(*, attempt_id="human", run_id="run", box_id="approve", state="waiting_human", ordinal=1):
    token_id = _seed_route_token(token_id=f"seed-{run_id}-{box_id}", run_id=run_id, box_id=box_id)
    return store.insert_box_attempt_v1(
        attempt_id=attempt_id, run_id=run_id, box_id=box_id, ordinal=ordinal,
        state=state,
        input_work={
            "activating_token_ids": [token_id],
            "request": "request",
            "preceding_outputs": [],
        },
    )


def test_missing_attempt_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()

    with pytest.raises(decisions.DecisionConflict, match="waiting_human"):
        decisions.enqueue_human_box_decision(
            attempt_id="missing", result="approved", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )


def test_wrong_state_attempt_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt(state="running")

    with pytest.raises(decisions.DecisionConflict, match="waiting_human"):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="approved", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )


def test_non_human_frozen_box_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run(run_id="run2", launch_key="launch2", recipe=NOT_HUMAN_RECIPE)
    _insert_attempt(attempt_id="worker-attempt", run_id="run2", box_id="start", state="waiting_human")

    with pytest.raises(decisions.DecisionConflict, match="who: human"):
        decisions.enqueue_human_box_decision(
            attempt_id="worker-attempt", result="approved", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )


def test_undeclared_result_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    with pytest.raises(decisions.DecisionConflict, match="outgoing arrow"):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="not-a-declared-result", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )


def test_non_human_actor_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    with pytest.raises(decisions.DecisionConflict, match="actor_kind"):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="approved", actor_kind="robot",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )


def test_identical_nonce_replay_returns_prior_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    first = decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )
    assert first["replayed"] is False

    second = decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )
    assert second["replayed"] is True
    assert second["id"] == first["id"]
    assert second["event_key"] == first["event_key"]

    with store._connect() as db:
        rows = store._rows(db.execute("SELECT * FROM human_box_decisions_v1"))
        events = store._rows(db.execute("SELECT * FROM run_events_v1"))
    assert len(rows) == 1
    assert len(events) == 1


def test_nonce_conflict_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt(attempt_id="human-a", box_id="approve", ordinal=1)
    _insert_attempt(attempt_id="human-b", box_id="approve", run_id="run", ordinal=2)
    store.update_box_attempt_v1(
        "human-b", expected_state="waiting_human", state="waiting_human",
    )

    decisions.enqueue_human_box_decision(
        attempt_id="human-a", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="shared-nonce",
    )

    with pytest.raises(decisions.DecisionConflict, match="conflicting"):
        decisions.enqueue_human_box_decision(
            attempt_id="human-b", result="rejected", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="shared-nonce",
        )


def test_attempt_conflict_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )

    with pytest.raises(decisions.DecisionConflict, match="conflicting"):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="rejected", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-2",
        )


def test_atomic_rollback_on_event_insertion_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    original_enqueue = store.enqueue_run_event_v1

    def _boom(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated failure")

    monkeypatch.setattr(store, "enqueue_run_event_v1", _boom)

    with pytest.raises(
        decisions.DecisionConflict, match="event could not be enqueued"
    ):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="approved", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )

    monkeypatch.setattr(store, "enqueue_run_event_v1", original_enqueue)

    with store._connect() as db:
        decision_rows = store._rows(db.execute("SELECT * FROM human_box_decisions_v1"))
        event_rows = store._rows(db.execute("SELECT * FROM run_events_v1"))
        attempt = store._rows(db.execute("SELECT * FROM box_attempts_v1 WHERE id='human'"))
    assert decision_rows == []
    assert event_rows == []
    assert attempt[0]["state"] == "waiting_human"


def test_decision_cannot_mark_attempt_or_run_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )

    attempt = store.get_recipe_run_v1("run")
    assert attempt["state"] != "completed"
    with store._connect() as db:
        box_attempt = store._rows(
            db.execute("SELECT * FROM box_attempts_v1 WHERE id='human'")
        )[0]
    assert box_attempt["state"] == "waiting_human"
    assert box_attempt["result"] is None
    assert box_attempt["finished_at"] is None


def test_replay_returns_prior_decision_after_graphrunner_consumed_event(tmp_path, monkeypatch):
    """A replay of the exact tuple must return the prior durable decision even
    after GraphRunner has applied the box_completed event and the attempt is
    no longer waiting_human. The prior durable decision is already
    authority/recipe validated, so replay must not re-run the waiting-state
    gate."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    first = decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )
    assert first["replayed"] is False

    summary = graph_runner.apply_events(owner="test-owner")
    assert summary["applied"] == 1

    with store._connect() as db:
        consumed_attempt = store._rows(
            db.execute("SELECT * FROM box_attempts_v1 WHERE id='human'")
        )[0]
    assert consumed_attempt["state"] != "waiting_human"

    second = decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )
    assert second["replayed"] is True
    assert second["id"] == first["id"]
    assert second["event_key"] == first["event_key"]

    with store._connect() as db:
        rows = store._rows(db.execute("SELECT * FROM human_box_decisions_v1"))
        events = store._rows(db.execute("SELECT * FROM run_events_v1"))
    assert len(rows) == 1
    assert len(events) == 1


def test_conflicting_replay_still_fails_closed_after_event_consumed(tmp_path, monkeypatch):
    """A same-attempt or same-nonce conflict must still fail closed even once
    the attempt is no longer waiting_human."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )
    graph_runner.apply_events(owner="test-owner")

    with pytest.raises(decisions.DecisionConflict, match="conflicting"):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="rejected", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-2",
        )


def test_reverse_atomic_rollback_on_decision_recording_failure(tmp_path, monkeypatch):
    """If enqueue_run_event_v1 succeeds but record_human_box_decision_v1 then
    raises, both the event and the decision must be rolled back and the
    attempt must be unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    original_record = store.record_human_box_decision_v1

    def _boom(*args, **kwargs):
        raise store.GraphStoreIntegrityError("simulated failure")

    monkeypatch.setattr(store, "record_human_box_decision_v1", _boom)

    with pytest.raises(decisions.DecisionConflict, match="could not be recorded"):
        decisions.enqueue_human_box_decision(
            attempt_id="human", result="approved", actor_kind="human",
            actor_id="operator", channel="dashboard", nonce="nonce-1",
        )

    monkeypatch.setattr(store, "record_human_box_decision_v1", original_record)

    with store._connect() as db:
        decision_rows = store._rows(db.execute("SELECT * FROM human_box_decisions_v1"))
        event_rows = store._rows(db.execute("SELECT * FROM run_events_v1"))
        attempt = store._rows(db.execute("SELECT * FROM box_attempts_v1 WHERE id='human'"))
    assert decision_rows == []
    assert event_rows == []
    assert attempt[0]["state"] == "waiting_human"


def test_event_payload_is_exact_and_apply_events_advances_attempt(tmp_path, monkeypatch):
    """Assert the exact event source and strict payload fields/type/
    expected_state/work/run/attempt/result, the event-key binding, and prove
    apply_events accepts it: the human attempt leaves waiting_human with the
    selected result."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    _create_run()
    _insert_attempt()

    result = decisions.enqueue_human_box_decision(
        attempt_id="human", result="approved", actor_kind="human",
        actor_id="operator", channel="dashboard", nonce="nonce-1",
    )
    assert result["event_key"] == result["event_key"]

    with store._connect() as db:
        event = store._rows(
            db.execute("SELECT * FROM run_events_v1 WHERE key=?", (result["event_key"],))
        )[0]
    assert event["source"] == "human_decision"
    assert event["run_id"] == "run"
    payload = json.loads(event["payload_json"])
    assert set(payload) == {
        "type", "run_id", "attempt_id", "expected_state", "result", "work",
    }
    assert payload["type"] == "box_completed"
    assert payload["run_id"] == "run"
    assert payload["attempt_id"] == "human"
    assert payload["expected_state"] == "waiting_human"
    assert payload["result"] == "approved"
    assert payload["work"] == "Human decision: approved"

    summary = graph_runner.apply_events(owner="test-owner")
    assert summary["applied"] == 1
    assert summary["discarded"] == 0
    assert summary["failed"] == 0

    with store._connect() as db:
        attempt = store._rows(
            db.execute("SELECT * FROM box_attempts_v1 WHERE id='human'")
        )[0]
    assert attempt["state"] != "waiting_human"
    assert attempt["result"] == "approved"
