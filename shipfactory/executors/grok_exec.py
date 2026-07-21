"""Grok (xAI) local executor driving the grok CLI in headless JSON mode.

Grok is a distinct provider family from codex and claude, so a codex builder
paired with a grok reviewer satisfies the cross-provider review law with no
Anthropic involvement. The CLI authenticates via its own session (no API key
in Factory config), and ~/.grok permission_mode=always-approve keeps it
non-interactive.
"""

from __future__ import annotations

import json
import re

from .base import Executor, token_usage, write_identity


def _last_json_object(log_text: str) -> dict:
    """Return grok's single ``--output-format json`` object from the log.

    spawn merges stderr into stdout, so a provenance/setup message may sit
    ahead of the JSON; parse the whole log first, then fall back to the
    outermost brace span.
    """
    stripped = log_text.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\{.*\}", stripped, re.S)
    if match:
        try:
            parsed = json.loads(match.group())
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


class GrokExecutor(Executor):
    """Run the grok CLI single-turn, reading its prompt from stdin."""

    name = "grok"
    CONFIG_KEYS = frozenset({"permission_mode"})

    def build_cmd(self, seat, prompt: str, workspace: str) -> list[str]:
        """Build the headless ``grok --prompt-file /dev/stdin`` argv.

        The prompt arrives on stdin (spawn pipes the durable .prompt file);
        /dev/stdin lets grok consume it as its single-turn prompt without an
        argv length limit — review prompts carry inlined sealed inputs and
        routinely exceed ARG_MAX.
        """
        cmd = ["grok", "--prompt-file", "/dev/stdin", "--output-format", "json"]
        if seat.model:
            cmd += ["-m", seat.model]
        return cmd

    def parse_usage(self, log_text: str) -> dict:
        """Read token usage from grok's single JSON result object."""
        usage = _last_json_object(log_text).get("usage")
        if isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage):
            return token_usage(usage.get("input_tokens"), usage.get("output_tokens"))
        return token_usage()

    def identity_files(self, seat, workspace: str) -> None:
        """Place profile instructions in the conventional ``AGENTS.md``."""
        write_identity(seat, workspace, "AGENTS.md")

    def extract_text(self, log_text: str) -> str:
        """Return grok's final agent text (where the sentinel lives).

        ``--output-format json`` emits one object whose ``text`` field is the
        agent's final message; the raw log is not line-delimited JSON, so the
        codex/claude per-line parsing would miss the sentinel (finding #23).
        """
        text = _last_json_object(log_text).get("text")
        if isinstance(text, str) and text.strip():
            return text
        return log_text
