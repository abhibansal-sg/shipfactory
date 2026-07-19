"""Factory executor registry."""

from .base import Executor
from .claude_exec import ClaudeExecutor
from .codex_exec import CodexExecutor
from .grok_exec import GrokExecutor
from .hermes_exec import HermesExecutor
from .opencode_exec import OpenCodeExecutor

_EXECUTORS = {
    item.name: item
    for item in (
        HermesExecutor(), CodexExecutor(), ClaudeExecutor(), GrokExecutor(),
        OpenCodeExecutor(),
    )
}


def get_executor(name: str) -> Executor:
    """Return the named executor or raise ``ValueError`` for an unknown harness."""
    try:
        return _EXECUTORS[name]
    except KeyError as exc:
        raise ValueError(f"unknown Factory executor: {name}") from exc


__all__ = [
    "Executor", "HermesExecutor", "CodexExecutor", "ClaudeExecutor",
    "GrokExecutor", "OpenCodeExecutor", "get_executor",
]
