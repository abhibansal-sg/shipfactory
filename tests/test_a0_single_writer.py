"""A0 control-plane races and crash-boundary regressions."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Spawn-child import determinism: multiprocessing children re-execute this
# module. If the checkout's PARENT directory appears on the child's sys.path
# and the repo directory is itself named 'shipfactory' (it is, on the main
# checkout), `import shipfactory` resolves to the repo-root PLUGIN SHIM
# __init__.py instead of the inner package and dies on a circular import.
# Pin the repo root ahead of everything so the inner package always wins.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if sys.path[0] != _REPO_ROOT:
    if _REPO_ROOT in sys.path:
        sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)

from shipfactory import daemon, policy, store
from shipfactory.cli import _recipe_gate
from shipfactory.recipes import advancer
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
HERMES = Path.home() / "Developer/products/hermes-mobile"
PROFILES = {"standard": {"max_runtime_seconds": 1800, "max_retries": 2,
                         "token_allowance": 50_000}}


def _gate(tmp_path: Path, conn, instance_id: str):
    library = tmp_path / f"library-{instance_id}"
    library.mkdir()
    (library / "gate.yaml").write_text(
        f"""schema: shipfactory.recipe/v1
id: gate-{instance_id}
version: 1
status: active
description: gate race fixture
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


def _apply_child(home: str, kanban_path: str) -> None:
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes.advancer import apply_events
    conn = kanban_db.connect(Path(kanban_path))
    try:
        apply_events(conn, profiles=PROFILES, board="test")
    finally:
        conn.close()


def _consume_then_exit(home: str, kanban_path: str) -> None:
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer
    conn = kanban_db.connect(Path(kanban_path))
    try:
        row = child_advancer._claim_event(owner=f"kill:{os.getpid()}", board="test")
        assert row is not None
        child_advancer._apply_claimed_event(conn, row)
    finally:
        conn.close()
    os._exit(73)


def _complete_then_exit(home: str, kanban_path: str) -> None:
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer
    conn = kanban_db.connect(Path(kanban_path))
    child_advancer._record_action_outcome = lambda *args, **kwargs: os._exit(74)
    child_advancer.run_action_intents(
        conn, board="test", kinds={"approval_gate_completion"}, limit=1,
    )


def _slow_send_child(home: str, kanban_path: str, started) -> None:
    os.environ["HERMES_HOME"] = home
    from hermes_cli import kanban_db
    from shipfactory.recipes import advancer as child_advancer

    def slow_run(*args, **kwargs):
        started.set()
        time.sleep(5.5)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    child_advancer.subprocess.run = slow_run
    conn = kanban_db.connect(Path(kanban_path))
    try:
        child_advancer.run_action_intents(
            conn, board="test", kinds={"notification_delivery"}, limit=1,
        )
    finally:
        conn.close()


def test_schema_migration_is_atomic_checksummed_and_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    store.set_policy("kept", {"stages": []})
    with store._connect() as db:
        migration = dict(db.execute("SELECT * FROM schema_migrations").fetchone())
        assert migration["version"] == 1
        assert len(migration["checksum"]) == 64
        assert db.execute("SELECT COUNT(*) FROM action_intents").fetchone()[0] == 0
    store.init_db()
    assert store.get_policy("kept") == {"stages": []}

    with store._connect() as db:
        db.execute("UPDATE schema_migrations SET checksum='mismatch' WHERE version=1")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        store.init_db()


def test_schema_migration_rejects_partial_application(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store.init_db()
    with store._connect() as db:
        db.execute("DELETE FROM schema_migrations WHERE version=1")
    with pytest.raises(RuntimeError, match="partially applied"):
        store.init_db()


def test_two_process_event_race_has_one_intent_and_one_completion(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "race")
    key = advancer.gate_decision("race", "approve", "approve")
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_apply_child, args=(home, kanban_path)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    verify = kanban_db.connect(Path(kanban_path))
    try:
        assert kanban_db.get_task(verify, gate["kanban_task_id"]).status == "done"
        completed = [item for item in kanban_db.list_events(verify, gate["kanban_task_id"])
                     if item.kind == "completed"]
        assert len(completed) == 1
    finally:
        verify.close()
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM action_intents WHERE kind='approval_gate_completion'"
        ).fetchone()[0] == 1
        event_row = db.execute("SELECT state FROM advance_events WHERE key=?", (key,)).fetchone()
        assert event_row["state"] == "applied"


def test_cli_approval_is_enqueue_only_until_daemon_tick(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "queued")
    result = _recipe_gate(None, "queued", "approve", "approve", "")
    assert result["status"] == "waiting_gate"
    assert result["decision_id"]
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "blocked"
    advancer.apply_events(kanban_conn, profiles=PROFILES, board="test")
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"


def test_restart_after_intent_before_effect_performs_effect_once(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "before-effect")
    event_key = advancer.gate_decision("before-effect", "approve", "approve")
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_consume_then_exit, args=(home, kanban_path))
    process.start(); process.join(20)
    assert process.exitcode == 73
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "blocked"
    with store._connect() as db:
        before = tuple(db.execute(
            "SELECT key,state,created_at FROM advance_events WHERE key=?", (event_key,)
        ).fetchone())
        assert db.execute("SELECT state FROM action_intents").fetchone()[0] == "planned"
    advancer.apply_events(kanban_conn, profiles=PROFILES, board="test")
    with store._connect() as db:
        after = tuple(db.execute(
            "SELECT key,state,created_at FROM advance_events WHERE key=?", (event_key,)
        ).fetchone())
    assert before == after
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"


def test_restart_after_effect_before_record_probes_without_duplicate(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    gate = _gate(tmp_path, kanban_conn, "after-effect")
    event_key = advancer.gate_decision("after-effect", "approve", "approve")
    row = advancer._claim_event(owner="parent-consumer", board="test")
    assert row and row["key"] == event_key
    advancer._apply_claimed_event(kanban_conn, row)
    home = os.environ["HERMES_HOME"]
    kanban_path = kanban_conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_complete_then_exit, args=(home, kanban_path))
    process.start(); process.join(20)
    assert process.exitcode == 74
    # Recovery is a FRESH PROCESS in production (daemon restart). The crashed
    # writer died via os._exit with a dangling WAL shm, and in-process readers
    # (even brand-new connections) cannot run WAL-index recovery while the
    # test process holds other handles to the file — they read a stale index
    # and see the pre-crash 'blocked'. A new OS process recovers the WAL and
    # sees the committed effect; assert through one, exactly like a restarted
    # daemon would observe the board.
    raw = subprocess.run(
        ["sqlite3", kanban_path,
         f"SELECT status FROM tasks WHERE id='{gate['kanban_task_id']}'"],
        capture_output=True, text=True, timeout=10,
    )
    assert raw.stdout.strip() == "done"
    recovered = kanban_db.connect(Path(kanban_path))
    assert kanban_db.get_task(recovered, gate["kanban_task_id"]).status == "done"
    with store._connect() as db:
        terminal_event = tuple(db.execute(
            "SELECT key,state,created_at FROM advance_events WHERE key=?", (event_key,)
        ).fetchone())
        db.execute(
            "UPDATE action_intents SET lease_until='1970-01-01T00:00:00+00:00' WHERE state='leased'"
        )
    assert advancer.run_action_intents(
        recovered, board="test", kinds={"approval_gate_completion"}, limit=1,
    ) == 1
    with store._connect() as db:
        assert tuple(db.execute(
            "SELECT key,state,created_at FROM advance_events WHERE key=?", (event_key,)
        ).fetchone()) == terminal_event
        assert db.execute(
            "SELECT COUNT(*) FROM action_intents WHERE logical_key=(SELECT logical_key FROM action_intents LIMIT 1)"
        ).fetchone()[0] == 2
    completed = [item for item in kanban_db.list_events(recovered, gate["kanban_task_id"])
                 if item.kind == "completed"]
    assert len(completed) == 1
    recovered.close()


def test_stale_gate_activation_is_discarded_and_cannot_complete_new_gate(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    _gate(tmp_path, kanban_conn, "stale")
    key = advancer.gate_decision("stale", "approve", "approve")
    task2 = kanban_db.create_blocked_task(
        kanban_conn, title="Approve activation two", body="new gate",
        block_kind="needs_input", reason="approval_required",
        idempotency_key="stale-activation-two", board="test",
    )
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,kanban_task_id,"
            "created_at,updated_at) VALUES('stale','approve',2,'approval_gate','waiting',?,?,?)",
            (task2, now, now),
        )
    advancer.apply_events(kanban_conn, profiles=PROFILES, board="test")
    assert kanban_db.get_task(kanban_conn, task2).status == "blocked"
    with store._connect() as db:
        event_row = db.execute(
            "SELECT state,outcome FROM advance_events WHERE key=?", (key,)
        ).fetchone()
        assert tuple(event_row) == ("discarded", "stale_or_nonmatching_activation")
        assert db.execute("SELECT COUNT(*) FROM action_intents").fetchone()[0] == 0


def test_root_completion_false_records_no_success_and_fresh_attempt(tmp_path, kanban_conn, monkeypatch):
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
            ("root-false", root, "test", "[]", "selected", root, now, now),
        )
    monkeypatch.setattr(kanban_db, "complete_task", lambda *args, **kwargs: False)
    assert advancer.reconcile_root_collectors(kanban_conn, board="test") == 0
    with store._connect() as db:
        states = [tuple(row) for row in db.execute(
            "SELECT attempt,state FROM action_intents ORDER BY attempt"
        )]
    assert states == [(1, "retryable_failed"), (2, "planned")]
    assert kanban_db.get_task(kanban_conn, root).status != "done"


def test_slow_send_holds_no_factory_write_transaction(tmp_path, kanban_conn):
    key = "slow-notification"
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
    process = ctx.Process(target=_slow_send_child, args=(home, kanban_path, started))
    process.start()
    assert started.wait(10)
    before = time.monotonic()
    store.set_policy("concurrent-writer", {"stages": []})
    elapsed = time.monotonic() - before
    process.join(15)
    assert process.exitcode == 0
    assert elapsed < 2.0
    assert store.get_policy("concurrent-writer") == {"stages": []}


@pytest.mark.parametrize("seats_text", [None, "company: test\nrecipes: [invalid]\n"])
def test_require_recipes_missing_or_invalid_config_opens_no_board(tmp_path, seats_text):
    if seats_text is not None:
        state = tmp_path / "shipfactory"
        state.mkdir(parents=True)
        (state / "seats.yaml").write_text(seats_text, encoding="utf-8")
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


def test_recipe_policy_db_error_never_mutates_legacy_policy(monkeypatch):
    class BrokenConnection:
        def __enter__(self):
            raise sqlite3.OperationalError("factory unavailable")
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(store, "_connect", lambda: BrokenConnection())
    monkeypatch.setattr(
        "shipfactory.config.load_seats",
        lambda: type("Config", (), {"recipes": {"enabled": True}})(),
    )
    mutations = []
    monkeypatch.setattr(policy, "_reopen", lambda *args: mutations.append(args))
    with pytest.raises(RuntimeError, match="legacy policy is fenced"):
        policy.on_complete("task", "test", "worker", "done")
    assert mutations == []


def test_second_daemon_process_exits_before_opening_its_board(tmp_path):
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
        second = subprocess.run(
            [sys.executable, str(ROOT / "shipfactory" / "cli.py"), "daemon",
             "--board", "second", "--once"],
            text=True, capture_output=True, env=env, timeout=10,
        )
        assert second.returncode != 0
        assert "already running" in second.stderr
        board_paths = list(tmp_path.rglob("kanban.db"))
        assert not any("second" in path.parts for path in board_paths)
    finally:
        first.terminate()
        try:
            first.wait(10)
        except subprocess.TimeoutExpired:
            first.kill(); first.wait(5)


def test_run_action_intents_refuses_dirty_connection(kanban_conn):
    """Finding: the effect-boundary commit must never flush a caller's
    unrelated open write transaction — a dirty connection is refused."""
    kanban_conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="transaction-clean"):
            advancer.run_action_intents(kanban_conn)
    finally:
        kanban_conn.rollback()


def test_unreadable_recipe_config_fails_closed(monkeypatch):
    """Finding: a load_seats() failure other than 'unconfigured' must fence
    legacy policy (fail closed), not silently read as recipes-disabled."""
    class BrokenConnection:
        def __enter__(self):
            raise sqlite3.OperationalError("factory unavailable")
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(store, "_connect", lambda: BrokenConnection())

    def _unreadable():
        raise PermissionError("seats.yaml unreadable")

    monkeypatch.setattr("shipfactory.config.load_seats", _unreadable)
    mutations = []
    monkeypatch.setattr(policy, "_reopen", lambda *args: mutations.append(args))
    with pytest.raises(RuntimeError, match="legacy policy is fenced"):
        policy.on_complete("task", "test", "worker", "done")
    assert mutations == []
