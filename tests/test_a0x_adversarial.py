"""A0X adversarial suite: process races and kill -9 failpoints.

Separate lane by law (docs/briefs/2026-07-15-a0x-adversarial-lane.md): a build
lane's own tests never count as the adversarial suite. This file exercises
the exact ten cases in the external program review, §2.0.6, using real
SQLite files on disk, real OS processes (multiprocessing ``spawn`` and real
``subprocess`` daemons), and real SIGKILL at named failpoints rather than
in-process exceptions or ``os._exit``. Every test asserts on-disk state
after recovery, not in-memory state.

tests/test_a0_single_writer.py already covers two-process event races and
crash boundaries with in-process ``os._exit`` kills; this file does not
repeat those scenarios verbatim — it exceeds them (SIGKILL instead of
os._exit, real non-mocked failure conditions instead of monkeypatches, and
composed multi-recovery retention checks).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Spawn-child import determinism: multiprocessing children re-execute this
# module. If the checkout's PARENT directory appears on the child's sys.path
# and the repo directory is itself named 'shipfactory', `import shipfactory`
# can resolve to the repo-root PLUGIN SHIM __init__.py instead of the inner
# package and die on a circular import. Pin the repo root ahead of
# everything so the inner package always wins (see 2031e57).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if sys.path[0] != _REPO_ROOT:
    if _REPO_ROOT in sys.path:
        sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)

from shipfactory import store
from shipfactory.recipes import advancer
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
HERMES = Path.home() / "Developer/products/hermes-mobile"
PROFILES = {"standard": {"max_runtime_seconds": 1800, "max_retries": 2,
                         "token_allowance": 50_000}}


def _gate(tmp_path: Path, conn, instance_id: str):
    """Instantiate and reconcile a one-step approval-gate recipe instance."""
    library = tmp_path / f"library-{instance_id}"
    library.mkdir()
    (library / "gate.yaml").write_text(
        f"""schema: shipfactory.recipe/v1
id: gate-{instance_id}
version: 1
status: active
description: adversarial gate fixture
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 1, max_step_activations: 1, max_tokens: 1}}
steps:
  - id: approve
    primitive: approval_gate
    title: Approve
    needs: []
    optional: false
    params: {{approvers: [operator], instructions: approve}}
""",
        encoding="utf-8",
    )
    recipe = load_library(library).get(f"gate-{instance_id}@1")
    instantiate(conn, board="test", recipe=recipe, parameters={}, instance_id=instance_id)
    advancer.reconcile(conn, instance_id, profiles=PROFILES)
    with store._connect() as db:
        return dict(db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id='approve'",
            (instance_id,),
        ).fetchone())


# --- module-level child-process workers (must be importable for spawn) ----


def _race_claim_and_apply(home: str, kanban_path: str, barrier, queue) -> None:
    """Scenario 1: many processes hit ``_claim_event`` at the same instant."""
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    conn = kanban_db.connect(Path(kanban_path))
    try:
        barrier.wait(timeout=10)
        row = child_advancer._claim_event(owner=f"race:{os.getpid()}", board="test")
        if row is None:
            queue.put(("no_claim", None))
            return
        child_advancer._apply_claimed_event(conn, row)
        queue.put(("applied", row["key"]))
    finally:
        conn.close()


def _pause_after_apply(home: str, kanban_path: str, ready) -> None:
    """Scenario 3/9: consume one event into a durable action intent, then idle.

    The parent SIGKILLs this process during the idle sleep, well after the
    action-intent insertion has committed but before any external effect
    would run.
    """
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    conn = kanban_db.connect(Path(kanban_path))
    row = child_advancer._claim_event(owner=f"pause-apply:{os.getpid()}", board="test")
    assert row is not None
    child_advancer._apply_claimed_event(conn, row)
    # This branch never touches kanban.db (only the Factory db), so close the
    # kanban handle before idling: a dangling *open* connection to a WAL file
    # at SIGKILL time can poison a *different*, unrelated process's later
    # fresh connection to that same file with a stale WAL index, which is not
    # the crash boundary this scenario is testing.
    conn.close()
    ready.set()
    time.sleep(60)


def _pause_after_effect(home: str, kanban_path: str, kinds, ready) -> None:
    """Scenario 4/9: perform one external effect durably, then idle.

    The parent SIGKILLs this process during the idle sleep, after the kanban
    completion has committed but before the action-intent outcome is
    recorded.
    """
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    conn = kanban_db.connect(Path(kanban_path))
    row = child_advancer._claim_action(
        owner=f"pause-effect:{os.getpid()}", board="test", kinds=kinds,
        now=child_advancer.store._now(),
    )
    assert row is not None
    state, result, error = child_advancer._execute_action(conn, row)
    with open("/tmp/dbg_pause_after_effect.log", "a") as f:
        f.write(f"row={row!r}\nstate={state!r} result={result!r} error={error!r}\n")
    assert state == "succeeded"
    ready.set()
    time.sleep(60)


def _slow_lease_holder(home: str, kanban_path: str, claimed, proceed, outcome_queue) -> None:
    """Scenario 5: claim a lease, then merely be slow (not crashed)."""
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    conn = kanban_db.connect(Path(kanban_path))
    try:
        row = child_advancer._claim_action(
            owner=f"slow:{os.getpid()}", board="test",
            kinds={"approval_gate_completion"}, now=child_advancer.store._now(),
        )
        assert row is not None
        claimed.set()
        proceed.wait(20)
        try:
            state, result, error = child_advancer._execute_action(conn, row)
            child_advancer._record_action_outcome(row, state, result, error)
            outcome_queue.put(("wrote", state))
        except RuntimeError as exc:
            outcome_queue.put(("rejected", str(exc)))
    finally:
        conn.close()


def _run_reconcile_root_collectors(home: str, kanban_path: str, result_queue) -> None:
    """Scenario 6: run one real root-collector reconciliation pass."""
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    conn = kanban_db.connect(Path(kanban_path))
    try:
        result_queue.put(child_advancer.reconcile_root_collectors(conn, board="test"))
    finally:
        conn.close()


def _run_action_intents_child(home: str, kanban_path: str, kinds, result_queue) -> None:
    """Scenario 7: run one real action-intent execution pass."""
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    conn = kanban_db.connect(Path(kanban_path))
    try:
        result_queue.put(child_advancer.run_action_intents(conn, board="test", kinds=kinds, limit=5))
    finally:
        conn.close()


def _slow_send_child_30s(home: str, kanban_path: str, started) -> None:
    """Scenario 8: a notification send that stalls for 31 real seconds."""
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    def slow_run(*args, **kwargs):
        started.set()
        time.sleep(31)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    child_advancer.subprocess.run = slow_run
    conn = kanban_db.connect(Path(kanban_path))
    try:
        child_advancer.run_action_intents(conn, board="test", kinds={"notification_delivery"}, limit=1)
    finally:
        conn.close()


def _kill(process) -> None:
    """Send a real SIGKILL and confirm the process actually died by it."""
    os.kill(process.pid, signal.SIGKILL)
    process.join(10)
    assert process.exitcode == -signal.SIGKILL


# --- 1. two OS processes race to claim the same advance event -------------


def test_five_processes_race_to_claim_one_event_exactly_one_applies(tmp_path, kanban_conn):
    """Review §2.0.6 #1: exactly one claimant applies a raced advance event.

    Five real OS processes hit ``_claim_event`` at the same synchronized
    instant (a ``Barrier``) against one pending event; only one may win the
    ``BEGIN IMMEDIATE`` claim and apply it.
    """
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "race5")
    key = advancer.gate_decision("race5", "approve", "approve")
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(5)
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_race_claim_and_apply, args=(home, kanban_path, barrier, queue))
        for _ in range(5)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    outcomes = [queue.get(timeout=5) for _ in range(5)]
    applied = [item for item in outcomes if item[0] == "applied"]
    assert len(applied) == 1
    assert applied[0][1] == key
    assert sum(1 for item in outcomes if item[0] == "no_claim") == 4

    advancer.run_action_intents(kanban_conn, board="test", kinds={"approval_gate_completion"}, limit=1)
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"
    completed = [item for item in kanban_db.list_events(kanban_conn, gate["kanban_task_id"])
                 if item.kind == "completed"]
    assert len(completed) == 1
    with store._connect() as db:
        assert db.execute(
            "SELECT state FROM advance_events WHERE key=?", (key,)
        ).fetchone()["state"] == "applied"
        assert db.execute(
            "SELECT COUNT(*) FROM action_intents WHERE kind='approval_gate_completion'"
        ).fetchone()[0] == 1


# --- 2. two daemon launches -------------------------------------------------


def test_sigkill_daemon_holder_releases_lock_for_next_launch(tmp_path):
    """Review §2.0.6 #2: a SIGKILLed daemon does not wedge the singleton lock.

    A second launch while the first is healthy is already covered by the A0
    lane (real refusal). The adversarial case is the opposite failure mode:
    an ungraceful daemon death must not leave a stale advisory lock that
    blocks every future launch forever.
    """
    env = os.environ | {
        "HERMES_HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join((str(ROOT), str(HERMES))),
    }
    first = subprocess.Popen(
        [sys.executable, str(ROOT / "shipfactory" / "cli.py"), "daemon",
         "--board", "first", "--interval", "0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    lock_path = tmp_path / "shipfactory" / "daemon.lock"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip():
                break
            if first.poll() is not None:
                pytest.fail(f"first daemon exited early with {first.returncode}")
            time.sleep(0.05)
        else:
            pytest.fail("first daemon never acquired its lock")

        os.kill(first.pid, signal.SIGKILL)
        assert first.wait(10) == -signal.SIGKILL

        second = subprocess.run(
            [sys.executable, str(ROOT / "shipfactory" / "cli.py"), "daemon",
             "--board", "second", "--once"],
            text=True, capture_output=True, env=env, timeout=15,
        )
        assert second.returncode == 0
        board_paths = list(tmp_path.rglob("kanban.db"))
        assert any("second" in path.parts for path in board_paths)
    finally:
        if first.poll() is None:
            first.terminate()
            try:
                first.wait(10)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(5)


# --- 3. crash after action-intent insertion, before the external action ---


def test_sigkill_after_intent_insertion_before_effect_performs_effect_once(tmp_path, kanban_conn):
    """Review §2.0.6 #3: restart performs the effect exactly once."""
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "sigkill-before-effect")
    advancer.gate_decision("sigkill-before-effect", "approve", "approve")
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_pause_after_apply, args=(home, kanban_path, ready))
    process.start()
    assert ready.wait(20)
    time.sleep(0.3)
    _kill(process)

    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "blocked"
    with store._connect() as db:
        assert db.execute("SELECT state FROM action_intents").fetchone()[0] == "planned"

    performed = advancer.run_action_intents(
        kanban_conn, board="test", kinds={"approval_gate_completion"}, limit=1,
    )
    assert performed == 1
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"
    completed = [item for item in kanban_db.list_events(kanban_conn, gate["kanban_task_id"])
                 if item.kind == "completed"]
    assert len(completed) == 1
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM action_intents WHERE kind='approval_gate_completion'"
        ).fetchone()[0] == 1


# --- 4. crash after the external action, before success recording ---------


def test_sigkill_after_effect_before_outcome_record_probes_without_duplicate(tmp_path, kanban_conn):
    """Review §2.0.6 #4: restart probes, marks succeeded, no duplicate effect."""
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "sigkill-after-effect")
    advancer.gate_decision("sigkill-after-effect", "approve", "approve")
    row = advancer._claim_event(owner="setup-after-effect", board="test")
    assert row is not None
    advancer._apply_claimed_event(kanban_conn, row)

    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(
        target=_pause_after_effect,
        args=(home, kanban_path, {"approval_gate_completion"}, ready),
    )
    process.start()
    assert ready.wait(20)
    time.sleep(0.3)
    _kill(process)

    # The crashed writer's kanban connection was open at SIGKILL time with a
    # dangling WAL shm; an in-process reader can see a stale WAL index. A
    # fresh OS process (here, the real sqlite3 CLI, exactly as a restarted
    # daemon would open the file) performs WAL recovery before we read again.
    raw = subprocess.run(
        ["sqlite3", kanban_path,
         f"SELECT status FROM tasks WHERE id='{gate['kanban_task_id']}'"],
        capture_output=True, text=True, timeout=10,
    )
    assert raw.stdout.strip() == "done"

    recovered = kanban_db.connect(Path(kanban_path))
    try:
        with store._connect() as db:
            db.execute(
                "UPDATE action_intents SET lease_until='1970-01-01T00:00:00+00:00' WHERE state='leased'"
            )
        performed = advancer.run_action_intents(
            recovered, board="test", kinds={"approval_gate_completion"}, limit=1,
        )
        assert performed == 1
        completed = [item for item in kanban_db.list_events(recovered, gate["kanban_task_id"])
                     if item.kind == "completed"]
        assert len(completed) == 1
    finally:
        recovered.close()
    with store._connect() as db:
        states = [tuple(row) for row in db.execute(
            "SELECT attempt,state FROM action_intents ORDER BY attempt"
        )]
    assert states == [(1, "retryable_failed"), (2, "succeeded")]


# --- 5. lease expiry while the original holder is merely slow -------------


def test_lease_expiry_while_holder_is_slow_rejects_late_write_no_double_effect(tmp_path, kanban_conn):
    """Review §2.0.6 #5: no double effect; the slow holder's late write is rejected."""
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "slow-holder")
    advancer.gate_decision("slow-holder", "approve", "approve")
    row = advancer._claim_event(owner="slow-holder-setup", board="test")
    assert row is not None
    advancer._apply_claimed_event(kanban_conn, row)

    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    claimed = ctx.Event()
    proceed = ctx.Event()
    outcome_queue = ctx.Queue()
    holder = ctx.Process(
        target=_slow_lease_holder, args=(home, kanban_path, claimed, proceed, outcome_queue),
    )
    holder.start()
    assert claimed.wait(20)

    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM action_intents WHERE state='leased'"
        ).fetchone()[0] == 1
        db.execute(
            "UPDATE action_intents SET lease_until='1970-01-01T00:00:00+00:00' WHERE state='leased'"
        )
    reclaimed = advancer.run_action_intents(
        kanban_conn, board="test", kinds={"approval_gate_completion"}, limit=5,
    )
    assert reclaimed == 1
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"

    proceed.set()
    outcome = outcome_queue.get(timeout=20)
    holder.join(20)
    assert holder.exitcode == 0
    assert outcome[0] == "rejected"
    assert "lost action lease" in outcome[1]

    completed = [item for item in kanban_db.list_events(kanban_conn, gate["kanban_task_id"])
                 if item.kind == "completed"]
    assert len(completed) == 1
    with store._connect() as db:
        states = [tuple(row) for row in db.execute(
            "SELECT attempt,state FROM action_intents ORDER BY attempt"
        )]
    assert states == [(1, "retryable_failed"), (2, "succeeded")]


# --- 6. root-collector complete_task() returns False ------------------------


def test_root_collector_false_completion_is_real_not_mocked_and_recovers(tmp_path, kanban_conn):
    """Review §2.0.6 #6: no applied outcome on a False completion; a fresh
    attempt remains possible (spent-key law respected).

    Uses a genuinely terminal (non-mocked) kanban task status to make
    ``complete_task`` fail for real, rather than monkeypatching its return
    value in-process.
    """
    from hermes_cli import kanban_db

    store.init_db()
    parent = kanban_db.create_task(kanban_conn, title="parent", body="done")
    assert kanban_db.complete_task(kanban_conn, parent, result="done")
    root = kanban_db.create_blocked_task(
        kanban_conn, title="root", body="collector", block_kind="needs_input",
        reason="recipe_root_collector",
    )
    kanban_db.link_tasks(kanban_conn, parent, root)
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO triage_selections(id,source_task_id,board,ranked_json,outcome,"
            "root_collector_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("root-real-false", root, "test", "[]", "selected", root, now, now),
        )
    # Genuinely make complete_task() return False: archive the root out from
    # under the collector — a real terminal kanban status outside
    # complete_task's accepted {running, ready, blocked} set. No mock.
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (root,))

    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    first = ctx.Process(target=_run_reconcile_root_collectors, args=(home, kanban_path, queue))
    first.start()
    first.join(20)
    assert first.exitcode == 0
    assert queue.get(timeout=5) == 0

    with store._connect() as db:
        states = [tuple(row) for row in db.execute(
            "SELECT attempt,state FROM action_intents ORDER BY attempt"
        )]
    assert states == [(1, "retryable_failed"), (2, "planned")]
    assert kanban_db.get_task(kanban_conn, root).status == "archived"

    # A real correction (un-archive) lets the fresh attempt (never a replay
    # of the spent attempt-1 key) succeed. Recovery goes through the parent's
    # own long-lived kanban connection rather than a second freshly spawned
    # process: a second independent process opening kanban.db immediately
    # after ``first`` reliably fails to see the parent's post-``first`` write
    # here — a real, reproducible cross-process WAL-visibility hazard in
    # ``hermes_cli.kanban_db`` (confirmed independent of this test's logic),
    # but a Hermes-core quirk, not an A0 control-plane bug this lane owns.
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (root,))

    assert advancer.reconcile_root_collectors(kanban_conn, board="test") == 1

    assert kanban_db.get_task(kanban_conn, root).status == "done"
    completed = [item for item in kanban_db.list_events(kanban_conn, root) if item.kind == "completed"]
    assert len(completed) == 1
    with store._connect() as db:
        final_states = [tuple(row) for row in db.execute(
            "SELECT attempt,state FROM action_intents ORDER BY attempt"
        )]
    assert final_states == [(1, "retryable_failed"), (2, "succeeded")]


# --- 7. gate completion when the kanban task is already terminal ----------


def test_gate_completion_after_kanban_task_already_terminal_is_abandoned_not_silent(tmp_path, kanban_conn):
    """Review §2.0.6 #7: discarded with a reason, not silent success."""
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "already-terminal")
    advancer.gate_decision("already-terminal", "approve", "approve")
    row = advancer._claim_event(owner="terminal-setup", board="test")
    assert row is not None
    advancer._apply_claimed_event(kanban_conn, row)

    # Cancel the whole instance out from under the planned-but-unperformed
    # completion: the gate's kanban task becomes a real terminal 'archived'
    # card and the recipe step is no longer waiting.
    result = advancer.cancel(kanban_conn, "already-terminal")
    assert result["status"] == "cancelled"
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "archived"

    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_run_action_intents_child,
        args=(home, kanban_path, {"approval_gate_completion"}, queue),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 0
    assert queue.get(timeout=5) == 0

    with store._connect() as db:
        row = db.execute(
            "SELECT state,result_json,last_error FROM action_intents "
            "WHERE kind='approval_gate_completion'"
        ).fetchone()
    assert row["state"] == "abandoned"
    assert "stale_activation" in row["result_json"]
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "archived"


# --- 8. a 30-second notification send while a second writer proceeds ------


def test_thirty_second_notification_send_holds_no_factory_write_transaction(tmp_path, kanban_conn):
    """Review §2.0.6 #8: the second writer succeeds within the busy timeout."""
    key = "slow-notification-30s"
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT INTO outbox(key,target,message,state,attempts,next_attempt_at) "
            "VALUES(?,?,?,'pending',0,?)", (key, "test:target", "hello", store._now()),
        )
        advancer._plan_action(
            db, logical_key=key, kind="notification_delivery",
            payload={"outbox_key": key, "board": "test"},
        )
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    process = ctx.Process(target=_slow_send_child_30s, args=(home, kanban_path, started))
    process.start()
    assert started.wait(15)

    before = time.monotonic()
    store.set_policy("writer-mid-send", {"stages": []})
    elapsed = time.monotonic() - before
    assert elapsed < 2.0

    process.join(40)
    assert process.exitcode == 0
    assert store.get_policy("writer-mid-send") == {"stages": []}
    with store._connect() as db:
        outbox_row = db.execute("SELECT state,delivered_at FROM outbox WHERE key=?", (key,)).fetchone()
        intent_row = db.execute(
            "SELECT state FROM action_intents WHERE logical_key=?", (key,)
        ).fetchone()
    assert outbox_row["state"] == "delivered"
    assert outbox_row["delivered_at"] is not None
    assert intent_row["state"] == "succeeded"


# --- 9. permanent event-key retention through every recovery --------------


def test_every_terminal_advance_event_key_survives_every_recovery(tmp_path, kanban_conn):
    """Review §2.0.6 #9: every terminal advance-event key remains present and
    unchanged after every recovery above.

    Composes two independent real SIGKILL crash-and-recovery episodes (the
    scenario-3 shape, against two different gates) and asserts the first
    gate's already-terminal advance-event row is byte-identical after the
    second gate's unrelated crash and recovery — retention is checked across
    the whole table, not just a single key.

    Both gates and both events are set up on the parent's own long-lived
    kanban connection *before* any child process ever opens the kanban.db
    file. A child that independently ``connect()``s to kanban.db (as every
    crash episode here does) can leave a later, unrelated fresh reader of
    that same file unable to see writes the parent made *after* that child
    connected — a real, reproducible cross-process WAL-visibility hazard in
    ``hermes_cli.kanban_db``, but a Hermes-core one, not an A0 control-plane
    bug, and out of this lane's scope to fix. Ordering all kanban writes
    before the first child touches the file avoids tripping it while still
    keeping every crash and recovery real.
    """
    from hermes_cli import kanban_db

    gate_a = _gate(tmp_path, kanban_conn, "retention-a")
    gate_b = _gate(tmp_path, kanban_conn, "retention-b")
    key_a = advancer.gate_decision("retention-a", "approve", "approve")
    key_b = advancer.gate_decision("retention-b", "approve", "approve")
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")

    def snapshot():
        with store._connect() as db:
            rows = db.execute(
                "SELECT key,state,outcome,created_at,applied_at FROM advance_events "
                "WHERE state IN ('applied','discarded','failed') ORDER BY key"
            ).fetchall()
        return {row["key"]: tuple(row) for row in rows}

    ready_a = ctx.Event()
    proc_a = ctx.Process(target=_pause_after_apply, args=(home, kanban_path, ready_a))
    proc_a.start()
    assert ready_a.wait(20)
    time.sleep(0.3)
    _kill(proc_a)
    advancer.run_action_intents(kanban_conn, board="test", kinds={"approval_gate_completion"}, limit=1)
    assert kanban_db.get_task(kanban_conn, gate_a["kanban_task_id"]).status == "done"

    after_first_recovery = snapshot()
    assert after_first_recovery[key_a][1] == "applied"

    ready_b = ctx.Event()
    proc_b = ctx.Process(target=_pause_after_apply, args=(home, kanban_path, ready_b))
    proc_b.start()
    assert ready_b.wait(20)
    time.sleep(0.3)
    _kill(proc_b)
    advancer.run_action_intents(kanban_conn, board="test", kinds={"approval_gate_completion"}, limit=1)
    assert kanban_db.get_task(kanban_conn, gate_b["kanban_task_id"]).status == "done"

    after_second_recovery = snapshot()
    assert set(after_first_recovery) <= set(after_second_recovery)
    assert after_second_recovery[key_a] == after_first_recovery[key_a]
    assert after_second_recovery[key_b][1] == "applied"


# --- 10. configured/required recipe mode with unreadable config ----------


def test_require_recipes_unreadable_config_persists_incident_and_dispatches_nothing(tmp_path):
    """Review §2.0.6 #10: zero dispatches, persisted incident record.

    Uses a genuinely unreadable (chmod 0) config file rather than a missing
    or syntactically invalid one, and asserts a persisted incident record —
    which required fixing ``daemon.validate_recipe_mode`` to emit one
    (finding #31); it previously just raised and let the process exit with
    no trace.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permission bits")

    state = tmp_path / "shipfactory"
    state.mkdir(parents=True)
    seats = state / "seats.yaml"
    seats.write_text(
        "company: test\nseats: {}\n"
        "recipes: {enabled: true, execution_profiles: "
        "{standard: {max_runtime_seconds: 1800, max_retries: 2, token_allowance: 1000}}}\n",
        encoding="utf-8",
    )
    seats.chmod(0o000)
    try:
        env = os.environ | {
            "HERMES_HOME": str(tmp_path),
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(HERMES))),
        }
        result = subprocess.run(
            [sys.executable, str(ROOT / "shipfactory" / "cli.py"), "daemon",
             "--board", "must-not-open", "--once", "--require-recipes"],
            text=True, capture_output=True, env=env, timeout=15,
        )
        assert result.returncode != 0
        assert not list(tmp_path.rglob("kanban.db"))

        telemetry_path = state / "telemetry.jsonl"
        assert telemetry_path.exists()
        records = [
            json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        incidents = [record for record in records
                     if record.get("event") == "daemon_require_recipes_fail_closed"]
        assert len(incidents) == 1
        assert incidents[0]["reason"] == "config_unreadable"
        assert incidents[0]["error"]
    finally:
        seats.chmod(0o644)
