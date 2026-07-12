"""Claude local executor, following Paperclip stream-JSON semantics."""

from __future__ import annotations

import json
import re

from .base import Executor, token_usage, write_identity


class ClaudeExecutor(Executor):
    """Run Claude headlessly with a stream-JSON transcript."""

    name = "claude"

    def build_cmd(self, seat, prompt: str, workspace: str) -> list[str]:
        """Build Paperclip's noninteractive Claude argv."""
        cmd = ["claude", "--print", "-", "--output-format", "stream-json", "--verbose"]
        if seat.model:
            cmd += ["--model", seat.model]
        if getattr(seat, "reasoning", ""):
            cmd += ["--effort", seat.reasoning]
        # CLAUDE.md provides persistent identity; --add-dir mirrors the adapter
        # and keeps workspace files within Claude's allowed filesystem scope.
        return cmd + ["--add-dir", workspace]

    def parse_usage(self, log_text: str) -> dict:
        """Parse Claude JSON/stream-JSON usage records with regex fallback."""
        tokens_in = tokens_out = 0
        for line in log_text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidates = [event, event.get("usage", {})]
            message = event.get("message")
            if isinstance(message, dict):
                candidates.append(message.get("usage", {}))
            for usage in candidates:
                if not isinstance(usage, dict):
                    continue
                tokens_in = max(tokens_in, int(usage.get("input_tokens", 0) or 0))
                tokens_out = max(tokens_out, int(usage.get("output_tokens", 0) or 0))
        if not tokens_in and not tokens_out:
            match = re.search(r'"input_tokens"\s*:\s*(\d+).*?"output_tokens"\s*:\s*(\d+)', log_text, re.S)
            if match:
                tokens_in, tokens_out = map(int, match.groups())
        return token_usage(tokens_in, tokens_out)

    def identity_files(self, seat, workspace: str) -> None:
        """Place profile instructions in Claude's conventional ``CLAUDE.md``."""
        write_identity(seat, workspace, "CLAUDE.md")
