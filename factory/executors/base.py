"""Common executor contract for Factory harnesses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Keep this lane importable while config.py is being built.
    from factory.config import Seat


class Executor(ABC):
    """Build and account for a single local coding-harness invocation."""

    name: str

    @abstractmethod
    def build_cmd(self, seat: "Seat", prompt: str, workspace: str) -> list[str]:
        """Return the argv used to run *seat* against *prompt*."""

    @abstractmethod
    def parse_usage(self, log_text: str) -> dict:
        """Return input, output, and total token counts (zero when unknown)."""

    @abstractmethod
    def identity_files(self, seat: "Seat", workspace: str) -> None:
        """Materialize the seat's identity instructions in *workspace*."""

    def extract_text(self, log_text: str) -> str:
        """Return the harness's agent-visible text from its raw log.

        Plain-text harnesses return the log unchanged. JSONL harnesses
        (codex ``--json``, claude ``stream-json``) MUST override this to
        pull the agent message text out of the event stream — otherwise
        the FACTORY_RESULT / FACTORY_VERDICT sentinel protocol can never
        match, because the raw log's last line is a machine event like
        ``{"type":"turn.completed",...}`` (finding #23, 2026-07-14).
        """
        return log_text


def token_usage(tokens_in: int = 0, tokens_out: int = 0) -> dict:
    """Return the canonical Factory usage mapping with non-negative values."""
    tokens_in, tokens_out = max(0, int(tokens_in)), max(0, int(tokens_out))
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
    }


def profile_instruction(seat: "Seat", filename: str) -> str:
    """Read a profile instruction file, tolerating a minimal test install."""
    try:
        from hermes_cli.profiles import get_profile_dir

        candidate = Path(get_profile_dir(seat.profile)) / filename
    except Exception:
        # Hermes profiles normally live below HERMES_HOME/profiles.  This
        # fallback also makes identity injection deterministic in isolated tests.
        import os

        candidate = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "profiles" / seat.profile / filename
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_identity(seat: "Seat", workspace: str, filename: str) -> None:
    """Copy profile identity into the workspace without creating a CWD elsewhere."""
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    source = profile_instruction(seat, filename)
    if not source and filename == "CLAUDE.md":
        source = profile_instruction(seat, "AGENTS.md")
    if source:
        (root / filename).write_text(source, encoding="utf-8")
