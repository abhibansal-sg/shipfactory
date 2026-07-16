"""Factory dispatcher spawn function and harness reaper."""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep, time_ns
from typing import Any

from shipfactory.executors import get_executor

_RESULT_RE = re.compile(r"^SHIPFACTORY_RESULT:\s*(done|blocked)\s+(.+?)\s*$", re.I)
_VERDICT_RE = re.compile(r"^SHIPFACTORY_VERDICT:\s*\{.*\}\s*$")
_RUNNING: dict[int, dict[str, Any]] = {}
_WORKER_LEASE_SECONDS = 300
_START_TOKEN_OBSERVATION_SECONDS = 2.0


logger = logging.getLogger(__name__)


class WorkerCapacityExhausted(RuntimeError):
    """Queue-only signal: no worker slot was available for this claim."""


class AccessModeResolutionError(RuntimeError):
    """A recipe step's declared ``access_mode`` could not be trusted.

    A step that IS declared ``readonly`` must never spawn unprotected just
    because a lookup happened to fail — the ambiguity itself is the danger
    (finding #1). Only a genuinely absent recipe step (a bare kanban task)
    is a legitimate ``None``; a DB error, unparsable recipe JSON, or a
    dangling step definition all raise here instead, and
    :func:`shipfactory_spawn` lets that abort the spawn rather than
    treating the unresolved step as if it were unrestricted.
    """


def _store_module() -> Any:
    return importlib.import_module("shipfactory.store")


def _value(task: Any, field: str, default: Any = None) -> Any:
    """Read a field from either Hermes's Task dataclass or a test mapping."""
    if isinstance(task, dict):
        return task.get(field, default)
    return getattr(task, field, default)


def _shipfactory_home() -> Path:
    """Return Factory's state root under the configured Hermes home."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "shipfactory"


def _worker_environment(root: Path, *, board: str | None, task_id: str) -> dict[str, str]:
    """Construct a worker environment that cannot disclose Factory key paths.

    The real Hermes home is replaced with an empty workspace-local worker home,
    and any ambient variable that looks like signing-key material is removed.
    Key bytes are never placed in process arguments, prompts, or environment.
    """
    # Readonly recipe workspaces leave this Factory output subtree writable.
    worker_home = root / ".shipfactory-output" / ".worker-home"
    worker_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    excluded_fragments = ("SIGNING_KEY", "HMAC_KEY", "DECISION_KEY")
    env = {
        key: value for key, value in os.environ.items()
        if key != "HERMES_HOME" and not any(fragment in key.upper() for fragment in excluded_fragments)
    }
    env.update({
        "HERMES_HOME": str(worker_home),
        "HERMES_KANBAN_TASK": str(task_id),
        "HERMES_KANBAN_WORKSPACE": str(root),
        "HERMES_KANBAN_BOARD": str(board or ""),
        "TERMINAL_CWD": str(root),
    })
    return env


def _worker_prompt(context: str) -> str:
    """Attach the Factory terminal-result protocol to worker context."""
    return (
        f"{context.rstrip()}\n\n"
        "## Factory completion protocol\n"
        "Complete the assigned work in this workspace. Your LAST output line MUST be exactly "
        "`SHIPFACTORY_RESULT: done <one-line summary>` on success, or "
        "`SHIPFACTORY_RESULT: blocked <one-line reason>` when blocked. "
        "Do not omit this line.\n"
    )


def _process_start_token(pid: int) -> str | None:
    """Return an OS start identity so PID reuse cannot adopt the wrong process."""
    try:
        import psutil
        return f"psutil:{psutil.Process(int(pid)).create_time():.6f}"
    except Exception:
        pass
    try:
        value = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            text=True, timeout=2,
        ).strip()
    except Exception:
        value = ""
    if value:
        return f"ps:{value}"
    return None


def _capture_start_token(pid: int, proc: Any | None = None) -> str | None:
    """Observe a new process for up to two seconds with bounded backoff."""
    deadline = monotonic() + _START_TOKEN_OBSERVATION_SECONDS
    delay = 0.02
    while True:
        token = _process_start_token(pid)
        if token is not None:
            return token
        if proc is not None and proc.poll() is not None:
            return f"exited-before-identity:{pid}:{time_ns()}"
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(delay, remaining))
        delay = min(delay * 2, 0.25)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe used only when an OS start token is absent."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def verified_killpg(pid: int | None, token: str | None, sig: int = signal.SIGKILL) -> bool:
    """Signal a process group only after reconfirming its OS start identity.

    A PID (and its process group) can be reused by an unrelated process
    between whenever it was last observed alive and the moment we act on
    that observation — the same race ``_AdoptedProcess``/``restore_running``
    guard against on daemon restart, just at a shorter timescale (one poll
    to the next signal). This re-probes ``token`` immediately before the
    ``killpg`` call so a stale identity is never signalled. When no token is
    available (degraded environment with neither ``psutil`` nor ``ps``),
    falls back to a liveness check rather than skipping the signal entirely.
    """
    if not pid:
        return False
    pid = int(pid)
    if token is not None:
        if _process_start_token(pid) != token:
            return False
    elif not _pid_alive(pid):
        return False
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


class _AdoptedProcess:
    """Minimal pollable handle for a worker whose original parent restarted."""

    def __init__(self, pid: int, token: str | None):
        self.pid = int(pid)
        self._token = token

    def poll(self) -> int | None:
        if self._token is None:
            return None if _pid_alive(self.pid) else 255
        return None if _process_start_token(self.pid) == self._token else 255


def _runtime_max_workers(cfg: Any) -> int:
    try:
        from shipfactory.config import recipe_runtime_config
        return int(recipe_runtime_config(getattr(cfg, "recipes", None))["max_workers"])
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return 2


def _runtime_artifact_max_bytes() -> int:
    """Return the validated operator limit, or the ratified 2 MiB default."""
    try:
        from shipfactory.config import load_seats, recipe_runtime_config
        return int(recipe_runtime_config(getattr(load_seats(), "recipes", None))[
            "artifact_max_bytes"
        ])
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError):
        return 2 * 1024 * 1024


def _duration_since(started_at: str | None) -> float:
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _legacy_transition(record: dict[str, Any], result: str, summary: str) -> None:
    """Compatibility path for isolated tests without the A0 journal module."""
    from hermes_cli import kanban_db
    conn = kanban_db.connect(board=record["board"])
    try:
        if result == "done":
            try:
                kanban_db.complete_task(
                    conn, record["task_id"], result=summary, summary=summary,
                )
            except TypeError:
                kanban_db.complete_task(conn, record["task_id"], summary=summary)
        else:
            kanban_db.block_task(conn, record["task_id"], reason=summary)
    finally:
        conn.close()


def _plan_worker_transition(record: dict[str, Any], result: str, summary: str) -> None:
    """Journal a board transition; never silently swallow a reap failure."""
    store = _store_module()
    if not hasattr(store, "_connect"):
        _legacy_transition(record, result, summary)
        return
    try:
        from shipfactory.recipes.advancer import plan_worker_transition
    except (ImportError, AttributeError):
        _legacy_transition(record, result, summary)
        return
    plan_worker_transition(
        run_id=int(record["run_id"]), task_id=str(record["task_id"]),
        board=record.get("board"), result=result, summary=summary,
        process_start_token=record.get("process_start_token"),
        task_attempt_id=record.get("task_attempt_id"),
    )


def _drain_worker_transitions(boards: set[str | None] | None = None) -> None:
    """Execute journaled worker transitions with probe-before-retry semantics."""
    store = _store_module()
    if not hasattr(store, "_connect"):
        return
    try:
        from shipfactory.recipes.advancer import run_action_intents
        from hermes_cli import kanban_db
    except (ImportError, AttributeError):
        return
    if boards is None:
        store.init_db()
        with store._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT json_extract(payload_json,'$.board') AS board "
                "FROM action_intents WHERE kind='worker_task_transition' "
                "AND state IN ('planned','retryable_failed')"
            ).fetchall()
        boards = {row["board"] for row in rows}
    for board in boards:
        conn = kanban_db.connect(board=board)
        try:
            run_action_intents(
                conn, board=board, kinds={"worker_task_transition"}, limit=100,
            )
        finally:
            conn.close()


def _step_access_mode(task_id: str) -> str | None:
    """Return the declared ``access_mode`` for *task_id*'s pinned recipe step.

    ``access_mode`` is validated for shape by the v2 recipe loader but was
    never consulted at spawn time — every executor ran ``workspace-write``
    regardless of a step's declared ``readonly`` (finding #34). This is the
    lookup the enforcement boundary needs; a task with no recipe step (a
    bare kanban task) returns ``None`` and is unaffected.

    Raises :class:`AccessModeResolutionError`, rather than returning
    ``None``, for any failure that could be masking a real ``readonly``
    declaration — a DB error, unparsable recipe JSON, a step definition
    missing from the pinned recipe, or malformed ``params`` all abort the
    caller's spawn instead of silently running unprotected (finding #1,
    the fail-open gap the cross-lab review found).
    """
    store = _store_module()
    if not hasattr(store, "_connect"):
        return None
    try:
        with store._connect() as db:
            row = db.execute(
                "SELECT s.step_id,i.recipe_id,i.recipe_version,v.normalized_yaml "
                "FROM recipe_steps s JOIN recipe_instances i ON i.id=s.instance_id "
                "JOIN recipe_versions v ON v.id=i.recipe_id AND v.version=i.recipe_version "
                "WHERE s.kanban_task_id=?",
                (str(task_id),),
            ).fetchone()
    except Exception as exc:
        raise AccessModeResolutionError(
            f"access_mode lookup failed for task {task_id}: {exc}"
        ) from exc
    if row is None:
        return None
    try:
        recipe = json.loads(row["normalized_yaml"])
    except (TypeError, ValueError) as exc:
        raise AccessModeResolutionError(
            f"access_mode recipe parse failed for task {task_id}: {exc}"
        ) from exc
    definition = next(
        (item for item in recipe.get("steps", []) if item.get("id") == row["step_id"]),
        None,
    )
    if not isinstance(definition, dict):
        raise AccessModeResolutionError(
            f"access_mode step definition missing for task {task_id} "
            f"step {row['step_id']!r}"
        )
    params = definition.get("params")
    if params is not None and not isinstance(params, dict):
        raise AccessModeResolutionError(
            f"access_mode params malformed for task {task_id} step {row['step_id']!r}"
        )
    return params.get("access_mode") if isinstance(params, dict) else None


# ``access_mode: readonly`` enforcement is filesystem permission bits, not a
# privilege or sandbox boundary: the worker runs under the SAME UID as this
# call, and standard POSIX permission bits are the owning user's own
# property to change. A worker that runs ``chmod u+w <file>`` before writing
# restores its own write access exactly as freely as this function removed
# it — that trivial bypass is real and is NOT closed by this mechanism
# (finding #1). Closing it for real would require running the executor
# under its own sandbox/privilege boundary (a different UID, a container, or
# an OS sandbox like macOS Seatbelt / Linux Landlock) — genuinely enforced
# per executor, which this engine does not set up here. So the truthful
# level this function provides is ``"advisory"``, never ``"enforced"``: it
# stops accidental or naive same-UID writes and any writer that genuinely
# runs under a different UID, but not a same-UID adversary that specifically
# re-``chmod``s before writing. This mirrors the SF-8 network-policy
# labeling (`shipfactory/environments.py`'s `_apply_network_policy`, finding
# #7) — callers must record the returned level rather than assuming
# "enforced", and it must be applied identically for every executor
# (codex, claude, hermes), not only the ones with their own build_cmd.
_READONLY_ENFORCEMENT_LEVEL = "advisory"


def _enforce_readonly_workspace(root: Path) -> str:
    """Deny filesystem writes outside the declared artifact output directory.

    ``access_mode: readonly`` is only a real boundary if the OS backs it —
    prompt wording alone is not a security boundary. The declared
    ``.shipfactory-output/`` directory stays writable so a readonly step
    (explore, spec-attack, plan-attack) can still seal its result or emit a
    verdict; every other file and directory the executor can see is made
    non-writable before it runs (finding #34). Returns the honest
    enforcement level (see :data:`_READONLY_ENFORCEMENT_LEVEL`) so the
    caller can record what was actually applied.
    """
    output_dir = (root / ".shipfactory-output").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        current = Path(dirpath).resolve()
        if current == output_dir:
            dirnames[:] = []
            continue
        for filename in filenames:
            try:
                os.chmod(current / filename, 0o440)
            except OSError:
                pass
        try:
            os.chmod(current, 0o550)
        except OSError:
            pass
    return _READONLY_ENFORCEMENT_LEVEL


def shipfactory_spawn(task, workspace: str, *, board=None) -> int | None:
    """Spawn the configured harness for a claimed kanban task, or skip unknown seats.

    This has the exact ``dispatch_once`` spawn callback signature.  Imports of
    Factory config/store and Hermes kanban are intentionally local so plugin
    loading remains independent of initialization order.
    """
    assignee = _value(task, "assignee")
    if not assignee:
        return None
    from shipfactory.config import load_seats
    store = _store_module()

    cfg = load_seats()
    seat = cfg.seats.get(assignee)
    if seat is None or store.seat_paused(assignee):
        return None
    from hermes_cli import kanban_db

    task_id = _value(task, "id")
    executor = get_executor(seat.executor)
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    logs = _shipfactory_home() / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    log_path = logs / f"{task_id}-{stamp}.log"
    prompt_path = logs / f"{task_id}-{stamp}.prompt"

    # Resolve and enforce access_mode BEFORE any executor-specific branch, so
    # coverage never depends on which harness (codex, claude, hermes) claimed
    # the task (finding #1 — the Hermes path used to bypass this entirely).
    # _step_access_mode raises AccessModeResolutionError, which propagates
    # out of this function, when the declaration is ambiguous — the spawn is
    # blocked outright rather than proceeding unprotected.
    access_mode = _step_access_mode(str(task_id))
    enforcement_level = "not_applicable"
    if access_mode == "readonly":
        enforcement_level = _enforce_readonly_workspace(root)

    if seat.executor == "hermes":
        prompt = f"work kanban task {task_id}"
        prompt_path.write_text(prompt, encoding="utf-8")
        try:
            log_path = kanban_db.worker_logs_dir(board=board) / f"{task_id}.log"
        except (AttributeError, OSError):
            pass
    else:
        conn = kanban_db.connect(board=board)
        try:
            context = kanban_db.build_worker_context(conn, task_id)
        finally:
            conn.close()
        executor.identity_files(seat, str(root))
        prompt = _worker_prompt(context)
        prompt_path.write_text(prompt, encoding="utf-8")

    run_kwargs = {
        "board": board,
        "workspace_path": str(root),
        "log_path": str(log_path),
        "prompt_path": str(prompt_path),
        "provider": seat.executor,
        "resolved_model": seat.model or "",
        "executor_version": str(getattr(executor, "version", "1")),
        "task_attempt_id": _value(task, "current_run_id"),
        "access_enforcement_level": enforcement_level,
    }
    try:
        run_id = store.record_run_start(
            task_id, assignee, seat.executor, seat.model, None, **run_kwargs,
        )
    except TypeError:  # isolated legacy test doubles
        run_id = store.record_run_start(task_id, assignee, seat.executor, seat.model, None)
    lease_key = f"worker_slot:run:{run_id}"
    if hasattr(store, "acquire_resource_lease"):
        acquired = store.acquire_resource_lease(
            "worker_slot", _runtime_max_workers(cfg), key=lease_key,
            lease_seconds=_WORKER_LEASE_SECONDS,
            metadata={"run_id": run_id, "task_id": task_id, "board": board},
        )
        if acquired is None:
            try:
                store.record_run_end(run_id, -1, None, None, 0.0, "capacity_refused")
            except Exception:
                logger.exception("Factory could not record capacity refusal for run %s", run_id)
            raise WorkerCapacityExhausted("worker_slot capacity exhausted")

    proc: Any
    pid: int | None = None
    try:
        if seat.executor == "hermes":
            pid = int(kanban_db._default_spawn(task, workspace, board=board))
            token = _capture_start_token(pid)
            proc = _AdoptedProcess(pid, token)
        else:
            command = executor.build_cmd(seat, prompt, str(root))
            # #16-V1: permit a real-path test/operator harness override without
            # replacing Factory modules or faking subprocess execution.
            override = os.environ.get(f"FACTORY_EXECUTOR_CMD_{seat.executor.upper()}")
            if override:
                command = shlex.split(override)
                if not command:
                    raise ValueError(f"FACTORY_EXECUTOR_CMD_{seat.executor.upper()} is empty")
            env = _worker_environment(root, board=board, task_id=str(task_id))
            log_file = log_path.open("wb")
            prompt_file = prompt_path.open("rb")
            try:
                proc = subprocess.Popen(
                    command, cwd=str(root), stdin=prompt_file, stdout=log_file,
                    stderr=subprocess.STDOUT, env=env, start_new_session=True,
                )
            finally:
                # The child owns duplicated descriptors after Popen; close ours.
                prompt_file.close()
                log_file.close()
            pid = int(proc.pid)
            token = _capture_start_token(pid, proc)
        if hasattr(store, "record_run_spawned"):
            store.record_run_spawned(run_id, pid, token)
        _RUNNING[pid] = {
            "proc": proc, "run_id": run_id, "task_id": task_id,
            "executor": seat.executor, "board": board, "log_path": log_path,
            "prompt_path": prompt_path, "workspace_path": root,
            "process_start_token": token,
            "task_attempt_id": _value(task, "current_run_id"),
            "lease_key": lease_key,
            "started": monotonic(), "started_at": datetime.now(timezone.utc).isoformat(),
            "adopted": seat.executor == "hermes",
        }
    except Exception:
        if pid is not None:
            try:
                os.killpg(pid, 15)
            except ProcessLookupError:
                pass
            except OSError:
                terminate = getattr(locals().get("proc"), "terminate", None)
                if terminate is not None:
                    try:
                        terminate()
                    except Exception:
                        logger.exception("Factory could not terminate failed spawn pid %s", pid)
        try:
            if hasattr(store, "record_run_crashed"):
                store.record_run_crashed(run_id, "spawn failed")
            else:
                store.record_run_end(run_id, -1, None, None, 0.0, "spawn_failed")
        except Exception:
            logger.exception("Factory could not record failed spawn run %s", run_id)
        finally:
            if hasattr(store, "release_resource_lease"):
                try:
                    store.release_resource_lease(lease_key)
                except Exception:
                    logger.exception("Factory could not release failed spawn lease %s", lease_key)
            if pid is not None:
                _RUNNING.pop(pid, None)
        raise
    return pid


def _parse_result(log_text: str, exit_code: int) -> tuple[str, str]:
    """Apply the strict final-line sentinel protocol to a completed harness."""
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    # Recipe review gates use the stronger structured verdict sentinel; the
    # executor-discipline template ALSO demands a trailing SHIPFACTORY_RESULT, so
    # disciplined review workers emit BOTH (verdict, then result). The verdict
    # must win whenever present — storing the RESULT prose as task.result
    # destroys the JSON the advancer's parse_verdict requires and fused every
    # review gate as "review final line must be SHIPFACTORY_VERDICT JSON"
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


def restore_running(*, max_workers: int = 2) -> dict[str, list[int]]:
    """Reconstruct live workers from durable runs and crash dead identities."""
    store = _store_module()

    restored: list[int] = []
    crashed: list[int] = []
    boards: set[str | None] = set()
    rows = store.nonterminal_runs()
    lease_capacity = max(int(max_workers), len(rows), 1)
    for row in rows:
        run_id = int(row["id"])
        pid = int(row["pid"]) if row.get("pid") is not None else 0
        token = row.get("process_start_token")
        lease_key = f"worker_slot:run:{run_id}"
        current = _RUNNING.get(pid)
        if current and int(current.get("run_id", -1)) == run_id:
            if hasattr(store, "renew_resource_lease"):
                store.renew_resource_lease(lease_key, lease_seconds=_WORKER_LEASE_SECONDS)
            restored.append(pid)
            continue
        identity_matches = bool(token) and _process_start_token(pid) == token
        pid_only_matches = token is None and pid > 0 and _pid_alive(pid)
        if pid > 0 and (identity_matches or pid_only_matches):
            if hasattr(store, "acquire_resource_lease"):
                store.acquire_resource_lease(
                    "worker_slot", lease_capacity, key=lease_key,
                    lease_seconds=_WORKER_LEASE_SECONDS,
                    metadata={"run_id": run_id, "task_id": row["task_id"],
                              "board": row.get("board")},
                )
            _RUNNING[pid] = {
                "proc": _AdoptedProcess(pid, token), "run_id": run_id,
                "task_id": row["task_id"], "executor": row["executor"],
                "board": row.get("board"), "log_path": row.get("log_path"),
                "prompt_path": row.get("prompt_path"),
                "workspace_path": row.get("workspace_path"),
                "process_start_token": token,
                "task_attempt_id": row.get("task_attempt_id"),
                "lease_key": lease_key,
                "started_at": row.get("started_at"), "adopted": True,
            }
            restored.append(pid)
            continue
        reason = "pid missing" if pid <= 0 else "pid dead or start token mismatched"
        store.record_run_crashed(run_id, reason)
        if hasattr(store, "release_resource_lease"):
            store.release_resource_lease(lease_key)
        record = {
            "run_id": run_id, "task_id": row["task_id"],
            "board": row.get("board"), "executor": row["executor"],
            "process_start_token": token,
            "task_attempt_id": row.get("task_attempt_id"),
        }
        _plan_worker_transition(record, "blocked", f"worker crashed: {reason}")
        boards.add(row.get("board"))
        crashed.append(run_id)
    if boards:
        _drain_worker_transitions(boards)
    return {"restored": restored, "crashed": crashed}


def _terminal_board_result(record: dict[str, Any]) -> tuple[str, str] | None:
    """Probe a self-completing worker target before inferring from process exit."""
    if not record.get("adopted"):
        return None
    try:
        from hermes_cli import kanban_db
        conn = kanban_db.connect(board=record.get("board"))
        try:
            task = kanban_db.get_task(conn, record["task_id"])
        finally:
            conn.close()
    except Exception:
        return None
    if task and task.status == "done":
        return "done", str(getattr(task, "result", None) or "worker completed")
    if task and task.status == "blocked":
        return "blocked", str(getattr(task, "blocked_reason", None) or "worker blocked")
    return None


def reap_finished() -> list[dict]:
    """Finalize exited harnesses and journal their kanban transitions."""
    store = _store_module()

    _drain_worker_transitions()
    finished: list[dict] = []
    boards: set[str | None] = set()
    for pid, record in list(_RUNNING.items()):
        code = record["proc"].poll()
        if code is None:
            if hasattr(store, "renew_resource_lease"):
                store.renew_resource_lease(
                    record.get("lease_key", f"worker_slot:run:{record['run_id']}"),
                    lease_seconds=_WORKER_LEASE_SECONDS,
                )
            continue
        try:
            log_text = Path(record["log_path"]).read_text(
                encoding="utf-8", errors="replace",
            ) if record.get("log_path") else ""
        except (OSError, TypeError):
            log_text = ""
        board_result = _terminal_board_result(record)
        if board_result is not None:
            result, summary = board_result
        else:
            result, summary = _parse_result(
                get_executor(record["executor"]).extract_text(log_text), code,
            )
        if result == "done" and hasattr(store, "_connect"):
            try:
                from shipfactory.artifacts import seal_declared_outputs_for_task
                seal_declared_outputs_for_task(
                    task_id=str(record["task_id"]), run_id=int(record["run_id"]),
                    workspace=record["workspace_path"],
                    max_bytes=_runtime_artifact_max_bytes(),
                )
            except Exception as exc:
                # A v2 output is part of the worker protocol, not optional
                # prose evidence. Fail closed before the board completion is
                # journaled; the advancer later persists the visible step reason.
                result = "blocked"
                summary = f"worker_failed: artifact sealing rejected: {exc}"[:1000]
        usage = get_executor(record["executor"]).parse_usage(log_text)
        duration = (
            monotonic() - record["started"]
            if record.get("started") is not None
            else _duration_since(record.get("started_at"))
        )
        recorded_code = 0 if code == 255 and result == "done" else code
        store.record_run_end(
            record["run_id"], recorded_code, usage["tokens_in"], usage["tokens_out"],
            duration, result,
        )
        _plan_worker_transition(record, result, summary)
        boards.add(record.get("board"))
        if hasattr(store, "release_resource_lease"):
            store.release_resource_lease(
                record.get("lease_key", f"worker_slot:run:{record['run_id']}")
            )
        outcome = {"pid": pid, "task_id": record["task_id"], "result": result,
                   "summary": summary, "exit_code": recorded_code}
        finished.append(outcome)
        del _RUNNING[pid]
    if boards:
        _drain_worker_transitions(boards)
    return finished


__all__ = [
    "AccessModeResolutionError", "WorkerCapacityExhausted", "shipfactory_spawn",
    "restore_running", "reap_finished",
]
