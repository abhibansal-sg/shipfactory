"""The Factory dispatch daemon."""

from __future__ import annotations

import os
import logging
import time
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
            from factory.telemetry import append_jsonl
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
    from factory.spawn import factory_spawn, reap_finished

    dispatch_kwargs: dict[str, Any] = {}
    result_recipes = None
    result_selector = None
    try:
        from factory.config import FactoryConfigError, load_seats
        cfg = load_seats()
        recipes_cfg = cfg.recipes or {}
        if recipes_cfg.get("enabled"):
            from factory.recipes.advancer import apply_events, deliver_outbox, reconcile_root_collectors, startup_guard
            startup_guard(cfg)
            dispatch_kwargs["max_in_progress"] = int(recipes_cfg["dispatcher_max_in_progress"])
            result_recipes = {"events": apply_events(conn, profiles=recipes_cfg["execution_profiles"], board=board or cfg.company), "outbox": deliver_outbox(), "root_collectors": reconcile_root_collectors(conn)}
            from factory.config import selector_config
            if selector_config(recipes_cfg)["enabled"]:
                from factory.recipes.selector_stage import run_stage
                result_selector = run_stage(conn, board or cfg.company)
            else:
                result_selector = {"leased": 0, "instantiated": 0, "parked": 0, "skipped": 0}
    except (ImportError, FileNotFoundError, OSError, FactoryConfigError):
        # Existing Factory installations without a seats file retain the old
        # dispatch behavior; a configured recipe board is always fail-closed.
        result_recipes = None
    dispatched = dispatch_once(conn, spawn_fn=factory_spawn, board=board, **dispatch_kwargs)
    reaped = reap_finished()
    _board_db_health_pass(conn, board)
    result: dict[str, Any] = {"dispatch": dispatched, "reaped": reaped}
    if result_recipes is not None:
        result["recipes"] = result_recipes
    if result_selector is not None:
        result["selector"] = result_selector
    # Lane C modules are deliberately optional at plugin import time.
    try:
        from factory import watchdog

        result["watchdog"] = watchdog.tick(conn, board=board)
    except ImportError:
        result["watchdog"] = None
    if sync:
        try:
            from factory import github_sync

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


def run(conn, *, board: str | None = None, interval: float = 5.0,
        once: bool = False, sync: bool = False, sync_interval: float | None = None) -> dict[str, Any] | None:
    """Run Factory ticks until interrupted, or return one tick when ``once``."""
    from factory import store
    if board is None:
        from hermes_cli.kanban_db import get_current_board
        board = get_current_board()
    run_id = store.record_daemon_start(board, os.getpid())
    last_sync = 0.0
    try:
        while True:
            now = time.monotonic()
            do_sync = sync and (sync_interval is None or now - last_sync >= sync_interval)
            result = tick(conn, board=board, sync=do_sync)
            store.record_daemon_tick(run_id, board)
            if do_sync:
                last_sync = now
            if once:
                return result
            time.sleep(max(0.01, interval))
    finally:
        store.record_daemon_end(run_id)


__all__ = ["tick", "run"]
