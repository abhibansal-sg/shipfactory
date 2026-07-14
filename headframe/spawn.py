"""Factory dispatcher spawn function and harness reaper."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from headframe.executors import get_executor

_RESULT_RE = re.compile(r"^HEADFRAME_RESULT:\s*(done|blocked)\s+(.+?)\s*$", re.I)
_VERDICT_RE = re.compile(r"^HEADFRAME_VERDICT:\s*\{.*\}\s*$")
_RUNNING: dict[int, dict[str, Any]] = {}


def _utc_now() -> str:
    """Return a UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _value(task: Any, field: str, default: Any = None) -> Any:
    """Read a field from either Hermes's Task dataclass or a test mapping."""
    if isinstance(task, dict):
        return task.get(field, default)
    return getattr(task, field, default)


def _headframe_home() -> Path:
    """Return Factory's state root under the configured Hermes home."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "headframe"


def _worker_prompt(context: str) -> str:
    """Attach the Factory terminal-result protocol to worker context."""
    return (
        f"{context.rstrip()}\n\n"
        "## Factory completion protocol\n"
        "Complete the assigned work in this workspace. Your LAST output line MUST be exactly "
        "`HEADFRAME_RESULT: done <one-line summary>` on success, or "
        "`HEADFRAME_RESULT: blocked <one-line reason>` when blocked. "
        "Do not omit this line.\n"
    )


def headframe_spawn(task, workspace: str, *, board=None) -> int | None:
    """Spawn the configured harness for a claimed kanban task, or skip unknown seats.

    This has the exact ``dispatch_once`` spawn callback signature.  Imports of
    Factory config/store and Hermes kanban are intentionally local so plugin
    loading remains independent of initialization order.
    """
    assignee = _value(task, "assignee")
    if not assignee:
        return None
    from headframe.config import load_seats
    from headframe import store

    cfg = load_seats()
    seat = cfg.seats.get(assignee)
    if seat is None or store.seat_paused(assignee):
        return None
    if seat.executor == "hermes":
        from hermes_cli.kanban_db import _default_spawn

        return _default_spawn(task, workspace, board=board)

    from hermes_cli import kanban_db

    task_id = _value(task, "id")
    conn = kanban_db.connect(board=board)
    try:
        context = kanban_db.build_worker_context(conn, task_id)
    finally:
        conn.close()
    executor = get_executor(seat.executor)
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    executor.identity_files(seat, str(root))
    prompt = _worker_prompt(context)
    command = executor.build_cmd(seat, prompt, str(root))
    # #16-V1: permit a real-path test/operator harness override without
    # replacing Factory modules or faking subprocess execution.
    override = os.environ.get(f"FACTORY_EXECUTOR_CMD_{seat.executor.upper()}")
    if override:
        command = shlex.split(override)
        if not command:
            raise ValueError(f"FACTORY_EXECUTOR_CMD_{seat.executor.upper()} is empty")
    logs = _headframe_home() / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{task_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.log"
    env = dict(os.environ)
    env.update({
        "HERMES_KANBAN_TASK": str(task_id),
        "HERMES_KANBAN_WORKSPACE": str(root),
        "HERMES_KANBAN_BOARD": str(board or ""),
        "TERMINAL_CWD": str(root),
    })
    log_file = log_path.open("wb")
    prompt_bytes = prompt.encode("utf-8")
    # #16-OPERATOR F1: NEVER write the prompt to the child's stdin from the
    # dispatch thread. Worker context regularly exceeds the 64KB macOS pipe
    # buffer; a slow-starting or crashed harness never drains the pipe and
    # the blocking write() WEDGES THE ENTIRE DAEMON — no exception is ever
    # raised (slowness is not an exception; the #62496 sweeper class).
    # Empirically proven 07-12: 200KB write to a non-reading child blocks
    # indefinitely. Fix: hand the child a real file as stdin.
    prompt_path = logs / f"{task_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.prompt"
    prompt_path.write_bytes(prompt_bytes)
    prompt_file = prompt_path.open("rb")
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=prompt_file,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    finally:
        # The child owns duplicated descriptors after Popen; close ours.
        prompt_file.close()
        log_file.close()
    run_id = store.record_run_start(task_id, assignee, seat.executor, seat.model, proc.pid)
    _RUNNING[proc.pid] = {
        "proc": proc, "run_id": run_id, "task_id": task_id, "executor": seat.executor,
        "board": board, "log_path": log_path, "started": monotonic(),
    }
    return proc.pid


def _parse_result(log_text: str, exit_code: int) -> tuple[str, str]:
    """Apply the strict final-line sentinel protocol to a completed harness."""
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    # Recipe review gates use the stronger structured verdict sentinel; the
    # executor-discipline template ALSO demands a trailing HEADFRAME_RESULT, so
    # disciplined review workers emit BOTH (verdict, then result). The verdict
    # must win whenever present — storing the RESULT prose as task.result
    # destroys the JSON the advancer's parse_verdict requires and fused every
    # review gate as "review final line must be HEADFRAME_VERDICT JSON"
    # (finding #25, t_10fdf585). Scan from the end so the latest verdict wins.
    for line in reversed(lines):
        if _VERDICT_RE.fullmatch(line):
            return "done", line
    match = _RESULT_RE.match(lines[-1]) if lines else None
    if match:
        return match.group(1).lower(), match.group(2)
    if exit_code == 0:
        return "blocked", "no result sentinel"
    return "blocked", f"harness exited with code {exit_code}"


def reap_finished() -> list[dict]:
    """Finalize exited non-Hermes harnesses and transition their kanban tasks."""
    from headframe import store

    finished: list[dict] = []
    for pid, record in list(_RUNNING.items()):
        code = record["proc"].poll()
        if code is None:
            continue
        try:
            log_text = Path(record["log_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
        result, summary = _parse_result(
            get_executor(record["executor"]).extract_text(log_text), code
        )
        usage = get_executor(record["executor"]).parse_usage(log_text)
        store.record_run_end(
            record["run_id"], code, usage["tokens_in"], usage["tokens_out"],
            monotonic() - record["started"], result,
        )
        try:
            from hermes_cli import kanban_db

            conn = kanban_db.connect(board=record["board"])
            try:
                if result == "done":
                    try:
                        kanban_db.complete_task(conn, record["task_id"], result=summary, summary=summary)
                    except TypeError:  # frozen older/stub callback contract
                        kanban_db.complete_task(conn, record["task_id"], summary=summary)
                else:
                    kanban_db.block_task(conn, record["task_id"], reason=summary)
            finally:
                conn.close()
        except Exception:
            # Persisting the run is more important than a transient board write;
            # the normal kanban crash/stale detection will recover the claim.
            pass
        outcome = {"pid": pid, "task_id": record["task_id"], "result": result, "summary": summary, "exit_code": code}
        finished.append(outcome)
        del _RUNNING[pid]
    return finished


__all__ = ["headframe_spawn", "reap_finished"]
