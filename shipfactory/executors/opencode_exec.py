"""OpenCode local executor for provider-backed coding models.

OpenCode is the harness; the seat model selects the provider/model pair (for
example ``zai-coding-plan/glm-5.2``). It emits JSONL events in headless
``run`` mode, with assistant text in ``type=text`` events and per-turn usage
in ``type=step_finish`` events.
"""

from __future__ import annotations

import json

from .base import Executor, token_usage, write_identity


class OpenCodeExecutor(Executor):
    """Run OpenCode headlessly with its raw JSON event stream."""

    name = "opencode"

    def build_cmd(self, seat, prompt: str, workspace: str) -> list[str]:
        """Build a non-interactive ``opencode run`` invocation.

        The Factory pipes its durable prompt file to stdin. ``--pure`` keeps
        ambient third-party plugins out of the worker trust boundary, while
        the built-in ``build`` agent retains normal workspace tools. We do
        not use ``--auto``: an unexpected permission request must fail closed
        rather than expanding access beyond the assigned workspace.
        """
        cmd = [
            "opencode", "run", "--pure", "--format", "json",
            "--agent", "build", "--dir", workspace,
        ]
        if seat.model:
            cmd += ["--model", seat.model]
        if getattr(seat, "reasoning", ""):
            cmd += ["--variant", seat.reasoning]
        return cmd

    def parse_usage(self, log_text: str) -> dict:
        """Sum input/output usage across OpenCode's completed model turns."""
        tokens_in = tokens_out = 0
        observed = False
        for line in log_text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "step_finish":
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            usage = part.get("tokens")
            if not isinstance(usage, dict):
                continue
            if "input" in usage or "output" in usage:
                observed = True
            tokens_in += int(usage.get("input", 0) or 0)
            tokens_out += int(usage.get("output", 0) or 0)
        return token_usage(tokens_in, tokens_out) if observed else token_usage()

    def identity_files(self, seat, workspace: str) -> None:
        """Place profile instructions in OpenCode's ``AGENTS.md`` channel."""
        write_identity(seat, workspace, "AGENTS.md")

    def extract_text(self, log_text: str) -> str:
        """Concatenate assistant text blocks from OpenCode JSONL events."""
        texts: list[str] = []
        for line in log_text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "text":
                continue
            part = event.get("part")
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value)
        return "\n".join(texts) if texts else log_text
