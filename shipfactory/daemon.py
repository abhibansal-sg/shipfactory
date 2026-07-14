"""The Factory dispatch daemon."""

from __future__ import annotations

import os
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any


logger = logging.getLogger(__name__)

# Low-cadence board-database maintenance. Tests may lower the cadence without
# changing the public daemon interface.
_DB_HEALTH_EVERY_TICKS = 60
_db_health_tick = 0


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


def tick(conn, *, board: str | None = None, sync: bool = False) -> dict[str, Any]:
    """Run one dispatch, reaping, watchdog, and optional GitHub-sync cycle."""
    from hermes_cli.kanban_db import dispatch_once
    from shipfactory.spawn import shipfactory_spawn, reap_finished

    dispatch_kwargs: dict[str, Any] = {}
    result_recipes = None
    result_selector = None
    try:
        from shipfactory.config import FactoryConfigError, load_seats
        cfg = load_seats()
        recipes_cfg = cfg.recipes or {}
        if recipes_cfg.get("enabled"):
            from shipfactory.recipes.advancer import apply_events, deliver_outbox, reconcile_root_collectors, startup_guard
            startup_guard(cfg)
            dispatch_kwargs["max_in_progress"] = int(recipes_cfg["dispatcher_max_in_progress"])
            recipe_board = board or cfg.company
            result_recipes = {
                "events": apply_events(
                    conn,
                    profiles=recipes_cfg["execution_profiles"],
                    board=recipe_board,
                ),
                "outbox": deliver_outbox(),
                "root_collectors": reconcile_root_collectors(conn, board=recipe_board),
            }
            from shipfactory.config import selector_config
            if selector_config(recipes_cfg)["enabled"]:
                from shipfactory.recipes.selector_stage import run_stage
                result_selector = run_stage(conn, board or cfg.company)
            else:
                result_selector = {"leased": 0, "instantiated": 0, "parked": 0, "skipped": 0}
    except (ImportError, FileNotFoundError, OSError, FactoryConfigError):
        # Existing Factory installations without a seats file retain the old
        # dispatch behavior; a configured recipe board is always fail-closed.
        result_recipes = None
    # Finding #23 tick-order race: reap exited harnesses BEFORE dispatch_once,
    # whose claim watchdog otherwise sees a dead pid with an unfinalized task
    # and records a protocol violation — burning the failure fuse on workers
    # that completed perfectly. Reaping first finalizes their kanban state.
    reaped = reap_finished()
    dispatched = dispatch_once(conn, spawn_fn=shipfactory_spawn, board=board, **dispatch_kwargs)
    reaped += reap_finished()
    _board_db_health_pass(conn, board)
    result: dict[str, Any] = {"dispatch": dispatched, "reaped": reaped}
    if result_recipes is not None:
        result["recipes"] = result_recipes
    if result_selector is not None:
        result["selector"] = result_selector
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
        sync_interval: float | None = None) -> dict[str, Any] | None:
    """Run one tick loop across one or more isolated board connections.

    A traditional single-board caller continues to pass its connection in
    ``conn``.  Multi-board callers may pass a board-to-connection mapping, or
    ``None`` to let the daemon own and reconnect each board connection.
    """
    from shipfactory import store
    board_names = _served_boards(board, boards)
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


__all__ = ["tick", "run"]
