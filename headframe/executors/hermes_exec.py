"""Hermes executor placeholder; Hermes spawning is delegated to kanban."""

from __future__ import annotations

from .base import Executor, token_usage


class HermesExecutor(Executor):
    """Represent Hermes-native execution for registry and telemetry purposes."""

    name = "hermes"

    def build_cmd(self, seat, prompt: str, workspace: str) -> list[str]:
        """Return the equivalent native worker argv (normally not launched here)."""
        return ["hermes", "-p", seat.profile, "chat", "-q", prompt]

    def parse_usage(self, log_text: str) -> dict:
        """Hermes session usage is queried elsewhere; no log fallback exists."""
        return token_usage()

    def identity_files(self, seat, workspace: str) -> None:
        """Hermes loads profile identity itself, so this is intentionally a no-op."""
