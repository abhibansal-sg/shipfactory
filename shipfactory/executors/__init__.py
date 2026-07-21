"""Factory executor registry."""

from typing import Any

from .base import COMMON_KEYS, Executor
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


def executor_config_keys(name: str) -> frozenset[str]:
    """Return the full config-key allowlist (adapter-specific + common) for an executor."""
    executor = get_executor(name)
    return frozenset(getattr(executor, "CONFIG_KEYS", frozenset())) | COMMON_KEYS


def validate_seat_config(executor: str, config: Any) -> None:
    """Reject a seat config key the chosen executor does not own.

    Called only after the executor-membership check, so ``get_executor`` here
    never raises for an unknown executor.
    """
    from shipfactory.config import FactoryConfigError

    if not isinstance(config, dict):
        raise FactoryConfigError("seat config must be a mapping")
    allowed = executor_config_keys(executor)
    for key in config:
        if key not in allowed:
            raise FactoryConfigError(
                f"executor {executor!r} rejects config key {key!r}; "
                f"allowed keys are {sorted(allowed)}"
            )


__all__ = [
    "Executor", "HermesExecutor", "CodexExecutor", "ClaudeExecutor",
    "GrokExecutor", "OpenCodeExecutor", "get_executor",
    "executor_config_keys", "validate_seat_config",
]
