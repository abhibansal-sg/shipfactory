"""The Factory dispatch daemon."""

from __future__ import annotations

import time
from typing import Any


def tick(conn, *, board: str | None = None, sync: bool = False) -> dict[str, Any]:
    """Run one dispatch, reaping, watchdog, and optional GitHub-sync cycle."""
    from hermes_cli.kanban_db import dispatch_once
    from factory.spawn import factory_spawn, reap_finished

    dispatch_kwargs: dict[str, Any] = {}
    result_recipes = None
    try:
        from factory.config import FactoryConfigError, load_seats
        cfg = load_seats()
        recipes_cfg = cfg.recipes or {}
        if recipes_cfg.get("enabled"):
            from factory.recipes.advancer import apply_events, deliver_outbox, reconcile_root_collectors, startup_guard
            startup_guard(cfg)
            dispatch_kwargs["max_in_progress"] = int(recipes_cfg["dispatcher_max_in_progress"])
            result_recipes = {"events": apply_events(conn, profiles=recipes_cfg["execution_profiles"]), "outbox": deliver_outbox(), "root_collectors": reconcile_root_collectors(conn)}
    except (ImportError, FileNotFoundError, OSError, FactoryConfigError):
        # Existing Factory installations without a seats file retain the old
        # dispatch behavior; a configured recipe board is always fail-closed.
        result_recipes = None
    dispatched = dispatch_once(conn, spawn_fn=factory_spawn, board=board, **dispatch_kwargs)
    reaped = reap_finished()
    result: dict[str, Any] = {"dispatch": dispatched, "reaped": reaped}
    if result_recipes is not None:
        result["recipes"] = result_recipes
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
    last_sync = 0.0
    while True:
        now = time.monotonic()
        do_sync = sync and (sync_interval is None or now - last_sync >= sync_interval)
        result = tick(conn, board=board, sync=do_sync)
        if do_sync:
            last_sync = now
        if once:
            return result
        time.sleep(max(0.01, interval))


__all__ = ["tick", "run"]
