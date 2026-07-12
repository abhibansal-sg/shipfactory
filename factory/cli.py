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
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "factory"


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
    from factory import store
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
    from factory.config import load_seats
    cfg = load_seats()
    rows = [vars(seat) for seat in cfg.seats.values()]
    return _emit(rows)


def _org(_args: argparse.Namespace) -> str:
    from factory.config import load_seats
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
    from factory import daemon
    from hermes_cli import kanban_db
    conn = kanban_db.connect(board=args.board)
    try:
        result = daemon.run(conn, board=args.board, interval=args.interval, once=args.once,
                            sync=bool(args.sync_interval), sync_interval=args.sync_interval)
    finally:
        conn.close()
    return _emit(result)


def _verdict(args: argparse.Namespace) -> Any:
    from factory.policy import record_verdict
    return _emit(record_verdict(args.task, args.stage, args.outcome, args.body, args.seat))


def _policy(args: argparse.Namespace) -> Any:
    from factory import store
    if args.policy_command == "show":
        return _emit(store.get_policy(args.task))
    if args.file:
        value = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        value = json.loads(args.json)
    store.set_policy(args.task, value)
    return _emit({"task_id": args.task, "policy": value})


def _monitor(args: argparse.Namespace) -> Any:
    from factory import store
    if args.monitor_command == "list":
        return _emit(store.due_monitors("9999-12-31T23:59:59+00:00"))
    store.add_monitor(args.task, args.next_check_at, args.timeout_at, args.max_attempts,
                      args.recovery_policy, args.notes, args.scheduled_by)
    return _emit({"added": args.task})


def _watchdog(args: argparse.Namespace) -> Any:
    from factory import store
    if args.watchdog_command == "list":
        return _emit(store.watchdogs())
    store.add_watchdog(args.root_task, args.agent, args.instructions)
    return _emit({"added": args.root_task})


def _costs(args: argparse.Namespace) -> Any:
    from factory import store
    return _emit(store.costs_rollup(args.by, args.since))


def _sync(args: argparse.Namespace) -> Any:
    from factory import github_sync
    return _emit(github_sync.sync(board=args.board, repo=args.repo))


def _dashboard(args: argparse.Namespace) -> None:
    from factory.dashboard.server import serve
    serve(port=args.port)


def _runs(args: argparse.Namespace) -> Any:
    from factory import store
    accessor = getattr(store, "get_run", None) if args.id else getattr(store, "runs", None)
    return _emit(accessor(args.id) if args.id and accessor else accessor() if accessor else [])


def _pause(args: argparse.Namespace) -> Any:
    from factory import store
    paused = args.factory_verb == "pause"
    store.set_seat_paused(args.seat, paused)
    return _emit({"seat": args.seat, "paused": paused})


def _handler(parser: argparse.ArgumentParser, name: str, help_text: str,
             function: Callable[[argparse.Namespace], Any]) -> argparse.ArgumentParser:
    command = parser.add_parser(name, help=help_text)
    command.set_defaults(_factory_handler=function)
    return command


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Install the complete ``hermes factory`` argparse command tree."""
    verbs = parser.add_subparsers(dest="factory_verb", required=True)
    p = _handler(verbs, "init", "initialize Factory state", _init); p.add_argument("--force", action="store_true")
    _handler(verbs, "seats", "list configured seats", _seats)
    _handler(verbs, "org", "print the reporting tree", _org)
    p = _handler(verbs, "daemon", "run dispatch and watchdog ticks", _daemon)
    p.add_argument("--board"); p.add_argument("--once", action="store_true"); p.add_argument("--interval", type=float, default=5.0); p.add_argument("--sync-interval", type=float)
    p = _handler(verbs, "verdict", "record a policy-stage verdict", _verdict)
    p.add_argument("task"); p.add_argument("--stage", required=True); p.add_argument("--outcome", choices=("approve", "request_changes"), required=True); p.add_argument("--body", required=True); p.add_argument("--seat", required=True)
    p = _handler(verbs, "policy", "show or set task policy", _policy); subs = p.add_subparsers(dest="policy_command", required=True)
    q = subs.add_parser("show"); q.add_argument("task")
    q = subs.add_parser("set"); q.add_argument("task"); source = q.add_mutually_exclusive_group(required=True); source.add_argument("--json"); source.add_argument("--file")
    p = _handler(verbs, "monitor", "add or list recovery monitors", _monitor); subs = p.add_subparsers(dest="monitor_command", required=True)
    subs.add_parser("list")
    q = subs.add_parser("add"); q.add_argument("task"); q.add_argument("--next-check-at", required=True); q.add_argument("--timeout-at"); q.add_argument("--max-attempts", type=int, default=3); q.add_argument("--recovery-policy", choices=("wake_owner", "create_recovery_task", "escalate_to_board"), default="wake_owner"); q.add_argument("--notes", default=""); q.add_argument("--scheduled-by", default="operator")
    p = _handler(verbs, "watchdog", "add or list subtree watchdogs", _watchdog); subs = p.add_subparsers(dest="watchdog_command", required=True)
    subs.add_parser("list"); q = subs.add_parser("add"); q.add_argument("root_task"); q.add_argument("--agent", required=True); q.add_argument("--instructions", required=True)
    p = _handler(verbs, "costs", "show token-cost rollups", _costs); p.add_argument("--by", choices=("seat", "executor", "task"), default="seat"); p.add_argument("--since", type=_since_days, default=7)
    p = _handler(verbs, "sync", "synchronize GitHub Issues", _sync); p.add_argument("--board"); p.add_argument("--repo", required=True)
    p = _handler(verbs, "dashboard", "serve the local operator dashboard", _dashboard); p.add_argument("--port", type=int, default=18820)
    p = _handler(verbs, "runs", "list or inspect harness runs", _runs); p.add_argument("id", nargs="?", type=int)
    for name in ("pause", "resume"):
        p = _handler(verbs, name, f"{name} a seat", _pause); p.add_argument("seat")


setup_parser = register_cli


def factory_command(args: argparse.Namespace) -> Any:
    """Dispatch parsed Factory arguments to their lazy command handler."""
    return args._factory_handler(args)


def main(argv: list[str] | None = None) -> Any:
    """Parse and execute a standalone Factory command, primarily for tests."""
    parser = argparse.ArgumentParser(prog="hermes factory")
    register_cli(parser)
    return factory_command(parser.parse_args(argv))


__all__ = ["factory_command", "main", "register_cli", "setup_parser"]


if __name__ == "__main__":
    # #16-V1: make the documented standalone CLI executable by subprocess.
    main()
