"""Factory executor registry."""

from .base import Executor
from .claude_exec import ClaudeExecutor
from .codex_exec import CodexExecutor
from .hermes_exec import HermesExecutor

_EXECUTORS = {item.name: item for item in (HermesExecutor(), CodexExecutor(), ClaudeExecutor())}


def get_executor(name: str) -> Executor:
    """Return the named executor or raise ``ValueError`` for an unknown harness."""
    try:
        return _EXECUTORS[name]
    except KeyError as exc:
        raise ValueError(f"unknown Factory executor: {name}") from exc


__all__ = ["Executor", "HermesExecutor", "CodexExecutor", "ClaudeExecutor", "get_executor"]
