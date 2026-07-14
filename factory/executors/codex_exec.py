"""Codex local executor, following Paperclip's JSONL invocation semantics."""

from __future__ import annotations

import json
import re

from .base import Executor, token_usage, write_identity


class CodexExecutor(Executor):
    """Run Codex in a workspace-write sandbox with JSON event output."""

    name = "codex"

    def build_cmd(self, seat, prompt: str, workspace: str) -> list[str]:
        """Build the Paperclip-style ``codex exec --json`` argv."""
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "-s", "workspace-write"]
        if seat.model:
            cmd += ["--model", seat.model]
        if getattr(seat, "reasoning", ""):
            cmd += ["-c", f'model_reasoning_effort={json.dumps(seat.reasoning)}']
        return cmd + ["-"]

    def parse_usage(self, log_text: str) -> dict:
        """Parse Codex JSONL usage, then its human ``tokens used`` fallback."""
        tokens_in = tokens_out = 0
        for line in log_text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") or event.get("total_usage") or {}
            if not isinstance(usage, dict):
                continue
            tokens_in = max(tokens_in, int(usage.get("input_tokens", usage.get("input", 0)) or 0))
            tokens_out = max(tokens_out, int(usage.get("output_tokens", usage.get("output", 0)) or 0))
        if not tokens_in and not tokens_out:
            match = re.search(r"tokens\s+used\s*\n\s*([\d,]+)", log_text, re.I)
            if match:
                return token_usage(0, int(match.group(1).replace(",", "")))
        return token_usage(tokens_in, tokens_out)

    def identity_files(self, seat, workspace: str) -> None:
        """Place the profile's Codex-recognized ``AGENTS.md`` at workspace root."""
        write_identity(seat, workspace, "AGENTS.md")

    def extract_text(self, log_text: str) -> str:
        """Concatenate ``agent_message`` texts from Codex ``--json`` JSONL.

        The sentinel line lives inside the last agent message's ``text``
        field; the raw log ends with ``turn.completed`` machine events
        (finding #23). Falls back to the raw log when no agent messages
        are found (e.g. startup crash before any turn).
        """
        texts: list[str] = []
        for line in log_text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
        return "\n".join(texts) if texts else log_text
