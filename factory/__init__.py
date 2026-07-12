"""Hermes Factory plugin registration entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _service_ready() -> bool:
    """Return whether Factory has been initialized for tool use."""
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return (Path(home) / "factory" / "factory.db").is_file()


def _verdict_tool(args: dict) -> str:
    """Record a policy verdict and return a Hermes JSON-string result."""
    from .policy import record_verdict
    return json.dumps(record_verdict(args["task_id"], args["stage_id"], args["outcome"], args["body"], args["seat"]))


def _costs_tool(args: dict) -> str:
    """Return Factory cost rollups as a Hermes JSON-string result."""
    from .store import costs_rollup
    return json.dumps(costs_rollup(args.get("by", "seat"), int(args.get("since_days", 7))))


def _monitor_tool(args: dict) -> str:
    """Add a Factory monitor and return a Hermes JSON-string result."""
    from .store import add_monitor
    add_monitor(args["task_id"], args["next_check_at"], args.get("timeout_at"), int(args.get("max_attempts", 3)),
                args.get("recovery_policy", "wake_owner"), args.get("notes", ""), args.get("scheduled_by", "agent"))
    return json.dumps({"ok": True, "task_id": args["task_id"]})


def _setup_cli(parser) -> None:
    """Delegate argparse setup to the optional Factory CLI lane."""
    from . import cli
    setup = getattr(cli, "register_cli", getattr(cli, "setup_parser", None))
    if setup is None:
        raise AttributeError("factory.cli must export register_cli or setup_parser")
    setup(parser)


def _handle_cli(args):
    """Delegate command handling to the optional Factory CLI lane."""
    from . import cli
    handler = getattr(cli, "factory_command", getattr(cli, "main", None))
    if handler is None:
        raise AttributeError("factory.cli must export factory_command or main")
    return handler(args)


def _on_complete(**kwargs) -> None:
    """Apply execution policy after a kanban completion event."""
    from .policy import on_complete
    on_complete(kwargs["task_id"], kwargs.get("board") or "", kwargs.get("assignee") or "", kwargs.get("summary") or "")


def _on_block(**kwargs) -> None:
    """Advance an existing task monitor after a blocked event."""
    from .store import bump_monitor
    bump_monitor(kwargs["task_id"])


def register(ctx) -> None:
    """Register Factory CLI, tools, and kanban lifecycle hooks."""
    ctx.register_cli_command(name="factory", help="Operate Hermes Factory", setup_fn=_setup_cli,
                             handler_fn=_handle_cli, description="Teams, hierarchy, policy, watchdogs, and cost telemetry")
    ctx.register_hook("kanban_task_completed", _on_complete)
    ctx.register_hook("kanban_task_blocked", _on_block)
    from .telemetry import on_claim
    ctx.register_hook("kanban_task_claimed", on_claim)
    tools = (
        ("factory_verdict", {"type": "object", "properties": {"task_id": {"type": "string"}, "stage_id": {"type": "string"}, "outcome": {"type": "string"}, "body": {"type": "string"}, "seat": {"type": "string"}}, "required": ["task_id", "stage_id", "outcome", "body", "seat"]}, _verdict_tool),
        ("factory_costs", {"type": "object", "properties": {"by": {"type": "string"}, "since_days": {"type": "integer"}}}, _costs_tool),
        ("factory_monitor_add", {"type": "object", "properties": {"task_id": {"type": "string"}, "next_check_at": {"type": "string"}, "timeout_at": {"type": "string"}, "max_attempts": {"type": "integer"}, "recovery_policy": {"type": "string"}, "notes": {"type": "string"}, "scheduled_by": {"type": "string"}}, "required": ["task_id", "next_check_at"]}, _monitor_tool),
    )
    for name, schema, handler in tools:
        ctx.register_tool(name=name, toolset="factory", schema=schema, handler=handler, check_fn=_service_ready,
                          description=f"Hermes Factory {name.removeprefix('factory_').replace('_', ' ')}")


__all__ = ["register"]
