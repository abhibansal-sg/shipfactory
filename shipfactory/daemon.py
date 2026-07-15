"""The Factory dispatch daemon."""

from __future__ import annotations

import fcntl
import inspect
import json
import os
import logging
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

# Low-cadence board-database maintenance. Tests may lower the cadence without
# changing the public daemon interface.
_DB_HEALTH_EVERY_TICKS = 60
_db_health_tick = 0


def _process_start_identity() -> str:
    """Return an OS-derived process-start identity robust to PID reuse."""
    try:
        return subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())],
            text=True, timeout=2,
        ).strip()
    except Exception:
        # The lock itself remains authoritative. This fallback is unique to
        # this process launch even on minimal systems without ps(1).
        return f"python-start:{time.time_ns()}"


@contextmanager
def daemon_lock(boards: Sequence[str]):
    """Hold the process-wide ShipFactory daemon advisory lock."""
    from shipfactory import store

    path = store._db_path().parent / "daemon.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown owner"
            raise RuntimeError(f"ShipFactory daemon already running: {owner}") from exc
        record = {
            "pid": os.getpid(),
            "process_start_identity": _process_start_identity(),
            "boards": list(boards),
            "executable": str(Path(sys.executable).resolve()),
        }
        handle.seek(0)
        handle.truncate()
        json.dump(record, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _incidents_dir() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "shipfactory" / "incidents"


def _write_incident_fallback(record: dict[str, Any]) -> None:
    """Durable last-resort incident record when telemetry itself is unwritable.

    Written atomically (temp file + ``os.replace``) so a crash mid-write
    never leaves a half-written incident file behind.
    """
    incidents_dir = _incidents_dir()
    incidents_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = incidents_dir / f"{stamp}-{os.getpid()}.json"
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def _record_require_recipes_incident(reason: str, error: Exception | None) -> None:
    """Persist a fail-closed require-recipes abort (finding #31).

    A ``--require-recipes`` startup abort previously just raised and let the
    process exit nonzero — the failure left no trace once the crashed
    process's stderr scrolled away. Review §2.0.6 and AGENTS.md A0-7 both
    require a persisted incident for every fail-closed path, not merely a
    nonzero exit code.

    Cross-lab review (finding #4): the original version of this function
    caught a telemetry-write failure and only logged it, so the abort could
    still leave no durable record at all — the exact gap this function
    exists to close. On a real telemetry-write failure, fall back to an
    atomic incident file under ``$HERMES_HOME/shipfactory/incidents/``.
    Only if *both* persistence paths fail do we give up, and even then we
    write to stderr so the failure is not silent.
    """
    record = {
        "event": "daemon_require_recipes_fail_closed",
        "reason": reason,
        "error": str(error) if error is not None else None,
    }
    try:
        from shipfactory import telemetry

        telemetry.append_jsonl(record)
        return
    except Exception as telemetry_exc:
        # `except ... as name` deletes `name` once the block exits, so save
        # a repr now for use in the fallback branch below.
        telemetry_error = repr(telemetry_exc)
        logger.exception("Factory could not persist a require-recipes incident to telemetry")

    try:
        _write_incident_fallback(record)
    except Exception as fallback_exc:
        logger.critical(
            "Factory require-recipes incident lost: telemetry=%s fallback=%r",
            telemetry_error, fallback_exc,
        )
        print(
            "Factory fail-closed abort could not be persisted: "
            f"telemetry write failed ({telemetry_error}) and incident-file "
            f"fallback also failed ({fallback_exc!r})",
            file=sys.stderr,
        )


def validate_recipe_mode(*, required: bool = False) -> Any:
    """Load and validate recipe authority before any board is opened."""
    from shipfactory.config import FactoryConfigError, load_seats
    from shipfactory.recipes.advancer import startup_guard

    try:
        cfg = load_seats()
    except (ImportError, FileNotFoundError, OSError, FactoryConfigError) as exc:
        if required:
            _record_require_recipes_incident("config_unreadable", exc)
            raise RuntimeError(f"recipe configuration is required: {exc}") from exc
        return None
    recipes = cfg.recipes or {}
    if required and not recipes.get("enabled"):
        _record_require_recipes_incident("recipes_not_enabled", None)
        raise RuntimeError("recipe configuration is required but recipes.enabled is false")
    if recipes.get("enabled"):
        try:
            startup_guard(cfg)
        except Exception as exc:
            if required:
                _record_require_recipes_incident("startup_guard_failed", exc)
            raise
    return cfg


def _board_db_health_pass(conn, board: str | None) -> None:
    """Best-effort WAL checkpoint and integrity probe for the live board."""
    global _db_health_tick
    _db_health_tick += 1
    if _db_health_tick % _DB_HEALTH_EVERY_TICKS:
        return
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0] or 0):
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        rows = conn.execute("PRAGMA quick_check").fetchall()
        failures = [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]
        if failures:
            raise RuntimeError("PRAGMA quick_check: " + "; ".join(failures[:10]))
    except Exception as exc:
        logger.critical("Factory board database health pass failed for %s: %s", board, exc, exc_info=True)
        try:
            from shipfactory.telemetry import append_jsonl
            append_jsonl({
                "event": "database_health_failure",
                "at": time.time(),
                "board": board,
                "error": str(exc),
            })
        except Exception:
            logger.exception("Factory could not emit database-health telemetry")


def _requeue_capacity_claim(conn: Any, task: Any) -> bool:
    """Return a just-claimed Hermes task to its source queue without a failure."""
    from hermes_cli import kanban_db

    task_id = str(task.id)
    failure_row = conn.execute(
        "SELECT consecutive_failures,last_failure_error FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    source_status = "ready"
    task_attempt_id = getattr(task, "current_run_id", None)
    if task_attempt_id is not None:
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind='claimed' ORDER BY id DESC LIMIT 1",
            (task_id, int(task_attempt_id)),
        ).fetchone()
        if event and event["payload"]:
            try:
                if json.loads(event["payload"]).get("source_status") == "review":
                    source_status = "review"
            except (TypeError, json.JSONDecodeError):
                pass
    if not kanban_db.reclaim_task(
        conn, task_id, reason="worker_slot capacity unavailable",
    ):
        return False
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status=?,consecutive_failures=?,last_failure_error=? "
            "WHERE id=? AND status='ready'",
            (
                source_status,
                int(failure_row["consecutive_failures"]) if failure_row else 0,
                failure_row["last_failure_error"] if failure_row else None,
                task_id,
            ),
        )
    return True


def tick(conn, *, board: str | None = None, sync: bool = False,
         require_recipes: bool = False) -> dict[str, Any]:
    """Run one dispatch, reaping, watchdog, and optional GitHub-sync cycle."""
    from hermes_cli.kanban_db import dispatch_once
    import shipfactory.spawn as spawn_module

    shipfactory_spawn = spawn_module.shipfactory_spawn
    reap_finished = spawn_module.reap_finished
    capacity_signal = getattr(
        spawn_module, "WorkerCapacityExhausted",
        type("_NoCapacitySignal", (Exception,), {}),
    )

    dispatch_kwargs: dict[str, Any] = {}
    result_recipes = None
    result_selector = None
    result_environments = None
    cfg = validate_recipe_mode(required=require_recipes)
    max_workers = 2
    if cfg is not None:
        try:
            from shipfactory.config import recipe_runtime_config
            runtime_cfg = recipe_runtime_config(cfg.recipes)
        except ImportError:  # isolated compatibility stubs
            runtime_cfg = {"max_workers": 2}
        max_workers = int(runtime_cfg["max_workers"])
        restore = getattr(spawn_module, "restore_running", None)
        if restore is not None:
            restore(max_workers=max_workers)
        # Environment sessions (SF-8) are reaped every cycle like any other
        # supervised child — never run their bootstrap/app-up synchronously
        # here. Lane C modules are optional at plugin import time.
        try:
            from shipfactory import environments
            from shipfactory.config import environment_runtime_config
            env_cfg = environment_runtime_config(cfg.recipes)
            environments.restore_materializations()
            materializations = environments.reap_materializations(env_cfg)
            apps = environments.tick(env_cfg)
            result_environments = {"materializations": materializations, "apps": apps["events"]}
        except ImportError:
            result_environments = None
        recipes_cfg = cfg.recipes or {}
        if recipes_cfg.get("enabled"):
            from shipfactory.recipes.advancer import apply_events, deliver_outbox, reconcile_root_collectors
            dispatch_kwargs["max_in_progress"] = int(recipes_cfg["dispatcher_max_in_progress"])
            recipe_board = board or cfg.company
            event_kwargs = {
                "profiles": recipes_cfg["execution_profiles"],
                "board": recipe_board,
            }
            try:
                if "board_day_token_ceiling" in inspect.signature(apply_events).parameters:
                    event_kwargs["board_day_token_ceiling"] = int(
                        recipes_cfg.get("board_day_token_ceiling", 10**18)
                    )
            except (TypeError, ValueError):
                pass
            result_recipes = {
                "events": apply_events(conn, **event_kwargs),
                "outbox": deliver_outbox(conn, board=recipe_board),
                "root_collectors": reconcile_root_collectors(conn, board=recipe_board),
            }
            from shipfactory.config import selector_config
            if selector_config(recipes_cfg)["enabled"]:
                from shipfactory.recipes.selector_stage import run_stage
                result_selector = run_stage(conn, board or cfg.company)
            else:
                result_selector = {"leased": 0, "instantiated": 0, "parked": 0, "skipped": 0}
    # Finding #23 tick-order race: reap exited harnesses BEFORE dispatch_once,
    # whose claim watchdog otherwise sees a dead pid with an unfinalized task
    # and records a protocol violation — burning the failure fuse on workers
    # that completed perfectly. Reaping first finalizes their kanban state.
    reaped = reap_finished()
    if cfg is not None and hasattr(conn, "execute"):
        from shipfactory import store
        available = store.available_resource_units("worker_slot", max_workers)
        running = int(conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running'"
        ).fetchone()[0])
        dispatch_kwargs["max_spawn"] = running + available
    capacity_deferred: set[str] = set()

    def queue_safe_spawn(task, workspace: str, *, board=None):
        try:
            return shipfactory_spawn(task, workspace, board=board)
        except capacity_signal:
            try:
                if _requeue_capacity_claim(conn, task):
                    capacity_deferred.add(str(task.id))
            except Exception:
                # Capacity is a queue condition even when the best-effort
                # claim release itself needs reconciliation on the next tick.
                logger.exception("Factory could not requeue capacity-deferred task %s", task.id)
            return None

    dispatched = dispatch_once(
        conn, spawn_fn=queue_safe_spawn, board=board, **dispatch_kwargs,
    )
    if capacity_deferred:
        dispatched.spawned = [
            item for item in dispatched.spawned if item[0] not in capacity_deferred
        ]
        dispatched.respawn_guarded.extend(
            (task_id, "worker_slot_capacity")
            for task_id in sorted(capacity_deferred)
        )
    reaped += reap_finished()
    _board_db_health_pass(conn, board)
    result: dict[str, Any] = {"dispatch": dispatched, "reaped": reaped}
    if result_recipes is not None:
        result["recipes"] = result_recipes
    if result_selector is not None:
        result["selector"] = result_selector
    if result_environments is not None:
        result["environments"] = result_environments
    # Lane C modules are deliberately optional at plugin import time.
    try:
        from shipfactory import watchdog

        result["watchdog"] = watchdog.tick(conn, board=board)
    except ImportError:
        result["watchdog"] = None
    if sync:
        try:
            from shipfactory import github_sync

            result["sync"] = github_sync.tick(board=board)
        except ImportError:
            result["sync"] = None
        except Exception as exc:  # sync is best-effort: a misconfigured or
            # failing GitHub sync must never kill the dispatch/reap/watchdog
            # cycle (integration fix 07-12: real github_sync raises ValueError
            # when no repo is configured; Lane B's stub never did).
            result["sync"] = None
            result["sync_error"] = str(exc)
    return result


def _record_board_tick_failure(board: str, exc: Exception) -> None:
    """Log and emit best-effort telemetry for one isolated board failure."""
    logger.error("Factory daemon tick failed for board %s: %s", board, exc, exc_info=True)
    try:
        from shipfactory import store
        from shipfactory.telemetry import append_jsonl

        append_jsonl({
            "event": "daemon_board_tick_failure",
            "at": store._now(),
            "board": board,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
    except Exception:
        logger.exception("Factory could not emit board-tick failure telemetry")


def _served_boards(board: str | None, boards: Sequence[str] | None) -> list[str]:
    """Normalize the daemon's ordered, de-duplicated board set."""
    values = [str(value).strip() for value in (boards or ()) if str(value).strip()]
    if board is not None and str(board).strip():
        values.insert(0, str(board).strip())
    if not values:
        from hermes_cli.kanban_db import get_current_board
        values = [get_current_board()]
    return list(dict.fromkeys(values))


def run(conn, *, board: str | None = None, boards: Sequence[str] | None = None,
        interval: float = 5.0, once: bool = False, sync: bool = False,
        sync_interval: float | None = None, require_recipes: bool = False,
        _lock_held: bool = False) -> dict[str, Any] | None:
    """Run one tick loop across one or more isolated board connections.

    A traditional single-board caller continues to pass its connection in
    ``conn``.  Multi-board callers may pass a board-to-connection mapping, or
    ``None`` to let the daemon own and reconnect each board connection.
    """
    from shipfactory import store
    board_names = _served_boards(board, boards)
    if not _lock_held:
        with daemon_lock(board_names):
            return run(
                conn, board=board, boards=board_names, interval=interval, once=once,
                sync=sync, sync_interval=sync_interval,
                require_recipes=require_recipes, _lock_held=True,
            )
    first_board = board_names[0]
    owns_connections = conn is None
    if isinstance(conn, Mapping):
        connections = dict(conn)
    elif conn is None:
        connections: dict[str, Any] = {}
    elif len(board_names) == 1:
        connections = {first_board: conn}
    else:
        raise ValueError("multi-board daemon requires a connection mapping or conn=None")

    run_id = store.record_daemon_start(
        first_board,
        os.getpid(),
        boards=board_names,
        tick_interval=interval,
    )
    last_sync = 0.0
    try:
        while True:
            now = time.monotonic()
            do_sync = sync and (sync_interval is None or now - last_sync >= sync_interval)
            results: dict[str, Any] = {}
            for board_name in board_names:
                board_conn = connections.get(board_name)
                if board_conn is None:
                    try:
                        from hermes_cli import kanban_db
                        board_conn = kanban_db.connect(board=board_name)
                        connections[board_name] = board_conn
                    except Exception as exc:
                        _record_board_tick_failure(board_name, exc)
                        results[board_name] = {"error": str(exc)}
                        continue
                try:
                    results[board_name] = tick(
                        board_conn,
                        board=board_name,
                        sync=do_sync,
                        require_recipes=require_recipes,
                    )
                    store.record_daemon_tick(run_id, board_name)
                except Exception as exc:
                    _record_board_tick_failure(board_name, exc)
                    results[board_name] = {"error": str(exc)}
                    if owns_connections:
                        try:
                            board_conn.close()
                        except Exception:
                            logger.exception(
                                "Factory could not close failed board connection for %s",
                                board_name,
                            )
                        connections.pop(board_name, None)
            if do_sync:
                last_sync = now
            if once:
                return results[first_board] if len(board_names) == 1 else {"boards": results}
            time.sleep(max(0.01, interval))
    finally:
        try:
            store.record_daemon_end(run_id)
        finally:
            if owns_connections:
                for board_conn in connections.values():
                    try:
                        board_conn.close()
                    except Exception:
                        logger.exception("Factory could not close a board connection")


__all__ = ["daemon_lock", "tick", "run", "validate_recipe_mode"]
