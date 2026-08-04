from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
from threading import Barrier

import pytest

from shipfactory import store
from shipfactory import graph_runner
from shipfactory.graph_recipe import validate
from shipfactory.graph_runner import (
    apply_events,
    enqueue_event,
    reconcile_run,
    start_run,
)


RECIPE = validate({
    "name": "task-6-runtime-recipe",
    "start": "runtime-start",
    "boxes": [
        {
            "id": "runtime-start",
            "name": "Runtime start",
            "who": "worker",
            "instructions": "Use the runtime request.",
        },
        {
            "id": "runtime-end",
            "name": "Runtime end",
            "who": "worker",
            "instructions": "Finish.",
            "end": True,
        },
    ],
    "arrows": [
        {"from": "runtime-start", "result": "done", "to": ["runtime-end"]},
    ],
})

V2_RECIPE = validate({
    "version": 2,
    "name": "non-executable-v2",
    "start": "build",
    "capability_sets": {
        "coding": {"skills": [], "toolsets": [], "plugins": []},
    },
    "boxes": [{
        "id": "build",
        "name": "Build",
        "who": "worker",
        "instructions": "Build.",
        "workspace": {"lane": "build", "access": "write"},
        "capabilities": "coding",
        "end": True,
    }],
    "arrows": [],
})

CONFLICTING_RECIPE = validate({
    "name": "task-6-conflicting-recipe",
    "start": "conflicting-start",
    "boxes": [
        {
            "id": "conflicting-start",
            "name": "Conflicting start",
            "who": "worker",
            "instructions": "Use a different frozen recipe.",
            "end": True,
        },
    ],
    "arrows": [],
})

FAN_OUT_RECIPE = validate({
    "name": "task-7-fan-out-recipe",
    "start": "fan-out-start",
    "boxes": [
        {
            "id": "fan-out-start",
            "name": "Fan-out start",
            "who": "worker",
            "instructions": "Produce work for both destinations.",
        },
        {
            "id": "left",
            "name": "Left",
            "who": "worker",
            "instructions": "Handle the left path.",
        },
        {
            "id": "right",
            "name": "Right",
            "who": "worker",
            "instructions": "Handle the right path.",
        },
        {
            "id": "fan-out-end",
            "name": "Fan-out end",
            "who": "worker",
            "instructions": "Finish.",
            "end": True,
        },
    ],
    "arrows": [
        {
            "from": "fan-out-start",
            "result": "done",
            "to": ["left", "right"],
        },
        {"from": "left", "result": "done", "to": ["fan-out-end"]},
        {"from": "right", "result": "done", "to": ["fan-out-end"]},
    ],
})

THREE_WAY_JOIN_RECIPE = validate({
    "name": "task-8-three-way-join-recipe",
    "start": "split",
    "boxes": [
        {
            "id": "split",
            "name": "Split",
            "who": "worker",
            "instructions": "Produce work for three destinations.",
        },
        {
            "id": "alpha",
            "name": "Alpha",
            "who": "worker",
            "instructions": "Handle alpha.",
        },
        {
            "id": "beta",
            "name": "Beta",
            "who": "worker",
            "instructions": "Handle beta.",
        },
        {
            "id": "gamma",
            "name": "Gamma",
            "who": "worker",
            "instructions": "Handle gamma.",
        },
        {
            "id": "synthesis",
            "name": "Synthesis",
            "who": "worker",
            "instructions": "Merge all active branch work.",
        },
        {
            "id": "end",
            "name": "End",
            "who": "worker",
            "instructions": "Finish.",
            "end": True,
        },
    ],
    "arrows": [
        {"from": "split", "result": "done", "to": ["alpha", "beta", "gamma"]},
        {"from": "alpha", "result": "done", "to": ["synthesis"]},
        {"from": "beta", "result": "done", "to": ["synthesis"]},
        {"from": "gamma", "result": "done", "to": ["synthesis"]},
        {"from": "synthesis", "result": "done", "to": ["end"]},
    ],
})

CONDITIONAL_JOIN_RECIPE = validate({
    "name": "task-8-conditional-join-recipe",
    "start": "split",
    "boxes": [
        {
            "id": "split",
            "name": "Split",
            "who": "worker",
            "instructions": "Produce three conditional branches.",
        },
        {
            "id": "left",
            "name": "Left",
            "who": "worker",
            "instructions": "Handle left.",
        },
        {
            "id": "middle",
            "name": "Middle",
            "who": "worker",
            "instructions": "Handle middle.",
        },
        {
            "id": "chooser",
            "name": "Chooser",
            "who": "worker",
            "instructions": "Choose synthesis or detour.",
        },
        {
            "id": "detour",
            "name": "Detour",
            "who": "worker",
            "instructions": "Continue away from synthesis.",
        },
        {
            "id": "synthesis",
            "name": "Synthesis",
            "who": "worker",
            "instructions": "Merge active synthesis branches.",
        },
        {
            "id": "end",
            "name": "End",
            "who": "worker",
            "instructions": "Finish.",
            "end": True,
        },
    ],
    "arrows": [
        {"from": "split", "result": "done", "to": ["left", "middle", "chooser"]},
        {"from": "left", "result": "done", "to": ["synthesis"]},
        {"from": "middle", "result": "done", "to": ["synthesis"]},
        {"from": "chooser", "result": "join", "to": ["synthesis"]},
        {"from": "chooser", "result": "away", "to": ["detour"]},
        {"from": "detour", "result": "done", "to": ["end"]},
        {"from": "synthesis", "result": "done", "to": ["end"]},
    ],
})

BACKWARD_REACHABILITY_RECIPE = validate({
    "name": "task-8-backward-reachability-recipe",
    "start": "split",
    "boxes": [
        {"id": "split", "name": "Split", "who": "worker",
         "instructions": "Create two paths."},
        {"id": "left", "name": "Left", "who": "worker",
         "instructions": "Produce the active join input."},
        {"id": "chooser", "name": "Chooser", "who": "worker",
         "instructions": "Choose the join or the later path."},
        {"id": "synthesis", "name": "Synthesis", "who": "worker",
         "instructions": "Continue with the active arrivals."},
        {"id": "later", "name": "Later", "who": "worker",
         "instructions": "Finish or traverse a backward correction arrow."},
        {"id": "end", "name": "End", "who": "worker",
         "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "split", "result": "done", "to": ["left", "chooser"]},
        {"from": "left", "result": "done", "to": ["synthesis"]},
        {"from": "chooser", "result": "join", "to": ["synthesis"]},
        {"from": "chooser", "result": "away", "to": ["later"]},
        {"from": "synthesis", "result": "done", "to": ["end"]},
        {"from": "later", "result": "rewind", "to": ["chooser"]},
        {"from": "later", "result": "done", "to": ["end"]},
    ],
})

REWORK_LIMIT_RECIPE = validate({
    "name": "task-9-rework-limit-recipe",
    "start": "draft",
    "boxes": [
        {"id": "draft", "name": "Draft", "who": "worker",
         "instructions": "Produce a draft."},
        {"id": "review", "name": "Review", "who": "worker",
         "instructions": "Accept or revise the draft."},
        {"id": "polish", "name": "Polish", "who": "worker",
         "instructions": "Polish the accepted draft."},
        {"id": "check", "name": "Check", "who": "worker",
         "instructions": "Accept or rework the polish."},
        {"id": "end", "name": "End", "who": "worker",
         "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "draft", "result": "done", "to": ["review"]},
        {"from": "review", "result": "revise", "to": ["draft"]},
        {"from": "review", "result": "accept", "to": ["polish"]},
        {"from": "polish", "result": "done", "to": ["check"]},
        {"from": "check", "result": "rework", "to": ["polish"]},
        {"from": "check", "result": "accept", "to": ["end"]},
    ],
})

INDEPENDENT_RETRY_LINEAGES_RECIPE = validate({
    "name": "task-9-independent-retry-lineages-recipe",
    "start": "split",
    "boxes": [
        {"id": "split", "name": "Split", "who": "worker",
         "instructions": "Activate the target and feeder independently."},
        {"id": "target", "name": "Target", "who": "worker",
         "instructions": "Process one exact input lineage."},
        {"id": "feeder", "name": "Feeder", "who": "worker",
         "instructions": "Activate a second target lineage."},
        {"id": "end", "name": "End", "who": "worker",
         "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "split", "result": "done", "to": ["target", "feeder"]},
        {"from": "target", "result": "done", "to": ["end"]},
        {"from": "feeder", "result": "retry", "to": ["target"]},
    ],
})

BACKWARD_FANOUT_RECIPE = validate({
    "name": "task-9-backward-fanout-recipe",
    "start": "draft-left",
    "boxes": [
        {"id": "draft-left", "name": "Draft left", "who": "worker",
         "instructions": "Produce the left draft branch."},
        {"id": "draft-right", "name": "Draft right", "who": "worker",
         "instructions": "Produce the right draft branch."},
        {"id": "review", "name": "Review", "who": "worker",
         "instructions": "Accept or fan out one revision traversal."},
        {"id": "end", "name": "End", "who": "worker",
         "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "draft-left", "result": "done", "to": ["review"]},
        {"from": "draft-right", "result": "done", "to": ["review"]},
        {
            "from": "review",
            "result": "revise",
            "to": ["draft-left", "draft-right"],
        },
        {"from": "review", "result": "accept", "to": ["end"]},
    ],
})

NESTED_JOIN_RECIPE = validate({
    "name": "task-8-nested-join-recipe",
    "start": "outer-split",
    "boxes": [
        {
            "id": "outer-split",
            "name": "Outer split",
            "who": "worker",
            "instructions": "Create outer branches.",
        },
        {
            "id": "outer-left",
            "name": "Outer left",
            "who": "worker",
            "instructions": "Create nested branches.",
        },
        {
            "id": "outer-right",
            "name": "Outer right",
            "who": "worker",
            "instructions": "Handle the outer right branch.",
        },
        {
            "id": "inner-a",
            "name": "Inner A",
            "who": "worker",
            "instructions": "Handle inner A.",
        },
        {
            "id": "inner-b",
            "name": "Inner B",
            "who": "worker",
            "instructions": "Handle inner B.",
        },
        {
            "id": "inner-join",
            "name": "Inner join",
            "who": "worker",
            "instructions": "Merge only the inner generation.",
        },
        {
            "id": "outer-join",
            "name": "Outer join",
            "who": "worker",
            "instructions": "Merge only the outer generation.",
        },
        {
            "id": "end",
            "name": "End",
            "who": "worker",
            "instructions": "Finish.",
            "end": True,
        },
    ],
    "arrows": [
        {
            "from": "outer-split",
            "result": "done",
            "to": ["outer-left", "outer-right"],
        },
        {
            "from": "outer-left",
            "result": "done",
            "to": ["inner-a", "inner-b"],
        },
        {"from": "inner-a", "result": "done", "to": ["inner-join"]},
        {"from": "inner-b", "result": "done", "to": ["inner-join"]},
        {"from": "inner-join", "result": "done", "to": ["outer-join"]},
        {"from": "outer-right", "result": "done", "to": ["outer-join"]},
        {"from": "outer-join", "result": "done", "to": ["end"]},
    ],
})

DIRECT_NESTED_ANCESTOR_JOIN_RECIPE = validate({
    "name": "task-8-direct-nested-ancestor-join-recipe",
    "start": "outer-split",
    "boxes": [
        {"id": "outer-split", "name": "Outer split", "who": "worker",
         "instructions": "Create the outer generation."},
        {"id": "outer-left", "name": "Outer left", "who": "worker",
         "instructions": "Create the nested generation."},
        {"id": "outer-right", "name": "Outer right", "who": "worker",
         "instructions": "Produce the other ancestor input."},
        {"id": "inner-a", "name": "Inner A", "who": "worker",
         "instructions": "Produce one nested ancestor input."},
        {"id": "inner-b", "name": "Inner B", "who": "worker",
         "instructions": "Produce the other nested ancestor input."},
        {"id": "ancestor-join", "name": "Ancestor join", "who": "worker",
         "instructions": "Merge the governing outer generation."},
        {"id": "end", "name": "End", "who": "worker",
         "instructions": "Finish.", "end": True},
    ],
    "arrows": [
        {"from": "outer-split", "result": "done",
         "to": ["outer-left", "outer-right"]},
        {"from": "outer-left", "result": "done",
         "to": ["inner-a", "inner-b"]},
        {"from": "inner-a", "result": "done", "to": ["ancestor-join"]},
        {"from": "inner-b", "result": "done", "to": ["ancestor-join"]},
        {"from": "outer-right", "result": "done", "to": ["ancestor-join"]},
        {"from": "ancestor-join", "result": "done", "to": ["end"]},
    ],
})


def _start(**overrides):
    arguments = {
        "project_id": "project-runtime",
        "board": "board-runtime",
        "recipe": RECIPE,
        "request_text": "Ship the exact runtime request: café.",
        "launch_key": "launch-runtime",
        "workspace_path": "/workspace/runtime",
    }
    arguments.update(overrides)
    return start_run(**arguments)


def test_start_run_rejects_v2_before_creating_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="not executable"):
        _start(recipe=V2_RECIPE, launch_key="must-not-launch-v2")

    assert not (tmp_path / "shipfactory" / "factory.db").exists()


def _reconcile(run_id):
    with store._connect() as conn:
        return reconcile_run(conn, run_id)


def _complete(
    run_id,
    attempt_id,
    *,
    result="done",
    work="Produced work.",
    key="box-completed",
    expected_state="ready",
):
    return enqueue_event(
        run_id=run_id,
        source="test",
        payload={
            "type": "box_completed",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "expected_state": expected_state,
            "result": result,
            "work": work,
        },
        key=key,
    )


def _fail(
    run_id,
    attempt_id,
    *,
    failure,
    key,
    expected_state="ready",
):
    return enqueue_event(
        run_id=run_id,
        source="test",
        payload={
            "type": "box_failed",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "expected_state": expected_state,
            "failure": failure,
        },
        key=key,
    )


def _attempts_by_box(run_id):
    with store._connect() as conn:
        return {
            row["box_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM box_attempts_v1 WHERE run_id=?",
                (run_id,),
            )
        }


def _latest_attempt(run_id, box_id):
    with store._connect() as conn:
        return dict(conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE run_id=? AND box_id=?
               ORDER BY ordinal DESC LIMIT 1""",
            (run_id, box_id),
        ).fetchone())


def _complete_and_apply(
    run_id,
    attempt,
    *,
    result="done",
    work=None,
    key=None,
):
    box_id = attempt["box_id"]
    _complete(
        run_id,
        attempt["id"],
        result=result,
        work=work or f"{box_id} immutable output.",
        key=key or f"{box_id}-completed",
    )
    return apply_events(owner=f"task-8-{box_id}")


def test_box_failure_retries_twice_then_escalates_with_canonical_history(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="technical-failure-limit")
    _reconcile(run["id"])
    attempt_ids = []
    failures = ["worker exited 17", "worker timed out", "executor unavailable"]
    original_input = None

    for ordinal, failure in enumerate(failures, start=1):
        with store._connect() as conn:
            attempt = dict(conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='runtime-start' AND ordinal=?""",
                (run["id"], ordinal),
            ).fetchone())
        attempt_ids.append(attempt["id"])
        if original_input is None:
            original_input = attempt["input_work_json"]
        else:
            assert attempt["input_work_json"] == original_input

        _fail(
            run["id"],
            attempt["id"],
            failure=failure,
            key=f"box-failed-{ordinal}",
        )
        assert apply_events(owner=f"task-9-failure-{ordinal}") == {
            "leased": 1, "applied": 1, "discarded": 0, "failed": 0,
        }

        with store._connect() as conn:
            failed = dict(conn.execute(
                "SELECT * FROM box_attempts_v1 WHERE id=?",
                (attempt["id"],),
            ).fetchone())
            attempts = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM box_attempts_v1
                       WHERE run_id=? AND box_id='runtime-start'
                       ORDER BY ordinal""",
                    (run["id"],),
                )
            ]
        assert failed["state"] == "failed"
        assert failed["technical_failure"] == failure
        if ordinal < 3:
            assert [item["ordinal"] for item in attempts] == list(
                range(1, ordinal + 2),
            )
            assert attempts[-1]["state"] == "ready"
            assert attempts[-1]["input_work_json"] == original_input

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        attempts = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='runtime-start'
                   ORDER BY ordinal""",
                (run["id"],),
            )
        ]
    assert stored_run["state"] == "escalated"
    assert stored_run["completed_at"] is None
    assert stored_run["blocked_reason"] == json.dumps(
        {
            "attempt_ids": attempt_ids,
            "box_id": "runtime-start",
            "failures": failures,
            "type": "technical_failure_limit",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert [attempt["ordinal"] for attempt in attempts] == [1, 2, 3]


@pytest.mark.parametrize(
    ("payload_update", "outcome"),
    [
        ({"unexpected": "field"}, "invalid_payload"),
        ({"failure": " \n\t"}, "invalid_failure"),
        ({"failure": 17}, "invalid_payload"),
    ],
    ids=["extra-key", "blank-failure", "non-string-failure"],
)
def test_box_failed_requires_exact_keys_and_a_nonempty_failure(
    tmp_path, monkeypatch, payload_update, outcome,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key=f"invalid-failure-{outcome}-{len(payload_update)}")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    payload = {
        "type": "box_failed",
        "run_id": run["id"],
        "attempt_id": attempt["id"],
        "expected_state": "ready",
        "failure": "technical failure",
    }
    payload.update(payload_update)
    event_key = f"invalid-failure-{outcome}-{repr(payload_update)}"
    enqueue_event(
        run_id=run["id"],
        source="test",
        payload=payload,
        key=event_key,
    )

    assert apply_events(owner=f"task-9-invalid-{outcome}") == {
        "leased": 1, "applied": 0, "discarded": 1, "failed": 0,
    }
    with store._connect() as conn:
        stored_attempt = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?",
            (attempt["id"],),
        ).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key=?",
            (event_key,),
        ).fetchone())
        attempt_count = conn.execute(
            "SELECT COUNT(*) FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    assert stored_attempt["state"] == "ready"
    assert stored_attempt["technical_failure"] is None
    assert event["outcome"] == outcome
    assert attempt_count == 1


def test_successful_attempt_resets_the_technical_failure_streak(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=BACKWARD_REACHABILITY_RECIPE,
        launch_key="technical-failure-reset",
    )
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["split"])
    _reconcile(run["id"])
    chooser = _attempts_by_box(run["id"])["chooser"]

    _fail(
        run["id"], chooser["id"],
        failure="failure before success",
        key="failure-before-success",
    )
    assert apply_events(owner="task-9-reset-first-failure")["applied"] == 1
    with store._connect() as conn:
        successful_retry = dict(conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE run_id=? AND box_id='chooser' AND ordinal=2""",
            (run["id"],),
        ).fetchone())
    _complete_and_apply(
        run["id"],
        successful_retry,
        result="away",
        work="Successful chooser output.",
        key="successful-chooser-retry",
    )
    _reconcile(run["id"])
    later = _attempts_by_box(run["id"])["later"]
    _complete_and_apply(
        run["id"],
        later,
        result="rewind",
        work="Revisit chooser.",
        key="rewind-after-success",
    )
    _reconcile(run["id"])

    for ordinal in (3, 4):
        with store._connect() as conn:
            attempt = dict(conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='chooser' AND ordinal=?""",
                (run["id"], ordinal),
            ).fetchone())
        _fail(
            run["id"],
            attempt["id"],
            failure=f"failure after success {ordinal}",
            key=f"failure-after-success-{ordinal}",
        )
        assert apply_events(owner=f"task-9-reset-{ordinal}")["applied"] == 1

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        attempts = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='chooser' ORDER BY ordinal""",
                (run["id"],),
            )
        ]
    assert stored_run["state"] == "running"
    assert stored_run["blocked_reason"] is None
    assert [attempt["state"] for attempt in attempts] == [
        "failed", "completed", "failed", "failed", "ready",
    ]


def test_technical_failure_streak_binds_to_exact_input_retry_chain(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=INDEPENDENT_RETRY_LINEAGES_RECIPE,
        launch_key="exact-input-retry-chain",
    )
    _reconcile(run["id"])
    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "split"),
        work="split output",
        key="exact-input-split",
    )
    _reconcile(run["id"])
    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "feeder"),
        result="retry",
        work="feeder output",
        key="exact-input-feeder",
    )
    _reconcile(run["id"])

    with store._connect() as conn:
        target_attempts = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='target'
                   ORDER BY ordinal""",
                (run["id"],),
            )
        ]
    assert len(target_attempts) == 2
    first_input = target_attempts[0]["input_work_json"]
    second_input = target_attempts[1]["input_work_json"]
    assert first_input != second_input

    first_failure_ids = []
    first_failures = []
    first_attempt = target_attempts[0]
    for failure_number in (1, 2):
        failure = f"first lineage failure {failure_number}"
        first_failure_ids.append(first_attempt["id"])
        first_failures.append(failure)
        _fail(
            run["id"],
            first_attempt["id"],
            failure=failure,
            key=f"exact-input-first-failure-{failure_number}",
        )
        assert apply_events(
            owner=f"task-9-exact-input-first-{failure_number}",
        )["applied"] == 1
        with store._connect() as conn:
            first_attempt = dict(conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='target' AND state='ready'
                     AND input_work_json=?
                   ORDER BY ordinal DESC LIMIT 1""",
                (run["id"], first_input),
            ).fetchone())

    second_attempt = target_attempts[1]
    _fail(
        run["id"],
        second_attempt["id"],
        failure="second lineage failure",
        key="exact-input-second-failure",
    )
    assert apply_events(owner="task-9-exact-input-second")["applied"] == 1
    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        second_retry = dict(conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE run_id=? AND box_id='target' AND state='ready'
                 AND input_work_json=?
               ORDER BY ordinal DESC LIMIT 1""",
            (run["id"], second_input),
        ).fetchone())
    assert stored_run["state"] == "running"
    assert stored_run["blocked_reason"] is None

    _complete_and_apply(
        run["id"],
        second_retry,
        work="second lineage completed",
        key="exact-input-second-completed",
    )
    third_failure = "first lineage failure 3"
    first_failure_ids.append(first_attempt["id"])
    first_failures.append(third_failure)
    _fail(
        run["id"],
        first_attempt["id"],
        failure=third_failure,
        key="exact-input-first-failure-3",
    )
    assert apply_events(owner="task-9-exact-input-first-3")["applied"] == 1

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
    assert stored_run["state"] == "escalated"
    assert stored_run["blocked_reason"] == json.dumps(
        {
            "attempt_ids": first_failure_ids,
            "box_id": "target",
            "failures": first_failures,
            "type": "technical_failure_limit",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_third_backward_traversal_escalates_with_cycle_history_and_no_token(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=REWORK_LIMIT_RECIPE,
        launch_key="third-backward-traversal",
    )
    _reconcile(run["id"])
    review_attempt_ids = []

    for cycle in range(1, 4):
        draft = _latest_attempt(run["id"], "draft")
        _complete_and_apply(
            run["id"],
            draft,
            work=f"draft v{cycle}",
            key=f"draft-completed-{cycle}",
        )
        _reconcile(run["id"])
        review = _latest_attempt(run["id"], "review")
        review_attempt_ids.append(review["id"])
        _complete_and_apply(
            run["id"],
            review,
            result="revise",
            work=f"review cycle {cycle}",
            key=f"review-revise-{cycle}",
        )
        if cycle < 3:
            _reconcile(run["id"])

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        backward_tokens = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM route_tokens_v1
                   WHERE run_id=? AND arrow_index=1 ORDER BY created_at,id""",
                (run["id"],),
            )
        ]
        latest_review = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?",
            (review_attempt_ids[-1],),
        ).fetchone())
    assert latest_review["state"] == "completed"
    assert stored_run["state"] == "escalated"
    assert stored_run["completed_at"] is None
    assert stored_run["blocked_reason"] == json.dumps(
        {
            "arrow_index": 1,
            "history": [
                {"output_work": "draft v3", "result": "done"},
                {"output_work": "review cycle 3", "result": "revise"},
            ],
            "result": "revise",
            "source_box_id": "review",
            "type": "rework_limit",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(backward_tokens) == 2
    assert all(token["state"] == "consumed" for token in backward_tokens)


def test_backward_fanout_counts_distinct_source_attempt_traversals(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=BACKWARD_FANOUT_RECIPE,
        launch_key="backward-fanout-distinct-traversals",
    )
    _reconcile(run["id"])
    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "draft-left"),
        work="initial left draft",
        key="backward-fanout-initial-draft",
    )
    _reconcile(run["id"])

    review_attempt_ids = []
    for traversal in range(1, 4):
        review = _latest_attempt(run["id"], "review")
        review_attempt_ids.append(review["id"])
        _complete_and_apply(
            run["id"],
            review,
            result="revise",
            work=f"review traversal {traversal}",
            key=f"backward-fanout-review-{traversal}",
        )
        with store._connect() as conn:
            stored_run = dict(conn.execute(
                "SELECT * FROM recipe_runs_v1 WHERE id=?",
                (run["id"],),
            ).fetchone())
        if traversal == 3:
            assert stored_run["state"] == "escalated"
            break

        assert stored_run["state"] == "running"
        assert stored_run["blocked_reason"] is None
        _reconcile(run["id"])
        for box_id in ("draft-left", "draft-right"):
            _complete_and_apply(
                run["id"],
                _latest_attempt(run["id"], box_id),
                work=f"{box_id} traversal {traversal}",
                key=f"backward-fanout-{box_id}-{traversal}",
            )
        _reconcile(run["id"])

    with store._connect() as conn:
        backward_tokens = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM route_tokens_v1
                   WHERE run_id=? AND arrow_index=2
                   ORDER BY created_at,id""",
                (run["id"],),
            )
        ]
    assert len(backward_tokens) == 4
    assert all(token["state"] == "consumed" for token in backward_tokens)
    assert {
        token["source_attempt_id"] for token in backward_tokens
    } == set(review_attempt_ids[:2])
    assert json.loads(stored_run["blocked_reason"])["type"] == "rework_limit"


def test_rework_history_boundary_uses_source_ordinal_not_consumption_time(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=BACKWARD_FANOUT_RECIPE,
        launch_key="backward-fanout-delayed-consumption",
    )
    _reconcile(run["id"])
    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "draft-left"),
        work="initial left draft",
        key="delayed-fanout-initial-draft",
    )
    _reconcile(run["id"])

    review_attempt_ids = []
    for traversal in (1, 2):
        review = _latest_attempt(run["id"], "review")
        review_attempt_ids.append(review["id"])
        _complete_and_apply(
            run["id"],
            review,
            result="revise",
            work=f"review traversal {traversal}",
            key=f"delayed-fanout-review-{traversal}",
        )
        _reconcile(run["id"])
        for box_id in ("draft-left", "draft-right"):
            _complete_and_apply(
                run["id"],
                _latest_attempt(run["id"], box_id),
                work=f"{box_id} traversal {traversal}",
                key=f"delayed-fanout-{box_id}-{traversal}",
            )
        _reconcile(run["id"])

    with store._connect() as conn:
        updated = conn.execute(
            """UPDATE route_tokens_v1
               SET consumed_at='9999-12-31T23:59:59.999999Z'
               WHERE run_id=? AND source_attempt_id=?
                 AND arrow_index=2 AND destination_box_id='draft-right'
                 AND state='consumed'""",
            (run["id"], review_attempt_ids[0]),
        ).rowcount
    assert updated == 1

    third_review = _latest_attempt(run["id"], "review")
    _complete_and_apply(
        run["id"],
        third_review,
        result="revise",
        work="review traversal 3",
        key="delayed-fanout-review-3",
    )

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
    assert stored_run["state"] == "escalated"
    blocked_reason = json.loads(stored_run["blocked_reason"])
    history = blocked_reason.pop("history")
    assert blocked_reason == {
        "arrow_index": 2,
        "result": "revise",
        "source_box_id": "review",
        "type": "rework_limit",
    }
    assert history[-1] == {
        "output_work": "review traversal 3",
        "result": "revise",
    }
    assert {
        (item["output_work"], item["result"]) for item in history[:-1]
    } == {
        ("draft-left traversal 2", "done"),
        ("draft-right traversal 2", "done"),
    }
    assert len(history) == 3


def test_distinct_backward_arrow_indexes_have_independent_rework_counts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=REWORK_LIMIT_RECIPE,
        launch_key="independent-backward-arrows",
    )
    _reconcile(run["id"])

    for cycle in range(1, 3):
        _complete_and_apply(
            run["id"],
            _latest_attempt(run["id"], "draft"),
            work=f"draft pass {cycle}",
            key=f"independent-draft-{cycle}",
        )
        _reconcile(run["id"])
        _complete_and_apply(
            run["id"],
            _latest_attempt(run["id"], "review"),
            result="revise",
            work=f"review revise {cycle}",
            key=f"independent-review-{cycle}",
        )
        _reconcile(run["id"])

    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "draft"),
        work="accepted draft",
        key="independent-draft-accepted",
    )
    _reconcile(run["id"])
    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "review"),
        result="accept",
        work="review accepted",
        key="independent-review-accepted",
    )
    _reconcile(run["id"])

    for cycle in range(1, 3):
        _complete_and_apply(
            run["id"],
            _latest_attempt(run["id"], "polish"),
            work=f"polish pass {cycle}",
            key=f"independent-polish-{cycle}",
        )
        _reconcile(run["id"])
        _complete_and_apply(
            run["id"],
            _latest_attempt(run["id"], "check"),
            result="rework",
            work=f"check rework {cycle}",
            key=f"independent-check-{cycle}",
        )
        _reconcile(run["id"])

    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "polish"),
        work="accepted polish",
        key="independent-polish-accepted",
    )
    _reconcile(run["id"])
    _complete_and_apply(
        run["id"],
        _latest_attempt(run["id"], "check"),
        result="accept",
        work="check accepted",
        key="independent-check-accepted",
    )
    _reconcile(run["id"])

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        consumed_by_arrow = {
            row["arrow_index"]: row["count"]
            for row in conn.execute(
                """SELECT arrow_index,COUNT(*) AS count
                   FROM route_tokens_v1
                   WHERE run_id=? AND state='consumed' AND arrow_index IN (1,4)
                   GROUP BY arrow_index""",
                (run["id"],),
            )
        }
    assert stored_run["state"] == "running"
    assert stored_run["blocked_reason"] is None
    assert consumed_by_arrow == {1: 2, 4: 2}
    assert _latest_attempt(run["id"], "end")["state"] == "ready"


@pytest.mark.parametrize("forward_result", ["revise", "rework"])
def test_forward_arrows_named_revise_or_rework_do_not_count_as_rework(
    tmp_path, monkeypatch, forward_result,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    recipe = validate({
        "name": f"task-9-forward-{forward_result}",
        "start": "draft",
        "boxes": [
            {"id": "draft", "name": "Draft", "who": "worker",
             "instructions": "Advance with a misleading result label."},
            {"id": "review", "name": "Review", "who": "worker",
             "instructions": "Accept or loop backward."},
            {"id": "end", "name": "End", "who": "worker",
             "instructions": "Finish.", "end": True},
        ],
        "arrows": [
            {"from": "draft", "result": forward_result, "to": ["review"]},
            {"from": "review", "result": "again", "to": ["draft"]},
            {"from": "review", "result": "accept", "to": ["end"]},
        ],
    })
    run = _start(
        recipe=recipe,
        launch_key=f"forward-label-{forward_result}",
    )
    _reconcile(run["id"])

    for cycle in range(1, 4):
        _complete_and_apply(
            run["id"],
            _latest_attempt(run["id"], "draft"),
            result=forward_result,
            work=f"forward pass {cycle}",
            key=f"{forward_result}-forward-{cycle}",
        )
        _reconcile(run["id"])
        if cycle < 3:
            _complete_and_apply(
                run["id"],
                _latest_attempt(run["id"], "review"),
                result="again",
                work=f"backward pass {cycle}",
                key=f"{forward_result}-backward-{cycle}",
            )
            _reconcile(run["id"])

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        token_counts = {
            row["arrow_index"]: row["count"]
            for row in conn.execute(
                """SELECT arrow_index,COUNT(*) AS count
                   FROM route_tokens_v1
                   WHERE run_id=? AND state='consumed' AND arrow_index IN (0,1)
                   GROUP BY arrow_index""",
                (run["id"],),
            )
        }
    assert stored_run["state"] == "running"
    assert stored_run["blocked_reason"] is None
    assert token_counts == {0: 3, 1: 2}
    assert _latest_attempt(run["id"], "review")["state"] == "ready"


def test_start_run_atomically_freezes_recipe_and_creates_one_root_token(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    run = _start()

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?", (run["id"],),
        ).fetchone())
        tokens = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM route_tokens_v1 WHERE run_id=?", (run["id"],),
            )
        ]
        legacy_instances = conn.execute(
            "SELECT COUNT(*) FROM recipe_instances",
        ).fetchone()[0]
        legacy_steps = conn.execute(
            "SELECT COUNT(*) FROM recipe_steps",
        ).fetchone()[0]

    assert run == stored_run
    assert stored_run["project_id"] == "project-runtime"
    assert stored_run["board"] == "board-runtime"
    assert stored_run["recipe_name"] == RECIPE.name
    assert stored_run["recipe_hash"] == RECIPE.hash
    assert stored_run["recipe_snapshot_json"] == RECIPE.canonical_json
    assert json.loads(stored_run["recipe_snapshot_json"]) == json.loads(
        RECIPE.canonical_json,
    )
    assert stored_run["request_text"] == "Ship the exact runtime request: café."
    assert stored_run["workspace_path"] == "/workspace/runtime"
    assert stored_run["launch_key"] == "launch-runtime"
    assert stored_run["state"] == "running"
    assert len(tokens) == 1
    assert tokens[0]["source_attempt_id"] is None
    assert tokens[0]["arrow_index"] is None
    assert tokens[0]["destination_box_id"] == RECIPE.start
    assert json.loads(tokens[0]["lineage_json"]) == []
    assert json.loads(tokens[0]["work_refs_json"]) == {
        "preceding_outputs": [],
        "request": "Ship the exact runtime request: café.",
    }
    assert tokens[0]["state"] == "pending"
    assert legacy_instances == 0
    assert legacy_steps == 0


def test_start_run_is_idempotent_by_launch_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    first = _start()
    replay = _start()

    assert replay == first
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM recipe_runs_v1",
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM route_tokens_v1",
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "conflicting_input",
    [
        {"request_text": "A conflicting runtime request."},
        {"recipe": CONFLICTING_RECIPE},
    ],
    ids=["request", "recipe"],
)
def test_start_run_rejects_conflicting_launch_identity_without_mutating_original(
    tmp_path, monkeypatch, conflicting_input,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = _start()

    with store._connect() as conn:
        original_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?", (original["id"],),
        ).fetchone())
        original_token = dict(conn.execute(
            "SELECT * FROM route_tokens_v1 WHERE run_id=?", (original["id"],),
        ).fetchone())

    with pytest.raises(store.GraphStoreConflict, match="launch key"):
        _start(**conflicting_input)

    with store._connect() as conn:
        runs = [
            dict(row)
            for row in conn.execute("SELECT * FROM recipe_runs_v1")
        ]
        tokens = [
            dict(row)
            for row in conn.execute("SELECT * FROM route_tokens_v1")
        ]

    assert runs == [original_run]
    assert tokens == [original_token]


def test_concurrent_identical_start_run_calls_share_one_run_and_root_token(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    monkeypatch.setattr(store, "init_db", lambda: None)
    barrier = Barrier(2)

    def start():
        barrier.wait()
        return _start()

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _index: start(), range(2)))

    assert runs[0] == runs[1]
    with store._connect() as conn:
        stored_runs = [
            dict(row)
            for row in conn.execute("SELECT * FROM recipe_runs_v1")
        ]
        tokens = [
            dict(row)
            for row in conn.execute("SELECT * FROM route_tokens_v1")
        ]

    assert stored_runs == [runs[0]]
    assert len(tokens) == 1
    assert tokens[0]["run_id"] == runs[0]["id"]
    assert tokens[0]["destination_box_id"] == RECIPE.start


def test_start_run_rolls_back_run_when_root_token_insert_fails(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def fail_root_token(**_kwargs):
        raise RuntimeError("root token insert failed")

    monkeypatch.setattr(store, "insert_route_token_v1", fail_root_token)

    with pytest.raises(RuntimeError, match="root token insert failed"):
        _start()

    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM recipe_runs_v1",
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM route_tokens_v1",
        ).fetchone()[0] == 0


def test_reconcile_turns_one_pending_token_into_one_ready_attempt_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()

    first = _reconcile(run["id"])
    replay = _reconcile(run["id"])

    with store._connect() as conn:
        attempts = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
            )
        ]
        tokens = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM route_tokens_v1 WHERE run_id=?", (run["id"],),
            )
        ]

    assert first == {"attempts_created": 1, "tokens_consumed": 1}
    assert replay == {"attempts_created": 0, "tokens_consumed": 0}
    assert len(attempts) == 1
    assert attempts[0]["box_id"] == RECIPE.start
    assert attempts[0]["ordinal"] == 1
    assert attempts[0]["state"] == "ready"
    assert json.loads(attempts[0]["input_work_json"]) == {
        "activating_token_ids": [tokens[0]["id"]],
        "preceding_outputs": [],
        "request": "Ship the exact runtime request: café.",
    }
    assert [token["state"] for token in tokens] == ["consumed"]


def test_completion_routes_exact_frozen_arrow_and_next_attempt_keeps_work_reference(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    with store._connect() as conn:
        conn.execute(
            """INSERT INTO split_groups_v1(
                   id,run_id,parent_lineage_json,branch_ids_json,state,created_at
               ) VALUES(
                   'preserved-group',?,'[]','["preserved-branch","unused-branch"]',
                   'open','2026-01-01T00:00:00+00:00'
               )""",
            (run["id"],),
        )
        conn.execute(
            """UPDATE route_tokens_v1
               SET lineage_json=?
               WHERE run_id=?""",
            (
                json.dumps([{
                    "split_group_id": "preserved-group",
                    "branch_id": "preserved-branch",
                }]),
                run["id"],
            ),
        )
    _reconcile(run["id"])
    with store._connect() as conn:
        start_attempt = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone())

    queued = _complete(
        run["id"],
        start_attempt["id"],
        work="Immutable plan output: café.",
    )
    applied = apply_events(owner="task-7")

    with store._connect() as conn:
        completed = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?", (start_attempt["id"],),
        ).fetchone())
        destination = dict(conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND state='pending'""",
            (run["id"],),
        ).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key=?", (queued["key"],),
        ).fetchone())

    assert applied == {"leased": 1, "applied": 1, "discarded": 0, "failed": 0}
    assert completed["state"] == "completed"
    assert completed["result"] == "done"
    assert completed["output_work"] == "Immutable plan output: café."
    assert completed["finished_at"] is not None
    assert destination["source_attempt_id"] == start_attempt["id"]
    assert destination["arrow_index"] == 0
    assert destination["destination_box_id"] == "runtime-end"
    assert json.loads(destination["lineage_json"]) == [{
        "split_group_id": "preserved-group",
        "branch_id": "preserved-branch",
    }]
    assert json.loads(destination["work_refs_json"]) == {
        "preceding_outputs": [
            {
                "attempt_id": start_attempt["id"],
                "box_id": "runtime-start",
                "output_work": "Immutable plan output: café.",
            },
        ],
        "request": "Ship the exact runtime request: café.",
    }
    assert event["state"] == "applied"
    assert event["outcome"] == "routed"

    _reconcile(run["id"])
    with store._connect() as conn:
        end_attempt = dict(conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE run_id=? AND box_id='runtime-end'""",
            (run["id"],),
        ).fetchone())
    assert json.loads(end_attempt["input_work_json"]) == {
        "activating_token_ids": [destination["id"]],
        "preceding_outputs": [
            {
                "attempt_id": start_attempt["id"],
                "box_id": "runtime-start",
                "output_work": "Immutable plan output: café.",
            },
        ],
        "request": "Ship the exact runtime request: café.",
    }


def test_completion_creates_one_pending_token_per_matching_destination(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=FAN_OUT_RECIPE,
        launch_key="fan-out",
    )
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone()[0]

    _complete(run["id"], attempt_id, key="fan-out-completed")
    apply_events(owner="task-7")

    with store._connect() as conn:
        tokens = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM route_tokens_v1
                   WHERE run_id=? AND state='pending'
                   ORDER BY destination_box_id""",
                (run["id"],),
            )
        ]
        split_group = dict(conn.execute(
            "SELECT * FROM split_groups_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone())
    assert [token["destination_box_id"] for token in tokens] == ["left", "right"]
    assert [token["arrow_index"] for token in tokens] == [0, 0]
    assert {token["source_attempt_id"] for token in tokens} == {attempt_id}
    assert split_group["state"] == "open"
    assert json.loads(split_group["parent_lineage_json"]) == []
    branch_ids = json.loads(split_group["branch_ids_json"])
    assert len(branch_ids) == 2
    assert len(set(branch_ids)) == 2
    assert {
        tuple(sorted(json.loads(token["lineage_json"])[0].items()))
        for token in tokens
    } == {
        tuple(sorted({
            "split_group_id": split_group["id"],
            "branch_id": branch_id,
        }.items()))
        for branch_id in branch_ids
    }


def test_unroutable_result_completes_attempt_and_pauses_run(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone()[0]

    _complete(
        run["id"],
        attempt_id,
        result="unexpected",
        work="Valid work with an undeclared result.",
    )
    applied = apply_events(owner="task-7")

    with store._connect() as conn:
        attempt = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?", (attempt_id,),
        ).fetchone())
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?", (run["id"],),
        ).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='box-completed'",
        ).fetchone())

    assert applied["applied"] == 1
    assert attempt["state"] == "completed"
    assert attempt["result"] == "unexpected"
    assert attempt["output_work"] == "Valid work with an undeclared result."
    assert stored_run["state"] == "paused"
    assert stored_run["blocked_reason"] == "unroutable_result"
    assert event["state"] == "applied"
    assert event["outcome"] == "unroutable_result"


def test_duplicate_completion_key_is_permanently_spent_and_not_replayed(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone()[0]

    first = _complete(run["id"], attempt_id, key="spent-completion")
    apply_events(owner="task-7")
    replay = _complete(run["id"], attempt_id, key="spent-completion")
    second_apply = apply_events(owner="task-7")

    assert replay["key"] == first["key"]
    assert replay["state"] == "applied"
    assert replay["attempt_count"] == 1
    assert replay["outcome"] == "routed"
    assert second_apply == {
        "leased": 0,
        "applied": 0,
        "discarded": 0,
        "failed": 0,
    }
    with store._connect() as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM route_tokens_v1
               WHERE run_id=? AND state='pending'""",
            (run["id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM run_events_v1 WHERE key='spent-completion'",
        ).fetchone()[0] == "applied"


def test_concurrent_event_appliers_lease_and_apply_completion_exactly_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="concurrent-event-appliers")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    _complete(run["id"], attempt["id"], key="concurrent-completion")

    barrier = Barrier(2)

    def synchronized_apply(owner):
        barrier.wait()
        return apply_events(owner=owner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(synchronized_apply, f"task-10-applier-{index}")
            for index in range(2)
        ]
        results = [future.result() for future in futures]

    assert sum(result["leased"] for result in results) == 1
    assert sum(result["applied"] for result in results) == 1
    assert sum(result["discarded"] for result in results) == 0
    assert sum(result["failed"] for result in results) == 0
    with store._connect() as conn:
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='concurrent-completion'",
        ).fetchone())
        route_count = conn.execute(
            """SELECT COUNT(*) FROM route_tokens_v1
               WHERE run_id=? AND source_attempt_id=?""",
            (run["id"], attempt["id"]),
        ).fetchone()[0]
    assert event["state"] == "applied"
    assert event["attempt_count"] == 1
    assert event["outcome"] == "routed"
    assert route_count == 1


def test_apply_events_reacquires_and_applies_an_expired_crash_lease(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="expired-crash-lease")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    _complete(run["id"], attempt["id"], key="expired-crash-completion")

    crashed_lease = store.lease_run_events_v1(
        owner="crashed-task-10-owner",
        limit=1,
        lease_seconds=1,
        now="2000-01-01T00:00:00Z",
    )
    assert crashed_lease[0]["attempt_count"] == 1

    result = apply_events(owner="task-10-recovery-owner")

    assert result == {
        "leased": 1,
        "applied": 1,
        "discarded": 0,
        "failed": 0,
    }
    with store._connect() as conn:
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='expired-crash-completion'",
        ).fetchone())
        route_count = conn.execute(
            """SELECT COUNT(*) FROM route_tokens_v1
               WHERE run_id=? AND source_attempt_id=?""",
            (run["id"], attempt["id"]),
        ).fetchone()[0]
    assert event["state"] == "applied"
    assert event["attempt_count"] == 2
    assert event["lease_owner"] is None
    assert event["outcome"] == "routed"
    assert route_count == 1


def test_apply_events_discards_stale_attempt_state_without_mutating_run_or_attempt(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="stale-attempt-state-unchanged")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    _complete(
        run["id"],
        attempt["id"],
        key="task-10-stale-attempt-state",
        expected_state="running",
    )
    with store._connect() as conn:
        run_before = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        attempt_before = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?",
            (attempt["id"],),
        ).fetchone())

    result = apply_events(owner="task-10-stale-owner")

    assert result == {
        "leased": 1,
        "applied": 0,
        "discarded": 1,
        "failed": 0,
    }
    with store._connect() as conn:
        run_after = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        attempt_after = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?",
            (attempt["id"],),
        ).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='task-10-stale-attempt-state'",
        ).fetchone())
    assert run_after == run_before
    assert attempt_after == attempt_before
    assert event["state"] == "discarded"
    assert event["outcome"] == "stale_attempt_state"


def test_apply_events_rolls_back_transition_before_recording_apply_failure(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="apply-event-rollback")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    _complete(run["id"], attempt["id"], key="task-10-apply-failure")
    with store._connect() as conn:
        run_before = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        attempt_before = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?",
            (attempt["id"],),
        ).fetchone())

    def mutate_then_raise(conn, event):
        assert event["key"] == "task-10-apply-failure"
        conn.execute(
            """UPDATE recipe_runs_v1
               SET state='paused',blocked_reason='must-roll-back'
               WHERE id=?""",
            (run["id"],),
        )
        raise RuntimeError("injected task-10 apply failure")

    monkeypatch.setattr(graph_runner, "_apply_event", mutate_then_raise)

    result = apply_events(owner="task-10-failure-owner")

    assert result == {
        "leased": 1,
        "applied": 0,
        "discarded": 0,
        "failed": 1,
    }
    with store._connect() as conn:
        run_after = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone())
        attempt_after = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE id=?",
            (attempt["id"],),
        ).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='task-10-apply-failure'",
        ).fetchone())
    assert run_after == run_before
    assert attempt_after == attempt_before
    assert event["state"] == "failed"
    assert event["outcome"] == "apply_failed"
    assert event["last_error"] == "injected task-10 apply failure"


def test_lost_lease_does_not_abort_other_events_and_can_be_recovered(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first_run = _start(launch_key="lost-lease-first")
    second_run = _start(launch_key="lost-lease-second")
    for run in (first_run, second_run):
        _reconcile(run["id"])
    first_attempt = _latest_attempt(first_run["id"], "runtime-start")
    second_attempt = _latest_attempt(second_run["id"], "runtime-start")
    _complete(
        first_run["id"], first_attempt["id"], key="lost-lease-first-event",
    )
    _complete(
        second_run["id"], second_attempt["id"], key="lost-lease-second-event",
    )
    real_finish = store.finish_run_event_v1

    def lose_first_lease(key, **kwargs):
        if key == "lost-lease-first-event":
            raise store.GraphStoreConflict("injected expired lease")
        return real_finish(key, **kwargs)

    monkeypatch.setattr(store, "finish_run_event_v1", lose_first_lease)
    summary = apply_events(owner="task-10-lost-lease-owner", limit=2)
    assert summary == {
        "leased": 2,
        "applied": 1,
        "discarded": 0,
        "failed": 1,
    }
    with store._connect() as conn:
        first_event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='lost-lease-first-event'",
        ).fetchone())
        second_event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='lost-lease-second-event'",
        ).fetchone())
    assert first_event["state"] == "leased"
    assert second_event["state"] == "applied"

    monkeypatch.setattr(store, "finish_run_event_v1", real_finish)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE run_events_v1 SET lease_until='2000-01-01T00:00:00+00:00'
               WHERE key='lost-lease-first-event'"""
        )
    recovered = apply_events(owner="task-10-lost-lease-recovery", limit=1)
    assert recovered == {
        "leased": 1,
        "applied": 1,
        "discarded": 0,
        "failed": 0,
    }


def test_event_transition_runtime_cannot_call_process_or_kanban_mutators(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="no-external-transition-effects")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    _complete(run["id"], attempt["id"], key="no-external-transition-event")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external effect inside GraphRunner transition")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in (
        "system", "fork", "posix_spawn", "posix_spawnp",
        "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe",
    ):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)

    from hermes_cli import kanban_db
    for name in (
        "block_task", "cancel_subtree", "claim_task", "complete_task",
        "create_blocked_task", "create_task", "reclaim_task", "unblock_task",
    ):
        monkeypatch.setattr(kanban_db, name, forbidden, raising=False)

    assert apply_events(owner="task-10-no-external-owner") == {
        "leased": 1,
        "applied": 1,
        "discarded": 0,
        "failed": 0,
    }


def test_conflicting_reuse_of_terminal_event_key_preserves_original_event(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="conflicting-terminal-event-key")
    _reconcile(run["id"])
    attempt = _latest_attempt(run["id"], "runtime-start")
    _complete(run["id"], attempt["id"], key="task-10-permanent-key")
    assert apply_events(owner="task-10-permanent-key-owner")["applied"] == 1
    with store._connect() as conn:
        original = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='task-10-permanent-key'",
        ).fetchone())

    with pytest.raises(store.GraphStoreConflict, match="different content"):
        _complete(
            run["id"],
            attempt["id"],
            key="task-10-permanent-key",
            work="Conflicting replacement work.",
        )

    with store._connect() as conn:
        unchanged = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='task-10-permanent-key'",
        ).fetchone())
        route_count = conn.execute(
            """SELECT COUNT(*) FROM route_tokens_v1
               WHERE run_id=? AND source_attempt_id=?""",
            (run["id"], attempt["id"]),
        ).fetchone()[0]
    assert unchanged == original
    assert unchanged["state"] == "applied"
    assert unchanged["outcome"] == "routed"
    assert route_count == 1


def test_apply_events_owner_is_stripped_nonempty_and_at_most_128_utf8_bytes(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert apply_events(owner="é" * 64) == {
        "leased": 0,
        "applied": 0,
        "discarded": 0,
        "failed": 0,
    }

    lease_called = False

    def forbidden_lease(**kwargs):
        nonlocal lease_called
        lease_called = True
        raise AssertionError("invalid owner reached the lease boundary")

    monkeypatch.setattr(
        graph_runner.store,
        "lease_run_events_v1",
        forbidden_lease,
    )
    for invalid_owner in (None, "", " \t\n", " owner", "owner ", "é" * 65):
        with pytest.raises(ValueError, match="owner"):
            apply_events(owner=invalid_owner)
    assert lease_called is False


def test_completion_event_validates_run_attempt_state_result_and_work(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone()[0]

    enqueue_event(
        run_id=run["id"],
        source="test",
        payload={
            "type": "box_completed",
            "run_id": run["id"],
            "attempt_id": attempt_id,
            "expected_state": "running",
            "result": "done",
            "work": "Work from a stale state.",
        },
        key="stale-completion",
    )
    applied = apply_events(owner="task-7")

    assert applied == {"leased": 1, "applied": 0, "discarded": 1, "failed": 0}
    with store._connect() as conn:
        assert conn.execute(
            "SELECT state FROM box_attempts_v1 WHERE id=?", (attempt_id,),
        ).fetchone()[0] == "ready"
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='stale-completion'",
        ).fetchone())
    assert event["state"] == "discarded"
    assert event["outcome"] == "stale_attempt_state"


def test_run_completes_only_after_explicit_end_attempt_completes(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        start_attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?", (run["id"],),
        ).fetchone()[0]

    _complete(run["id"], start_attempt_id, key="start-completed")
    apply_events(owner="task-7")
    _reconcile(run["id"])

    with store._connect() as conn:
        end_attempt_id = conn.execute(
            """SELECT id FROM box_attempts_v1
               WHERE run_id=? AND box_id='runtime-end'""",
            (run["id"],),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT state FROM recipe_runs_v1 WHERE id=?", (run["id"],),
        ).fetchone()[0] == "running"

    _complete(run["id"], end_attempt_id, key="end-completed")
    apply_events(owner="task-7")

    with store._connect() as conn:
        stored_run = dict(conn.execute(
            "SELECT * FROM recipe_runs_v1 WHERE id=?", (run["id"],),
        ).fetchone())
        assert conn.execute(
            """SELECT COUNT(*) FROM route_tokens_v1
               WHERE run_id=? AND state='pending'""",
            (run["id"],),
        ).fetchone()[0] == 0
    assert stored_run["state"] == "completed"
    assert stored_run["completed_at"] is not None
    assert stored_run["blocked_reason"] is None


def test_lineage_uses_exact_activating_token_when_work_is_identical(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    with store._connect() as conn:
        conn.execute(
            """INSERT INTO split_groups_v1(
                   id,run_id,parent_lineage_json,branch_ids_json,state,created_at
               ) VALUES(
                   'identity-group',?,'[]','["correct-branch","wrong-branch"]',
                   'open','2026-01-01T00:00:00+00:00'
               )""",
            (run["id"],),
        )
        conn.execute(
            """UPDATE route_tokens_v1
               SET lineage_json=?
               WHERE run_id=?""",
            (
                json.dumps([{
                    "split_group_id": "identity-group",
                    "branch_id": "correct-branch",
                }]),
                run["id"],
            ),
        )
    _reconcile(run["id"])

    with store._connect() as conn:
        root_token = dict(conn.execute(
            "SELECT * FROM route_tokens_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone())
        attempt = dict(conn.execute(
            "SELECT * FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone())
        conn.execute(
            """INSERT INTO route_tokens_v1(
                   id,run_id,source_attempt_id,arrow_index,destination_box_id,
                   lineage_json,work_refs_json,state,created_at,consumed_at
               ) VALUES(
                   'decoy-token',?,?,0,?,
                   ? ,?,'consumed',?,?
               )""",
            (
                run["id"],
                attempt["id"],
                RECIPE.start,
                json.dumps([{
                    "split_group_id": "identity-group",
                    "branch_id": "wrong-branch",
                }]),
                json.dumps({
                    "preceding_outputs": [],
                    "request": run["request_text"],
                }),
                "9999-01-01T00:00:00+00:00",
                "9999-01-01T00:00:00+00:00",
            ),
        )

    _complete(run["id"], attempt["id"], key="identity-bound-lineage")
    applied = apply_events(owner="task-7")

    assert applied == {"leased": 1, "applied": 1, "discarded": 0, "failed": 0}
    with store._connect() as conn:
        destination = dict(conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND state='pending'""",
            (run["id"],),
        ).fetchone())
        stored_input = json.loads(attempt["input_work_json"])
    assert stored_input["activating_token_ids"] == [root_token["id"]]
    assert json.loads(destination["lineage_json"]) == [{
        "split_group_id": "identity-group",
        "branch_id": "correct-branch",
    }]


@pytest.mark.parametrize(
    "work_refs",
    [
        {"request": "Ship the exact runtime request: café."},
        {
            "request": "Ship the exact runtime request: café.",
            "preceding_outputs": [],
            "unexpected": True,
        },
    ],
    ids=["missing-history", "extra-field"],
)
def test_root_token_requires_exact_work_reference_shape(
    tmp_path, monkeypatch, work_refs,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    with store._connect() as conn:
        conn.execute(
            "UPDATE route_tokens_v1 SET work_refs_json=? WHERE run_id=?",
            (json.dumps(work_refs), run["id"]),
        )

    with pytest.raises(
        store.GraphStoreIntegrityError,
        match="work references.*exact|required",
    ):
        _reconcile(run["id"])


def test_root_token_rejects_nonempty_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    with store._connect() as conn:
        store.insert_box_attempt_v1(
            attempt_id="synthetic-completed-attempt",
            run_id=run["id"],
            box_id=RECIPE.start,
            ordinal=1,
            state="ready",
            input_work={
                "activating_token_ids": ["synthetic-token"],
                "preceding_outputs": [],
                "request": run["request_text"],
            },
            conn=conn,
        )
        store.update_box_attempt_v1(
            "synthetic-completed-attempt",
            expected_state="ready",
            state="completed",
            result="done",
            output_work="Synthetic completed work.",
            conn=conn,
        )
        conn.execute(
            "UPDATE route_tokens_v1 SET work_refs_json=? WHERE run_id=?",
            (
                json.dumps({
                    "preceding_outputs": [{
                        "attempt_id": "synthetic-completed-attempt",
                        "box_id": RECIPE.start,
                        "output_work": "Synthetic completed work.",
                    }],
                    "request": run["request_text"],
                }),
                run["id"],
            ),
        )

    with pytest.raises(store.GraphStoreIntegrityError, match="root.*empty history"):
        _reconcile(run["id"])


def test_non_root_token_rejects_omitted_source_provenance(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    _complete(run["id"], attempt_id, key="source-completed")
    apply_events(owner="task-7")
    with store._connect() as conn:
        conn.execute(
            """UPDATE route_tokens_v1
               SET work_refs_json=?
               WHERE run_id=? AND state='pending'""",
            (
                json.dumps({
                    "preceding_outputs": [],
                    "request": run["request_text"],
                }),
                run["id"],
            ),
        )

    with pytest.raises(
        store.GraphStoreIntegrityError,
        match="source attempt provenance",
    ):
        _reconcile(run["id"])


def test_non_root_token_rejects_foreign_run_provenance(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="provenance-target")
    foreign_run = _start(launch_key="provenance-foreign")
    _reconcile(run["id"])
    _reconcile(foreign_run["id"])
    with store._connect() as conn:
        attempt_ids = {
            row["run_id"]: row["id"]
            for row in conn.execute(
                "SELECT id,run_id FROM box_attempts_v1",
            )
        }
    _complete(
        run["id"],
        attempt_ids[run["id"]],
        key="target-source-completed",
    )
    _complete(
        foreign_run["id"],
        attempt_ids[foreign_run["id"]],
        work="Foreign output.",
        key="foreign-source-completed",
    )
    apply_events(owner="task-7")
    with store._connect() as conn:
        conn.execute(
            """UPDATE route_tokens_v1
               SET work_refs_json=?
               WHERE run_id=? AND state='pending'""",
            (
                json.dumps({
                    "preceding_outputs": [{
                        "attempt_id": attempt_ids[foreign_run["id"]],
                        "box_id": RECIPE.start,
                        "output_work": "Foreign output.",
                    }],
                    "request": run["request_text"],
                }),
                run["id"],
            ),
        )

    with pytest.raises(
        store.GraphStoreIntegrityError,
        match="source attempt provenance",
    ):
        _reconcile(run["id"])


def test_non_root_token_rejects_cross_branch_provenance(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(recipe=FAN_OUT_RECIPE, launch_key="cross-branch")
    _reconcile(run["id"])
    with store._connect() as conn:
        start_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    _complete(run["id"], start_id, key="fan-out-source")
    apply_events(owner="task-7")
    _reconcile(run["id"])
    with store._connect() as conn:
        branch_attempts = {
            row["box_id"]: dict(row)
            for row in conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id IN ('left','right')""",
                (run["id"],),
            )
        }
    _complete(
        run["id"],
        branch_attempts["left"]["id"],
        work="Left output.",
        key="left-completed",
    )
    _complete(
        run["id"],
        branch_attempts["right"]["id"],
        work="Right output.",
        key="right-completed",
    )
    apply_events(owner="task-7")

    left_history = json.loads(
        branch_attempts["left"]["input_work_json"],
    )["preceding_outputs"]
    with store._connect() as conn:
        conn.execute(
            """UPDATE route_tokens_v1
               SET work_refs_json=?
               WHERE run_id=? AND source_attempt_id=?""",
            (
                json.dumps({
                    "preceding_outputs": [
                        *left_history,
                        {
                            "attempt_id": branch_attempts["right"]["id"],
                            "box_id": "right",
                            "output_work": "Right output.",
                        },
                    ],
                    "request": run["request_text"],
                }),
                run["id"],
                branch_attempts["left"]["id"],
            ),
        )

    with pytest.raises(
        store.GraphStoreIntegrityError,
        match="source attempt provenance",
    ):
        _reconcile(run["id"])


def test_non_root_token_revalidates_frozen_arrow_provenance(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(recipe=FAN_OUT_RECIPE, launch_key="arrow-provenance")
    _reconcile(run["id"])
    with store._connect() as conn:
        start_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    _complete(run["id"], start_id, key="arrow-source-completed")
    apply_events(owner="task-7")
    with store._connect() as conn:
        conn.execute(
            """UPDATE route_tokens_v1
               SET arrow_index=1
               WHERE run_id=? AND destination_box_id='left' AND state='pending'""",
            (run["id"],),
        )

    with pytest.raises(store.GraphStoreIntegrityError, match="arrow provenance"):
        _reconcile(run["id"])

    with store._connect() as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM box_attempts_v1
               WHERE run_id=? AND box_id IN ('left','right')""",
            (run["id"],),
        ).fetchone()[0] == 0


def test_completion_rejects_attempt_from_another_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = _start(launch_key="first-run")
    second = _start(launch_key="second-run")
    _reconcile(first["id"])
    _reconcile(second["id"])
    with store._connect() as conn:
        foreign_attempt = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (second["id"],),
        ).fetchone()[0]

    _complete(first["id"], foreign_attempt, key="cross-run-completion")
    applied = apply_events(owner="task-7")

    assert applied == {"leased": 1, "applied": 0, "discarded": 1, "failed": 0}
    with store._connect() as conn:
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key='cross-run-completion'",
        ).fetchone())
        assert conn.execute(
            "SELECT state FROM box_attempts_v1 WHERE id=?",
            (foreign_attempt,),
        ).fetchone()[0] == "ready"
    assert event["outcome"] == "attempt_run_mismatch"


@pytest.mark.parametrize(
    ("payload_update", "outcome"),
    [
        ({"unexpected": "field"}, "invalid_payload"),
        ({"result": "Done!"}, "invalid_result"),
        ({"work": " \n\t"}, "invalid_work"),
    ],
    ids=["malformed-payload", "invalid-result", "blank-work"],
)
def test_completion_rejects_malformed_result_or_work(
    tmp_path, monkeypatch, payload_update, outcome,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    payload = {
        "type": "box_completed",
        "run_id": run["id"],
        "attempt_id": attempt_id,
        "expected_state": "ready",
        "result": "done",
        "work": "Valid work.",
    }
    payload.update(payload_update)
    enqueue_event(
        run_id=run["id"],
        source="test",
        payload=payload,
        key=f"invalid-{outcome}",
    )

    applied = apply_events(owner="task-7")

    assert applied == {"leased": 1, "applied": 0, "discarded": 1, "failed": 0}
    with store._connect() as conn:
        event = dict(conn.execute(
            "SELECT * FROM run_events_v1 WHERE key=?",
            (f"invalid-{outcome}",),
        ).fetchone())
        assert conn.execute(
            "SELECT state FROM box_attempts_v1 WHERE id=?",
            (attempt_id,),
        ).fetchone()[0] == "ready"
    assert event["outcome"] == outcome


@pytest.mark.parametrize("fence", ["lease-owner", "attempt-count"])
def test_box_completion_is_fenced_by_exact_event_lease(
    tmp_path, monkeypatch, fence,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start()
    _reconcile(run["id"])
    with store._connect() as conn:
        attempt_id = conn.execute(
            "SELECT id FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    _complete(run["id"], attempt_id, key=f"fenced-{fence}")
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = store.lease_run_events_v1(
            owner="rightful-owner",
            run_id=run["id"],
            conn=conn,
        )[0]
    stale_event = dict(event)
    if fence == "lease-owner":
        stale_event["lease_owner"] = "other-owner"
    else:
        stale_event["attempt_count"] += 1

    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(store.GraphStoreConflict, match="lease changed"):
            graph_runner._apply_box_completed(conn, stale_event)

    with store._connect() as conn:
        assert conn.execute(
            "SELECT state FROM box_attempts_v1 WHERE id=?",
            (attempt_id,),
        ).fetchone()[0] == "ready"


@pytest.mark.parametrize(
    "completion_order",
    list(permutations(("alpha", "beta", "gamma"))),
)
def test_three_direct_siblings_join_once_in_every_completion_permutation(
    tmp_path, monkeypatch, completion_order,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=THREE_WAY_JOIN_RECIPE,
        launch_key="three-way-" + "-".join(completion_order),
    )
    _reconcile(run["id"])
    split_attempt = _attempts_by_box(run["id"])["split"]
    _complete_and_apply(run["id"], split_attempt)
    _reconcile(run["id"])
    branch_attempts = _attempts_by_box(run["id"])

    for index, box_id in enumerate(completion_order):
        _complete_and_apply(
            run["id"],
            branch_attempts[box_id],
            work=f"{box_id} output.",
            key=f"{box_id}-completed-{index}",
        )
        _reconcile(run["id"])

    with store._connect() as conn:
        synthesis = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM box_attempts_v1
                   WHERE run_id=? AND box_id='synthesis'""",
                (run["id"],),
            )
        ]
        group = dict(conn.execute(
            "SELECT * FROM split_groups_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone())
        arrival_tokens = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM route_tokens_v1
                   WHERE run_id=? AND destination_box_id='synthesis'
                   ORDER BY id""",
                (run["id"],),
            )
        ]

    assert len(synthesis) == 1
    assert synthesis[0]["state"] == "ready"
    input_work = json.loads(synthesis[0]["input_work_json"])
    assert input_work["activating_token_ids"] == [
        token["id"] for token in arrival_tokens
    ]
    assert input_work["preceding_outputs"][0] == {
        "attempt_id": split_attempt["id"],
        "box_id": "split",
        "output_work": "split immutable output.",
    }
    assert {
        reference["box_id"]: reference
        for reference in input_work["preceding_outputs"][1:]
    } == {
        box_id: {
            "attempt_id": branch_attempts[box_id]["id"],
            "box_id": box_id,
            "output_work": f"{box_id} output.",
        }
        for box_id in ("alpha", "beta", "gamma")
    }
    assert group["state"] == "closed"
    assert all(token["state"] == "consumed" for token in arrival_tokens)


def test_duplicate_join_reconciliation_preserves_one_attempt_and_closed_group(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(recipe=THREE_WAY_JOIN_RECIPE, launch_key="duplicate-join")
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["split"])
    _reconcile(run["id"])
    branches = _attempts_by_box(run["id"])
    for box_id in ("alpha", "beta", "gamma"):
        _complete_and_apply(run["id"], branches[box_id])

    first = _reconcile(run["id"])
    duplicate_delivery = _complete(
        run["id"],
        branches["alpha"]["id"],
        work="alpha immutable output.",
        key="alpha-completed",
    )
    duplicate_apply = apply_events(owner="task-8-duplicate-delivery")
    replay = _reconcile(run["id"])

    with store._connect() as conn:
        synthesis_count = conn.execute(
            """SELECT COUNT(*) FROM box_attempts_v1
               WHERE run_id=? AND box_id='synthesis'""",
            (run["id"],),
        ).fetchone()[0]
        group_state = conn.execute(
            "SELECT state FROM split_groups_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    assert first == {"attempts_created": 1, "tokens_consumed": 3}
    assert duplicate_delivery["state"] == "applied"
    assert duplicate_apply == {
        "leased": 0, "applied": 0, "discarded": 0, "failed": 0,
    }
    assert replay == {"attempts_created": 0, "tokens_consumed": 0}
    assert synthesis_count == 1
    assert group_state == "closed"


def test_join_waits_while_slow_live_sibling_can_still_arrive(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(recipe=THREE_WAY_JOIN_RECIPE, launch_key="slow-sibling")
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["split"])
    _reconcile(run["id"])
    branches = _attempts_by_box(run["id"])
    _complete_and_apply(run["id"], branches["alpha"])
    _complete_and_apply(run["id"], branches["beta"])

    waiting = _reconcile(run["id"])
    with store._connect() as conn:
        synthesis_count = conn.execute(
            """SELECT COUNT(*) FROM box_attempts_v1
               WHERE run_id=? AND box_id='synthesis'""",
            (run["id"],),
        ).fetchone()[0]
        pending_arrivals = conn.execute(
            """SELECT COUNT(*) FROM route_tokens_v1
               WHERE run_id=? AND destination_box_id='synthesis'
                 AND state='pending'""",
            (run["id"],),
        ).fetchone()[0]
        group_state = conn.execute(
            "SELECT state FROM split_groups_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    assert waiting == {"attempts_created": 0, "tokens_consumed": 0}
    assert synthesis_count == 0
    assert pending_arrivals == 2
    assert group_state == "open"

    _complete_and_apply(run["id"], branches["gamma"])
    ready = _reconcile(run["id"])
    assert ready == {"attempts_created": 1, "tokens_consumed": 3}


def test_conditional_sibling_routed_away_is_not_awaited_by_join(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=CONDITIONAL_JOIN_RECIPE,
        launch_key="conditional-routed-away",
    )
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["split"])
    _reconcile(run["id"])
    branches = _attempts_by_box(run["id"])
    _complete_and_apply(run["id"], branches["left"], work="Left output.")
    _complete_and_apply(run["id"], branches["middle"], work="Middle output.")

    waiting = _reconcile(run["id"])
    assert waiting == {"attempts_created": 0, "tokens_consumed": 0}

    _complete_and_apply(
        run["id"],
        branches["chooser"],
        result="away",
        work="Chooser routed away.",
    )
    ready = _reconcile(run["id"])

    with store._connect() as conn:
        synthesis = dict(conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE run_id=? AND box_id='synthesis'""",
            (run["id"],),
        ).fetchone())
        detour = dict(conn.execute(
            """SELECT * FROM box_attempts_v1
               WHERE run_id=? AND box_id='detour'""",
            (run["id"],),
        ).fetchone())
        arrival_tokens = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM route_tokens_v1
                   WHERE run_id=? AND destination_box_id='synthesis'
                   ORDER BY id""",
                (run["id"],),
            )
        ]
    assert ready == {"attempts_created": 2, "tokens_consumed": 3}
    assert synthesis["state"] == "ready"
    assert detour["state"] == "ready"
    assert json.loads(synthesis["input_work_json"])["activating_token_ids"] == [
        token["id"] for token in arrival_tokens
    ]


def test_join_can_start_from_one_active_arrival_and_ignores_backward_reachability(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=BACKWARD_REACHABILITY_RECIPE,
        launch_key="ignore-backward-reachability",
    )
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["split"])
    _reconcile(run["id"])
    branches = _attempts_by_box(run["id"])
    _complete_and_apply(run["id"], branches["left"], work="Left output.")
    _complete_and_apply(
        run["id"],
        branches["chooser"],
        result="away",
        work="Chooser routed to later.",
    )

    reconciled = _reconcile(run["id"])

    assert reconciled == {"attempts_created": 2, "tokens_consumed": 2}
    attempts = _attempts_by_box(run["id"])
    assert attempts["synthesis"]["state"] == "ready"
    assert attempts["later"]["state"] == "ready"
    synthesis_input = json.loads(attempts["synthesis"]["input_work_json"])
    assert len(synthesis_input["activating_token_ids"]) == 1
    assert synthesis_input["preceding_outputs"][-1] == {
        "attempt_id": branches["left"]["id"],
        "box_id": "left",
        "output_work": "Left output.",
    }


def test_restart_between_second_and_third_join_arrival_preserves_wait(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(recipe=THREE_WAY_JOIN_RECIPE, launch_key="join-restart")
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["split"])
    _reconcile(run["id"])
    branches = _attempts_by_box(run["id"])
    _complete_and_apply(run["id"], branches["alpha"])
    _complete_and_apply(run["id"], branches["beta"])
    assert _reconcile(run["id"]) == {
        "attempts_created": 0,
        "tokens_consumed": 0,
    }

    with store._connect() as restarted_conn:
        restarted = reconcile_run(restarted_conn, run["id"])
    assert restarted == {"attempts_created": 0, "tokens_consumed": 0}

    _complete_and_apply(run["id"], branches["gamma"])
    with store._connect() as restarted_conn:
        ready = reconcile_run(restarted_conn, run["id"])
    assert ready == {"attempts_created": 1, "tokens_consumed": 3}

    with store._connect() as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM box_attempts_v1
               WHERE run_id=? AND box_id='synthesis'""",
            (run["id"],),
        ).fetchone()[0] == 1


def test_nested_split_generations_join_and_collapse_without_mixing(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(recipe=NESTED_JOIN_RECIPE, launch_key="nested-generations")
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["outer-split"])
    _reconcile(run["id"])
    outer_attempts = _attempts_by_box(run["id"])
    _complete_and_apply(run["id"], outer_attempts["outer-left"])
    _reconcile(run["id"])
    nested_attempts = _attempts_by_box(run["id"])

    with store._connect() as conn:
        groups = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM split_groups_v1 WHERE run_id=?",
                (run["id"],),
            )
        ]
        outer_left_token = dict(conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND destination_box_id='outer-left'""",
            (run["id"],),
        ).fetchone())
        outer_right_token = dict(conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND destination_box_id='outer-right'""",
            (run["id"],),
        ).fetchone())
    assert len(groups) == 2
    outer_group = next(
        group for group in groups
        if json.loads(group["parent_lineage_json"]) == []
    )
    inner_group = next(group for group in groups if group != outer_group)
    outer_left_lineage = json.loads(outer_left_token["lineage_json"])
    outer_right_lineage = json.loads(outer_right_token["lineage_json"])
    assert outer_left_lineage[0]["split_group_id"] == outer_group["id"]
    assert outer_right_lineage[0]["split_group_id"] == outer_group["id"]
    assert outer_left_lineage[0]["branch_id"] != outer_right_lineage[0]["branch_id"]
    assert json.loads(inner_group["parent_lineage_json"]) == json.loads(
        outer_left_token["lineage_json"],
    )

    _complete_and_apply(run["id"], nested_attempts["inner-a"])
    _complete_and_apply(run["id"], nested_attempts["inner-b"])
    assert _reconcile(run["id"]) == {
        "attempts_created": 1,
        "tokens_consumed": 2,
    }
    after_inner = _attempts_by_box(run["id"])
    inner_join = after_inner["inner-join"]
    inner_ids = json.loads(inner_join["input_work_json"])["activating_token_ids"]
    with store._connect() as conn:
        inner_tokens = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM route_tokens_v1
                    WHERE id IN ({','.join('?' for _ in inner_ids)})""",
                inner_ids,
            )
        ]
        states = {
            row["id"]: row["state"]
            for row in conn.execute(
                "SELECT id,state FROM split_groups_v1 WHERE run_id=?",
                (run["id"],),
            )
        }
    assert {json.loads(token["lineage_json"])[-1]["split_group_id"]
            for token in inner_tokens} == {inner_group["id"]}
    assert states[inner_group["id"]] == "closed"
    assert states[outer_group["id"]] == "open"

    _complete_and_apply(run["id"], inner_join)
    _complete_and_apply(run["id"], outer_attempts["outer-right"])
    assert _reconcile(run["id"]) == {
        "attempts_created": 1,
        "tokens_consumed": 2,
    }

    outer_join = _attempts_by_box(run["id"])["outer-join"]
    outer_ids = json.loads(outer_join["input_work_json"])["activating_token_ids"]
    with store._connect() as conn:
        outer_tokens = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM route_tokens_v1
                    WHERE id IN ({','.join('?' for _ in outer_ids)})""",
                outer_ids,
            )
        ]
        group_states = {
            row["id"]: row["state"]
            for row in conn.execute(
                "SELECT id,state FROM split_groups_v1 WHERE run_id=?",
                (run["id"],),
            )
        }
    assert {json.loads(token["lineage_json"])[-1]["split_group_id"]
            for token in outer_tokens} == {outer_group["id"]}
    assert inner_group["id"] not in {
        frame["split_group_id"]
        for token in outer_tokens
        for frame in json.loads(token["lineage_json"])
    }
    assert group_states == {
        outer_group["id"]: "closed",
        inner_group["id"]: "closed",
    }


def test_nested_branches_can_join_directly_at_governing_ancestor(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(
        recipe=DIRECT_NESTED_ANCESTOR_JOIN_RECIPE,
        launch_key="direct-nested-ancestor-join",
    )
    _reconcile(run["id"])
    _complete_and_apply(run["id"], _attempts_by_box(run["id"])["outer-split"])
    _reconcile(run["id"])
    outer_attempts = _attempts_by_box(run["id"])
    _complete_and_apply(run["id"], outer_attempts["outer-left"])
    _reconcile(run["id"])
    nested_attempts = _attempts_by_box(run["id"])

    _complete_and_apply(run["id"], nested_attempts["inner-a"])
    _complete_and_apply(run["id"], outer_attempts["outer-right"])
    assert _reconcile(run["id"]) == {
        "attempts_created": 0,
        "tokens_consumed": 0,
    }

    _complete_and_apply(run["id"], nested_attempts["inner-b"])
    assert _reconcile(run["id"]) == {
        "attempts_created": 1,
        "tokens_consumed": 3,
    }
    ancestor_join = _attempts_by_box(run["id"])["ancestor-join"]
    activating_ids = json.loads(
        ancestor_join["input_work_json"],
    )["activating_token_ids"]
    with store._connect() as conn:
        arrivals = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM route_tokens_v1
                    WHERE id IN ({','.join('?' for _ in activating_ids)})""",
                activating_ids,
            )
        ]
        groups = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM split_groups_v1 WHERE run_id=?",
                (run["id"],),
            )
        ]
    outer_group = next(
        group for group in groups
        if json.loads(group["parent_lineage_json"]) == []
    )
    inner_group = next(group for group in groups if group != outer_group)
    assert {group["state"] for group in groups} == {"closed"}
    assert {
        json.loads(token["lineage_json"])[0]["split_group_id"]
        for token in arrivals
    } == {outer_group["id"]}
    assert sum(
        inner_group["id"] in {
            frame["split_group_id"]
            for frame in json.loads(token["lineage_json"])
        }
        for token in arrivals
    ) == 2

    _complete_and_apply(run["id"], ancestor_join)
    assert _reconcile(run["id"]) == {
        "attempts_created": 1,
        "tokens_consumed": 1,
    }
    with store._connect() as conn:
        end_token = dict(conn.execute(
            """SELECT * FROM route_tokens_v1
               WHERE run_id=? AND destination_box_id='end'""",
            (run["id"],),
        ).fetchone())
    assert json.loads(end_token["lineage_json"]) == []


def test_reconcile_failure_rolls_back_savepoint_not_caller_transaction(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="caller-owned-savepoint")
    original_insert = graph_runner._insert_ready_attempt

    def insert_then_fail(*args, **kwargs):
        original_insert(*args, **kwargs)
        raise RuntimeError("fail after reconcile write")

    monkeypatch.setattr(
        graph_runner,
        "_insert_ready_attempt",
        insert_then_fail,
    )
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE recipe_runs_v1 SET workspace_path=? WHERE id=?",
            ("/workspace/caller-owned", run["id"]),
        )
        with pytest.raises(RuntimeError, match="fail after reconcile write"):
            reconcile_run(conn, run["id"])

        assert conn.in_transaction
        assert conn.execute(
            "SELECT workspace_path FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone()[0] == "/workspace/caller-owned"
        assert conn.execute(
            "SELECT COUNT(*) FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT state FROM route_tokens_v1
               WHERE run_id=? AND destination_box_id=?""",
            (run["id"], RECIPE.start),
        ).fetchone()[0] == "pending"
        conn.commit()

    with store._connect() as conn:
        assert conn.execute(
            "SELECT workspace_path FROM recipe_runs_v1 WHERE id=?",
            (run["id"],),
        ).fetchone()[0] == "/workspace/caller-owned"
        assert conn.execute(
            "SELECT COUNT(*) FROM box_attempts_v1 WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0] == 0


def test_reconcile_commits_a_transaction_it_started_before_returning(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run = _start(launch_key="reconcile-owned-transaction")
    conn = store._connect()
    try:
        assert reconcile_run(conn, run["id"]) == {
            "attempts_created": 1,
            "tokens_consumed": 1,
        }
        assert conn.in_transaction is False
        with store._connect() as observer:
            assert observer.execute(
                "SELECT COUNT(*) FROM box_attempts_v1 WHERE run_id=?",
                (run["id"],),
            ).fetchone()[0] == 1
            assert observer.execute(
                """SELECT COUNT(*) FROM route_tokens_v1
                   WHERE run_id=? AND state='consumed'""",
                (run["id"],),
            ).fetchone()[0] == 1
    finally:
        conn.close()
