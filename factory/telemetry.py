"""Factory executor usage parsing and append-only telemetry."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _factory_home() -> Path:
    """Return Factory's state directory under the configured Hermes home."""
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "factory"


def parse_usage(executor_name: str, log_text: str) -> dict:
    """Delegate usage parsing to the registered executor."""
    from factory.executors import get_executor
    return get_executor(executor_name).parse_usage(log_text)


def append_jsonl(record: dict) -> None:
    """Append one canonical JSON record to ``telemetry.jsonl``."""
    root = _factory_home()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "telemetry.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def on_claim(task_id, board, assignee, **kw) -> None:
    """Mirror a kanban claim event to the append-only telemetry ledger."""
    append_jsonl({"event": "claim", "at": datetime.now(timezone.utc).isoformat(), "task_id": task_id,
                  "board": board, "assignee": assignee, **kw})


__all__ = ["append_jsonl", "on_claim", "parse_usage"]
