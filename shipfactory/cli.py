"""Command-line operator surface for Hermes Factory."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


SEATS_SKELETON = """company: straits-lab-eng
seats:
  release:
    profile: release
    executor: codex
    model: gpt-5.6
    role: devops
  verifier:
    profile: verifier
    executor: claude
    model: sonnet-5
    reasoning: adaptive
    reports_to: architect
    role: qa
    max_concurrent: 2
  architect:
    profile: architect
    executor: codex
    model: gpt-5.6
    reports_to: release
    role: engineer
  dev-backend:
    profile: dev-backend
    executor: codex
    model: gpt-5.6
    reasoning: medium
    reports_to: architect
    role: engineer
hierarchy_gates:
  landers: [release]
  verdicts: [verifier]
"""


def _home() -> Path:
    """Return Factory's state directory."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "shipfactory"


def _emit(value: Any) -> Any:
    """Print structured results consistently and return them for embedding."""
    if value is not None:
        print(value if isinstance(value, str) else json.dumps(value, indent=2, default=str))
    return value


def _since_days(value: str) -> int:
    """Parse CLI durations such as ``7d`` into whole days."""
    text = value.strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    days = int(text)
    if days < 0:
        raise argparse.ArgumentTypeError("duration must be non-negative")
    return days


def _init(args: argparse.Namespace) -> dict[str, Any]:
    from shipfactory import store
    root = _home()
    root.mkdir(parents=True, exist_ok=True)
    seats = root / "seats.yaml"
    if seats.exists() and not args.force:
        written = False
    else:
        seats.write_text(SEATS_SKELETON, encoding="utf-8")
        written = True
    store.init_db()
    return _emit({"initialized": True, "seats": str(seats), "written": written})


def _seats(_args: argparse.Namespace) -> list[dict[str, Any]]:
    from shipfactory.config import load_seats
    cfg = load_seats()
    rows = [vars(seat) for seat in cfg.seats.values()]
    return _emit(rows)


def _seat_create(args: argparse.Namespace) -> dict[str, Any]:
    from shipfactory.seats_admin import create_seat
    return _emit(create_seat(
        args.name, args.profile, args.executor, args.model, args.reasoning,
        args.role, args.max_concurrent, _provider_config_from_args(args),
    ))


def _seat_update(args: argparse.Namespace) -> dict[str, Any]:
    from shipfactory.seats_admin import update_seat
    return _emit(update_seat(
        args.name, args.profile, args.executor, args.model, args.reasoning,
        args.role, args.max_concurrent, _provider_config_from_args(args),
    ))


def _seat_list(_args: argparse.Namespace) -> list[dict[str, Any]]:
    from shipfactory.seats_admin import seat_details
    return _emit(seat_details())


def _provider_config_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    supplied = [getattr(args, field, None) for field in ("provider", "base_url", "provider_model")]
    if not any(value is not None for value in supplied):
        return None
    if not all(value is not None for value in supplied):
        raise ValueError("--provider, --base-url, and --provider-model must be supplied together")
    return {"provider": args.provider, "base_url": args.base_url, "model": args.provider_model}


def _org(_args: argparse.Namespace) -> str:
    from shipfactory.config import load_seats
    cfg = load_seats()
    children: dict[str | None, list[str]] = {}
    for name, seat in cfg.seats.items():
        children.setdefault(seat.reports_to, []).append(name)
    lines: list[str] = []
    def walk(name: str, prefix: str = "") -> None:
        lines.append(prefix + name)
        names = sorted(children.get(name, []))
        for index, child in enumerate(names):
            walk(child, prefix + ("└── " if index == len(names) - 1 else "├── "))
    for root in sorted(children.get(None, [])):
        walk(root)
    return _emit("\n".join(lines))


def _daemon(args: argparse.Namespace) -> Any:
    from shipfactory import daemon
    def argument_values(value: Any) -> list[Any]:
        if value is None:
            return []
        return list(value) if isinstance(value, (list, tuple)) else [value]

    values = [
        *argument_values(getattr(args, "board", None)),
        *argument_values(getattr(args, "boards", None)),
    ]
    boards = list(dict.fromkeys(
        board.strip()
        for value in values
        for board in str(value).split(",")
        if board.strip()
    ))
    served = daemon._served_boards(None, boards)
    require_recipes = bool(getattr(args, "require_recipes", False))
    with daemon.daemon_lock(served):
        if require_recipes:
            daemon.validate_recipe_mode(required=True)
        if len(served) == 1:
            # conn=None makes the daemon open a fresh board connection per
            # tick (stale-WAL hygiene); board=served[0] preserves the bare
            # single-board result shape for --once consumers.
            result = daemon.run(
                None,
                board=served[0],
                interval=args.interval,
                once=args.once,
                sync=bool(args.sync_interval),
                sync_interval=args.sync_interval,
                require_recipes=require_recipes,
                _lock_held=True,
            )
        else:
            result = daemon.run(
                None,
                boards=served,
                interval=args.interval,
                once=args.once,
                sync=bool(args.sync_interval),
                sync_interval=args.sync_interval,
                require_recipes=require_recipes,
                _lock_held=True,
            )
    return _emit(result)


def _verdict(args: argparse.Namespace) -> Any:
    from shipfactory.policy import record_verdict
    return _emit(record_verdict(args.task, args.stage, args.outcome, args.body, args.seat))


def _policy(args: argparse.Namespace) -> Any:
    from shipfactory import store
    if args.policy_command == "show":
        return _emit(store.get_policy(args.task))
    if args.file:
        value = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        value = json.loads(args.json)
    store.set_policy(args.task, value)
    return _emit({"task_id": args.task, "policy": value})


def _monitor(args: argparse.Namespace) -> Any:
    from shipfactory import store
    if args.monitor_command == "list":
        return _emit(store.due_monitors("9999-12-31T23:59:59+00:00"))
    store.add_monitor(args.task, args.next_check_at, args.timeout_at, args.max_attempts,
                      args.recovery_policy, args.notes, args.scheduled_by, args.interval_seconds)
    return _emit({"added": args.task})


def _watchdog(args: argparse.Namespace) -> Any:
    from shipfactory import store
    if args.watchdog_command == "list":
        return _emit(store.watchdogs())
    store.add_watchdog(args.root_task, args.agent, args.instructions)
    return _emit({"added": args.root_task})


def _costs(args: argparse.Namespace) -> Any:
    from shipfactory import store
    return _emit(store.costs_rollup(args.by, args.since))


def _sync(args: argparse.Namespace) -> Any:
    from shipfactory import github_sync
    return _emit(github_sync.sync(board=args.board, repo=args.repo))


def _dashboard(args: argparse.Namespace) -> None:
    from shipfactory.dashboard.server import serve
    serve(port=args.port)


def _runs(args: argparse.Namespace) -> Any:
    from shipfactory import store
    accessor = getattr(store, "get_run", None) if args.id else getattr(store, "runs", None)
    return _emit(accessor(args.id) if args.id and accessor else accessor() if accessor else [])


def _pause(args: argparse.Namespace) -> Any:
    from shipfactory import store
    paused = args.shipfactory_verb == "pause"
    store.set_seat_paused(args.seat, paused)
    return _emit({"seat": args.seat, "paused": paused})


def _recipe(args: argparse.Namespace) -> Any:
    """Thin CLI facade: commands enqueue/reconcile through the recipe service."""
    from shipfactory import store
    from shipfactory.recipes import advancer
    from hermes_cli import kanban_db
    command = args.recipe_command
    if command in {"show", "waiting", "list"}:
        with store._connect() as db:
            if command == "show":
                row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (args.instance,)).fetchone()
                if not row: raise ValueError("unknown recipe instance")
                answer = dict(row); answer["steps"] = [dict(r) for r in db.execute("SELECT * FROM recipe_steps WHERE instance_id=? ORDER BY step_id,activation", (args.instance,))]
            elif command == "waiting":
                answer = [dict(r) for r in db.execute("SELECT * FROM recipe_instances WHERE status IN ('waiting_gate','waiting_event','blocked') ORDER BY updated_at")]
                answer.extend({
                    **dict(row), "status": "blocked", "blocked_reason": row["outcome"],
                    "kind": "triage_selection",
                } for row in db.execute(
                    "SELECT * FROM triage_selections WHERE outcome IN "
                    "('needs_clarification','no_recipe_match') ORDER BY updated_at"
                ))
            else:
                answer = [dict(r) for r in db.execute("SELECT * FROM recipe_instances ORDER BY created_at DESC")]
        return _emit(answer)
    if command == "event":
        return _emit({"key": advancer.event(args.instance, args.step, json.loads(args.payload))})
    if command in {"approve", "reject"}:
        return _emit(_recipe_gate(
            None, args.instance, args.step, command, getattr(args, "reason", ""),
            activation=args.activation, revision_hash=args.revision_hash,
            evidence_bundle_hash=args.evidence_bundle_hash, nonce=args.nonce,
            actor_kind=args.actor_kind, actor_id=args.actor_id, channel=args.channel,
        ))
    if command == "release":
        return _emit(_recipe_release(None, args.instance, args.step, args.reason))
    conn = kanban_db.connect(board=getattr(args, "board", None))
    try:
        if command == "cancel":
            return _emit(advancer.cancel(conn, args.instance, dry_run=args.dry_run))
        if command == "reroute":
            return _emit(_reroute(conn, args))
    finally:
        conn.close()


def _recipe_gate(
    conn: Any, instance_id: str, step_id: str, decision: str, reason: str, *,
    activation: int | None = None, revision_hash: str | None = None,
    evidence_bundle_hash: str | None = None, nonce: str | None = None,
    actor_kind: str = "operator", actor_id: str = "local-operator",
    channel: str = "cli",
) -> dict[str, Any]:
    from shipfactory import store
    from shipfactory.recipes import advancer
    with store._connect() as db:
        instance = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        step = db.execute("SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? ORDER BY activation DESC LIMIT 1", (instance_id, step_id)).fetchone()
        if not instance or not step or step["primitive"] != "approval_gate" or step["state"] != "waiting": raise ValueError("approval gate is not waiting")
    key = advancer.gate_decision(
        instance_id, step_id, decision, reason, activation=activation,
        revision_hash=revision_hash, evidence_bundle_hash=evidence_bundle_hash,
        nonce=nonce, actor_kind=actor_kind, actor_id=actor_id, channel=channel,
    )
    with store._connect() as db:
        updated = db.execute("SELECT status FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
        recorded = db.execute(
            "SELECT id FROM gate_decisions WHERE advance_event_key=?", (key,),
        ).fetchone()
    return {
        "instance_id": instance_id,
        "decision_id": recorded["id"] if recorded else key,
        "key": key,
        "status": updated["status"] if updated else "unknown",
    }


def _recipe_release(conn: Any, instance_id: str, step_id: str, reason: str) -> dict[str, Any]:
    from shipfactory import store
    from shipfactory.recipes import advancer
    key = advancer.release_review_stall(instance_id, step_id, reason)
    with store._connect() as db:
        updated = db.execute("SELECT status FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
    return {
        "instance_id": instance_id,
        "decision_id": key,
        "status": updated["status"] if updated else "unknown",
        "key": key,
    }


def _reroute(conn: Any, args: argparse.Namespace) -> dict[str, Any]:
    from shipfactory import store
    from shipfactory.recipes.loader import load_library
    from shipfactory.recipes.instantiate import instantiate, replace_unactivated
    from shipfactory.recipes.advancer import cancel
    with store._connect() as db:
        old = dict(db.execute("SELECT * FROM recipe_instances WHERE id=?", (args.instance,)).fetchone() or {})
        if not old: raise ValueError("unknown recipe instance")
        activated = db.execute(
            "SELECT 1 FROM recipe_steps WHERE instance_id=? "
            "AND (kanban_task_id IS NOT NULL OR state NOT IN ('pending','skipped')) LIMIT 1",
            (args.instance,),
        ).fetchone()
    library = load_library(args.library)
    recipe = library.get(args.recipe)
    parameters = json.loads(args.parameters)
    if activated:
        cancel(conn, args.instance)
        result = instantiate(conn, board=old["board"], recipe=recipe, parameters=parameters, parent_tasks=[])
    else:
        result = replace_unactivated(instance_id=args.instance, recipe=recipe, parameters=parameters)
    return {"old_instance": args.instance, "activated": bool(activated), "replacement": result}


def _handler(parser: argparse.ArgumentParser, name: str, help_text: str,
             function: Callable[[argparse.Namespace], Any]) -> argparse.ArgumentParser:
    command = parser.add_parser(name, help=help_text)
    command.set_defaults(_shipfactory_handler=function)
    return command


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Install the complete ``hermes shipfactory`` argparse command tree."""
    verbs = parser.add_subparsers(dest="shipfactory_verb", required=True)
    p = _handler(verbs, "init", "initialize Factory state", _init); p.add_argument("--force", action="store_true")
    _handler(verbs, "seats", "list configured seats", _seats)
    p = _handler(verbs, "seat-create", "create a Factory employment contract", _seat_create)
    p.add_argument("name"); p.add_argument("--profile", required=True); p.add_argument("--executor", required=True, choices=("hermes", "codex", "claude")); p.add_argument("--model", required=True); p.add_argument("--reasoning", default="medium"); p.add_argument("--role", required=True); p.add_argument("--max-concurrent", type=int, default=1); p.add_argument("--provider"); p.add_argument("--base-url"); p.add_argument("--provider-model")
    p = _handler(verbs, "seat-update", "update a Factory employment contract", _seat_update)
    p.add_argument("name"); p.add_argument("--profile"); p.add_argument("--executor", choices=("hermes", "codex", "claude")); p.add_argument("--model"); p.add_argument("--reasoning"); p.add_argument("--role"); p.add_argument("--max-concurrent", type=int); p.add_argument("--provider"); p.add_argument("--base-url"); p.add_argument("--provider-model")
    _handler(verbs, "seat-list", "list seats with profile model resolution", _seat_list)
    _handler(verbs, "org", "print the reporting tree", _org)
    p = _handler(verbs, "daemon", "run dispatch and watchdog ticks", _daemon)
    p.add_argument("--board", action="append"); p.add_argument("--boards", action="append"); p.add_argument("--once", action="store_true"); p.add_argument("--interval", type=float, default=5.0); p.add_argument("--sync-interval", type=float); p.add_argument("--require-recipes", action="store_true")
    p = _handler(verbs, "verdict", "record a policy-stage verdict", _verdict)
    p.add_argument("task"); p.add_argument("--stage", required=True); p.add_argument("--outcome", choices=("approve", "request_changes"), required=True); p.add_argument("--body", required=True); p.add_argument("--seat", required=True)
    p = _handler(verbs, "policy", "show or set task policy", _policy); subs = p.add_subparsers(dest="policy_command", required=True)
    q = subs.add_parser("show"); q.add_argument("task")
    q = subs.add_parser("set"); q.add_argument("task"); source = q.add_mutually_exclusive_group(required=True); source.add_argument("--json"); source.add_argument("--file")
    p = _handler(verbs, "monitor", "add or list recovery monitors", _monitor); subs = p.add_subparsers(dest="monitor_command", required=True)
    subs.add_parser("list")
    q = subs.add_parser("add"); q.add_argument("task"); q.add_argument("--next-check-at", required=True); q.add_argument("--timeout-at"); q.add_argument("--max-attempts", type=int, default=3); q.add_argument("--interval-seconds", type=int, default=300); q.add_argument("--recovery-policy", choices=("wake_owner", "create_recovery_task", "escalate_to_board"), default="wake_owner"); q.add_argument("--notes", default=""); q.add_argument("--scheduled-by", default="operator")
    p = _handler(verbs, "watchdog", "add or list subtree watchdogs", _watchdog); subs = p.add_subparsers(dest="watchdog_command", required=True)
    subs.add_parser("list"); q = subs.add_parser("add"); q.add_argument("root_task"); q.add_argument("--agent", required=True); q.add_argument("--instructions", required=True)
    p = _handler(verbs, "costs", "show token-cost rollups", _costs); p.add_argument("--by", choices=("seat", "executor", "task"), default="seat"); p.add_argument("--since", type=_since_days, default=7)
    p = _handler(verbs, "sync", "synchronize GitHub Issues", _sync); p.add_argument("--board"); p.add_argument("--repo", required=True)
    p = _handler(verbs, "dashboard", "serve the local operator dashboard", _dashboard); p.add_argument("--port", type=int, default=18820)
    p = _handler(verbs, "runs", "list or inspect harness runs", _runs); p.add_argument("id", nargs="?", type=int)
    p = _handler(verbs, "recipe", "operate recipe instances", _recipe); subs = p.add_subparsers(dest="recipe_command", required=True)
    q = subs.add_parser("show"); q.add_argument("instance")
    subs.add_parser("waiting"); subs.add_parser("list")
    for name in ("approve", "reject"):
        q = subs.add_parser(name); q.add_argument("instance"); q.add_argument("step"); q.add_argument("--reason", default=""); q.add_argument("--activation", type=int, required=True); q.add_argument("--revision-hash", required=True); q.add_argument("--evidence-bundle-hash", required=True); q.add_argument("--nonce", required=True); q.add_argument("--actor-kind", default="operator"); q.add_argument("--actor-id", required=True); q.add_argument("--channel", default="cli"); q.add_argument("--board")
    q = subs.add_parser("release"); q.add_argument("instance"); q.add_argument("step"); q.add_argument("--reason", required=True); q.add_argument("--board")
    q = subs.add_parser("event"); q.add_argument("instance"); q.add_argument("step"); q.add_argument("payload"); q.add_argument("--board")
    q = subs.add_parser("cancel"); q.add_argument("instance"); q.add_argument("--dry-run", action="store_true"); q.add_argument("--board")
    q = subs.add_parser("reroute"); q.add_argument("instance"); q.add_argument("recipe"); q.add_argument("--parameters", default="{}"); q.add_argument("--library", required=True); q.add_argument("--board")
    for name in ("pause", "resume"):
        p = _handler(verbs, name, f"{name} a seat", _pause); p.add_argument("seat")


setup_parser = register_cli


def shipfactory_command(args: argparse.Namespace) -> Any:
    """Dispatch parsed Factory arguments to their lazy command handler."""
    return args._shipfactory_handler(args)


def main(argv: list[str] | None = None) -> Any:
    """Parse and execute a standalone Factory command, primarily for tests."""
    parser = argparse.ArgumentParser(prog="hermes shipfactory")
    register_cli(parser)
    return shipfactory_command(parser.parse_args(argv))


__all__ = ["shipfactory_command", "main", "register_cli", "setup_parser"]


if __name__ == "__main__":
    # #16-V1: make the documented standalone CLI executable by subprocess.
    main()
