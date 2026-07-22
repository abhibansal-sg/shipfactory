"""SF-8 deterministic environment provisioning and app sessions.

The runtime manifest is trusted only from the instance's base commit,
pinned by git blob SHA (review §2.1.1). Bootstrap/seed/app-start scripts
are never executed from a candidate-modifiable working tree: their bytes
are read from the pinned base commit and materialized into a Factory-owned
scratch directory before exec. Materialization and app-up both run as
supervised child processes reaped by the daemon tick, never synchronously
inside it (review, correction to the brief).
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any

import yaml

from shipfactory import spawn as _spawn
from shipfactory import store

RUNTIME_SCHEMA = "shipfactory.runtime/v1"
DEFAULT_MANIFEST_PATH = ".shipfactory/runtime.yaml"

_TOP = {"schema", "bootstrap", "app", "seed"}
_BOOTSTRAP = {"argv", "tracked_inputs", "network"}
_APP = {"start_argv", "healthcheck", "stop_signal"}
_HEALTHCHECK = {"path", "expected_status"}
_SEED = {"argv"}
_NETWORK_VALUES = {"allow", "deny"}
_STOP_SIGNALS = {"TERM", "INT", "KILL", "HUP", "QUIT"}
_BLOB_MODES = {"100644", "100755"}


class EnvironmentError(RuntimeError):
    """Base error for the environment-session subsystem."""


class ManifestError(EnvironmentError):
    """The runtime manifest is missing, malformed, or fails schema validation."""


class PathSafetyError(EnvironmentError):
    """A referenced script violates path/process safety (§2.1.5)."""


def _error(cls: type[EnvironmentError], message: str) -> None:
    raise cls(message)


def _require_exact(document: Any, keys: set[str], label: str) -> None:
    if not isinstance(document, dict):
        _error(ManifestError, f"{label} must be a mapping")
    unknown = sorted(set(document) - keys)
    missing = sorted(keys - set(document))
    if unknown:
        _error(ManifestError, f"{label} has unknown keys: {', '.join(unknown)}")
    if missing:
        _error(ManifestError, f"{label} is missing keys: {', '.join(missing)}")


def _require_argv(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        _error(ManifestError, f"{label} must be a non-empty list of non-empty strings")


def validate_runtime_manifest(document: Any) -> dict[str, Any]:
    """Strictly validate a ``shipfactory.runtime/v1`` document; never coerce."""
    _require_exact(document, _TOP, "runtime manifest")
    if document["schema"] != RUNTIME_SCHEMA:
        _error(ManifestError, f"unsupported runtime schema {document['schema']!r}")
    bootstrap = document["bootstrap"]
    _require_exact(bootstrap, _BOOTSTRAP, "bootstrap")
    _require_argv(bootstrap["argv"], "bootstrap.argv")
    if not isinstance(bootstrap["tracked_inputs"], list) or not all(
        isinstance(item, str) and item for item in bootstrap["tracked_inputs"]
    ):
        _error(ManifestError, "bootstrap.tracked_inputs must be a list of strings")
    if bootstrap["network"] not in _NETWORK_VALUES:
        _error(ManifestError, "bootstrap.network must be allow or deny")
    app = document["app"]
    _require_exact(app, _APP, "app")
    _require_argv(app["start_argv"], "app.start_argv")
    healthcheck = app["healthcheck"]
    _require_exact(healthcheck, _HEALTHCHECK, "app.healthcheck")
    if not isinstance(healthcheck["path"], str) or not healthcheck["path"].startswith("/"):
        _error(ManifestError, "app.healthcheck.path must be an absolute HTTP path")
    status = healthcheck["expected_status"]
    if not isinstance(status, int) or isinstance(status, bool) or not (100 <= status <= 599):
        _error(ManifestError, "app.healthcheck.expected_status must be a valid HTTP status")
    if app["stop_signal"] not in _STOP_SIGNALS:
        _error(ManifestError, f"app.stop_signal must be one of {sorted(_STOP_SIGNALS)}")
    seed = document["seed"]
    _require_exact(seed, _SEED, "seed")
    _require_argv(seed["argv"], "seed.argv")
    for label, argv in (
        ("bootstrap.argv", bootstrap["argv"]), ("app.start_argv", app["start_argv"]),
        ("seed.argv", seed["argv"]),
    ):
        _repo_relative_path(argv[0], label)
    for path in bootstrap["tracked_inputs"]:
        _repo_relative_path(path, "bootstrap.tracked_inputs")
    return document


def _repo_relative_path(path: str, label: str) -> str:
    parsed = PurePosixPath(path)
    if (
        not path or not path.strip() or parsed.is_absolute() or "\\" in path
        or ".." in parsed.parts
    ):
        _error(PathSafetyError, f"{label} {path!r} is not a safe repo-relative path")
    return path


def _git(repo_root: Path, *args: str, timeout: float = 10.0) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo_root, text=True, stderr=subprocess.PIPE, timeout=timeout,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ManifestError(f"git {' '.join(args)} failed: {exc.stderr}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvironmentError(f"git {' '.join(args)} unavailable: {exc}") from exc


def _ls_tree_blob(repo_root: Path, ref: str, path: str) -> tuple[str, str]:
    """Return ``(mode, blob_sha)`` for ``path`` at ``ref``, fail-closed otherwise."""
    out = _git(repo_root, "ls-tree", ref, "--", path)
    if not out:
        raise ManifestError(f"{path!r} is not present in the pinned tree {ref}")
    header, _, _name = out.partition("\t")
    mode, kind, blob_sha = header.split()
    if kind != "blob":
        raise PathSafetyError(f"{path!r} is not a regular tracked file at {ref}")
    if mode not in _BLOB_MODES:
        raise PathSafetyError(f"{path!r} has unsafe git mode {mode} (symlink or special file)")
    return mode, blob_sha


def _blob_bytes(repo_root: Path, blob_sha: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "cat-file", "-p", blob_sha], cwd=repo_root, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        raise ManifestError(f"git cat-file {blob_sha} failed: {exc}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvironmentError(f"git cat-file unavailable: {exc}") from exc


def control_plane_paths(document: dict[str, Any], manifest_path: str) -> frozenset[str]:
    """Return the set of repo paths whose modification is control-plane risk."""
    return frozenset({
        manifest_path,
        document["bootstrap"]["argv"][0],
        document["app"]["start_argv"][0],
        document["seed"]["argv"][0],
    })


class RuntimeManifest:
    """A validated manifest pinned to one trusted base commit."""

    def __init__(self, *, document: dict[str, Any], blob_sha: str, base_sha: str,
                manifest_path: str):
        self.document = document
        self.blob_sha = blob_sha
        self.base_sha = base_sha
        self.manifest_path = manifest_path
        self.control_paths = control_plane_paths(document, manifest_path)


def load_runtime_manifest(
    repo_root: str | Path, base_sha: str, manifest_path: str = DEFAULT_MANIFEST_PATH,
) -> RuntimeManifest:
    """Read, parse, and validate the manifest from the trusted base commit only.

    Never reads ``manifest_path`` off the working tree — a candidate that
    edited its local checkout must not influence what executes.
    """
    repo_root = Path(repo_root)
    manifest_path = _repo_relative_path(manifest_path, "manifest_path")
    _mode, blob_sha = _ls_tree_blob(repo_root, base_sha, manifest_path)
    raw = _blob_bytes(repo_root, blob_sha)
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ManifestError(f"runtime manifest is not valid YAML: {exc}") from exc
    validate_runtime_manifest(document)
    manifest = RuntimeManifest(
        document=document, blob_sha=blob_sha, base_sha=base_sha, manifest_path=manifest_path,
    )
    # Verify every referenced script is itself present & safe in the pinned
    # tree (not just the manifest file) before anything is materialized.
    for path in manifest.control_paths - {manifest_path}:
        _ls_tree_blob(repo_root, base_sha, path)
    return manifest


def _atomic_write_once(path: Path, data: bytes, *, mode: int = 0o700) -> None:
    """Publish ``data`` at ``path`` via a same-directory fsynced temp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    if existing is not None and hashlib.sha256(existing).digest() == hashlib.sha256(data).digest():
        return
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def control_blob_shas(repo_root: str | Path, manifest: RuntimeManifest) -> dict[str, str]:
    """Return ``{repo-relative path: blob_sha}`` for every non-manifest script.

    This is the actual content identity of what will execute: the manifest's
    own blob SHA only covers the YAML file, not the bootstrap/app/seed
    scripts it references by path (review finding #1).
    """
    repo_root = Path(repo_root)
    return {
        path: _ls_tree_blob(repo_root, manifest.base_sha, path)[1]
        for path in sorted(manifest.control_paths - {manifest.manifest_path})
    }


def materialize_pinned_scripts(
    repo_root: str | Path, manifest: RuntimeManifest, dest_root: str | Path,
    *, blob_shas: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Copy every referenced script's pinned bytes into a Factory-owned root.

    Returns a mapping from repo-relative path to its materialized, executable
    location. The candidate workspace is never consulted, so a working tree
    that modified or symlinked these paths after validation has no effect
    (closes the "runtime script becomes a symlink after validation" and
    "candidate modifies its own bootstrap script" attacks).
    """
    repo_root = Path(repo_root)
    dest_root = Path(dest_root) / manifest.blob_sha
    resolved_shas = blob_shas if blob_shas is not None else control_blob_shas(repo_root, manifest)
    materialized: dict[str, Path] = {}
    for path in sorted(manifest.control_paths - {manifest.manifest_path}):
        blob_sha = resolved_shas[path]
        data = _blob_bytes(repo_root, blob_sha)
        dest = dest_root / blob_sha / path
        _atomic_write_once(dest, data, mode=0o700)
        materialized[path] = dest
    return materialized


_MISSING_INPUT_SENTINEL = "missing"


def _read_workspace_file(workspace: Path, rel_path: str, max_bytes: int) -> bytes | None:
    """Read a tracked-input file with no-symlink-follow semantics, or ``None``."""
    parts = PurePosixPath(rel_path).parts
    descriptors: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(
                part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow, dir_fd=current,
            )
            descriptors.append(current)
        fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current)
        descriptors.append(fd)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max(1, int(max_bytes) + 1 - total)))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > int(max_bytes):
                break
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def compute_tracked_input_hash(
    workspace: str | Path, tracked_inputs: list[str], *, max_bytes: int = 10 * 1024 * 1024,
) -> str:
    """Content-address the actual workspace bytes of every tracked input.

    A missing file hashes to a stable sentinel distinct from an empty file,
    so a tracked input appearing/disappearing still changes the key.
    """
    workspace = Path(workspace)
    parts = []
    for rel in sorted(tracked_inputs):
        data = _read_workspace_file(workspace, rel, max_bytes)
        digest = _MISSING_INPUT_SENTINEL if data is None else hashlib.sha256(data).hexdigest()
        parts.append(f"{rel}:{digest}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def materialization_key(
    *, base_sha: str, candidate_sha: str | None, manifest_blob_sha: str,
    tracked_input_hash: str, control_blob_shas: dict[str, str],
) -> str:
    """Return the content-addressed cache key for one materialized environment.

    Must fold in every byte that influences what actually executes. The
    manifest blob SHA alone is not enough: two different base commits can
    carry an identical ``runtime.yaml`` while their referenced bootstrap or
    seed script differs, and a manifest blob hash cannot see that. Folding
    in ``base_sha``, ``candidate_sha``, and each referenced script's blob SHA
    closes that reuse hole (review finding #1) — an existing "ready" row is
    only ever reused when every one of these matches exactly.
    """
    parts = [base_sha, candidate_sha or "", manifest_blob_sha, tracked_input_hash]
    parts.extend(f"{path}={sha}" for path, sha in sorted(control_blob_shas.items()))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def candidate_control_plane_diff(
    repo_root: str | Path, base_sha: str, candidate_sha: str | None,
    control_paths: frozenset[str],
) -> list[str]:
    """Return control-plane paths a candidate commit touched versus base."""
    if not candidate_sha or candidate_sha == base_sha:
        return []
    repo_root = Path(repo_root)
    changed = set(
        line for line in _git(
            repo_root, "diff", "--name-only", base_sha, candidate_sha,
        ).splitlines() if line
    )
    return sorted(changed & set(control_paths))


def _state_root() -> Path:
    return store._db_path().parent


def _scripts_root() -> Path:
    return _state_root() / "env-scripts"


def _logs_root() -> Path:
    return _state_root() / "env-logs"


def _kill_group(pid: int | None, sig: int = signal.SIGKILL, *, token: str | None = None) -> None:
    """Signal a process group after reconfirming its OS start identity.

    Every call site passes the ``token`` captured at spawn (the A1
    run-identity pattern already used for daemon-restart adoption) so a
    PID/group reused by an unrelated process between an earlier poll and
    this signal is never killed on a stale identity (review finding #3).
    Delegates to :func:`shipfactory.spawn.verified_killpg`, the one shared
    implementation of this check, rather than reinventing it here.
    """
    if not pid:
        return
    _spawn.verified_killpg(int(pid), token, sig)


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_bytes()[-limit:].decode("utf-8", errors="replace")
    except OSError:
        return ""


# Proxy variables are the only mechanical lever available without a real OS
# sandbox: stripping them blocks proxy-routed traffic but not raw sockets, so
# ``network: deny`` is always reported as "advisory", never "enforced". A
# genuine block (macOS: ``sandbox-exec`` with a loopback-allow / network-deny
# profile) is a documented option, deliberately not wired up here — the
# session must never claim a guarantee this code does not provide (finding #7).
_PROXY_ENV_VARS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy",
})


def _apply_network_policy(env: dict[str, str], policy: str) -> str:
    """Mutate ``env`` for ``policy`` in place; return the truthful enforcement level.

    ``SHIPFACTORY_NETWORK_POLICY`` alone is advisory — a trusted script (or a
    dependency it shells out to) can simply ignore it. Returns
    ``"not_applicable"`` for ``allow`` (nothing to constrain) or
    ``"advisory"`` for ``deny`` (proxy env stripped, but raw sockets are
    unconstrained) — never ``"enforced"``, since no real sandbox is applied.
    """
    env["SHIPFACTORY_NETWORK_POLICY"] = policy
    if policy != "deny":
        return "not_applicable"
    for var in _PROXY_ENV_VARS:
        env.pop(var, None)
    return "advisory"


class _OutputCapWatcher:
    """A background poller tracking whether a child exceeded its output cap."""

    def __init__(self) -> None:
        self.exceeded = threading.Event()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=0.5)


def _watch_output_cap(
    log_path: Path, max_bytes: int, on_exceeded, *, poll_interval: float = 0.1,
) -> _OutputCapWatcher:
    """Poll ``log_path``'s size on a tight interval and kill immediately on breach.

    A full daemon tick can be seconds apart; checking the cap only once per
    tick lets a fast child fill disk in the gap between ticks (review
    finding #5). A dedicated poller closes that window down to
    ``poll_interval`` instead. stdout is still wired directly to a real log
    file rather than a pipe the daemon process holds open: a child's stdout
    must survive a daemon crash/restart on its own, exactly like every other
    spawn path in this module (an in-process pipe reader would starve the
    child of a writable stdout the moment the daemon that adopted it died,
    breaking orphan adoption across restarts).
    """
    watcher = _OutputCapWatcher()
    max_bytes = max(0, int(max_bytes))

    def _poll() -> None:
        while not watcher._stop.is_set():
            try:
                size = log_path.stat().st_size
            except OSError:
                size = 0
            if size > max_bytes:
                watcher.exceeded.set()
                on_exceeded()
                return
            watcher._stop.wait(poll_interval)

    watcher.thread = threading.Thread(target=_poll, daemon=True)
    watcher.thread.start()
    return watcher


def _stop_output_watch(record: dict[str, Any]) -> None:
    watch = record.get("output_watch")
    if watch is not None:
        watch.stop()


def _output_cap_exceeded(record: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Return whether ``record`` is over its output cap, right now.

    The watcher thread's ``exceeded`` flag is a fast path (kills a
    long-running child quickly), not the source of truth: a child that
    writes a huge burst and exits before the watcher's next poll tick would
    otherwise race past it undetected. A synchronous size check here is the
    authoritative backstop, independent of watcher thread timing.
    """
    watch = record.get("output_watch")
    if watch is not None and watch.exceeded.is_set():
        return True
    log_path = record.get("log_path")
    if log_path is None:
        return False
    try:
        return log_path.exists() and log_path.stat().st_size > int(cfg["max_output_bytes"])
    except OSError:
        return False


# Materializations (bootstrap [+ seed]) currently owned by this daemon
# process, keyed by env_session id. Never run synchronously inside a tick —
# spawned once here, then polled to completion across subsequent ticks.
_MATERIALIZING: dict[str, dict[str, Any]] = {}


def _spawn_phase(
    id: str, record: dict[str, Any], *, phase: str, argv: list[str], cfg: dict[str, Any],
) -> None:
    _stop_output_watch(record)  # a prior phase's watcher (e.g. bootstrap) must not race a new one
    resolved = [str(record["scripts"][argv[0]]), *argv[1:]]
    log_file = open(record["log_path"], "ab")
    env = dict(os.environ)
    # The daemon runs inside the canonical venv; hand scripts that exact
    # interpreter so they never gamble on the ambient PATH resolving a
    # `python` with the right dependencies (finding #89).
    env["SHIPFACTORY_PYTHON"] = sys.executable
    enforcement_level = _apply_network_policy(env, record["network_policy"])
    try:
        proc = subprocess.Popen(
            resolved, cwd=str(record["workspace"]), stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
    finally:
        log_file.close()
    pid = proc.pid
    # Persist the pid the instant Popen returns — before the (up to two
    # second) OS start-token observation below — so a daemon crash in that
    # window still leaves a durable pid recovery can verify/kill, instead of
    # leaking an untracked orphan (review finding #2).
    store.mark_env_session_pid(id, pid)
    store.update_env_session_network_enforcement(id, enforcement_level)
    record["proc"], record["pid"], record["phase"], record["token"] = proc, pid, phase, None

    def _on_output_cap_exceeded() -> None:
        store.mark_env_session_output_capped(id)
        _kill_group(pid, token=record.get("token"))

    record["output_watch"] = _watch_output_cap(
        record["log_path"], cfg["max_output_bytes"], _on_output_cap_exceeded,
    )
    token = _spawn._capture_start_token(pid, proc)
    store.mark_env_session_token(id, token)
    record["token"] = token


def _finish_env_session(id: str, state: str, error: str | None, record: dict[str, Any]) -> None:
    store.update_env_session_state(id, state, last_error=error)
    if record.get("lease_key"):
        store.release_resource_lease(record["lease_key"])


def request_materialization(
    *, repo_root: str | Path, workspace: str | Path, base_sha: str,
    candidate_sha: str | None = None, manifest_path: str | None = None,
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an existing/queued materialization row, or spawn a new one.

    Returns ``None`` only when the operator's ``max_sessions`` concurrency
    cap is exhausted — the caller should retry on a later tick rather than
    treat this as a failure (queue, not fail).
    """
    repo_root = Path(repo_root)
    workspace = Path(workspace)
    manifest = load_runtime_manifest(
        repo_root, base_sha, manifest_path or cfg["manifest_path"],
    )
    tracked_hash = compute_tracked_input_hash(
        workspace, manifest.document["bootstrap"]["tracked_inputs"],
    )
    blob_shas = control_blob_shas(repo_root, manifest)
    key = materialization_key(
        base_sha=base_sha, candidate_sha=candidate_sha, manifest_blob_sha=manifest.blob_sha,
        tracked_input_hash=tracked_hash, control_blob_shas=blob_shas,
    )
    existing = store.latest_env_session_for_key(key)
    if existing and existing["state"] in ("materializing", "ready"):
        return existing

    lease_key = f"materialization_slot:{uuid.uuid4().hex}"
    acquired = store.acquire_resource_lease(
        "materialization_slot", int(cfg["max_sessions"]), key=lease_key,
        lease_seconds=int(cfg["bootstrap_timeout_seconds"]) + 60,
        metadata={"base_sha": base_sha, "workspace": str(workspace)},
    )
    if acquired is None:
        return None

    changed_control_paths = candidate_control_plane_diff(
        repo_root, base_sha, candidate_sha, manifest.control_paths,
    )
    id = uuid.uuid4().hex
    log_path = _logs_root() / f"{id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    store.insert_env_session(
        id, key=key, base_sha=base_sha, candidate_sha=candidate_sha,
        manifest_path=manifest.manifest_path, manifest_blob_sha=manifest.blob_sha,
        tracked_input_hash=tracked_hash, workspace_path=str(workspace),
        control_plane_risk=bool(changed_control_paths),
        control_plane_paths=changed_control_paths, lease_key=lease_key,
        stdout_path=str(log_path), stderr_path=None,
    )
    scripts = materialize_pinned_scripts(repo_root, manifest, _scripts_root(), blob_shas=blob_shas)
    record: dict[str, Any] = {
        "log_path": log_path, "workspace": workspace, "scripts": scripts,
        "network_policy": manifest.document["bootstrap"]["network"],
        "seed_argv": manifest.document["seed"]["argv"],
        "lease_key": lease_key,
        "deadline": monotonic() + int(cfg["bootstrap_timeout_seconds"]),
    }
    try:
        _spawn_phase(
            id, record, phase="bootstrap", argv=manifest.document["bootstrap"]["argv"], cfg=cfg,
        )
    except Exception as exc:
        _finish_env_session(id, "failed", f"bootstrap spawn failed: {exc}", record)
        return store.env_session_row(id)
    _MATERIALIZING[id] = record
    return store.env_session_row(id)


def restore_materializations() -> None:
    """Fail-closed reconciliation of materializations from a prior daemon life.

    A daemon restart loses in-memory phase/manifest context, so an adopted
    row is never optimistically resumed or marked ready — it is killed and
    failed, forcing a fresh rebuild on next demand. This is the same
    fail-closed posture as artifact sealing: safety over salvaging partial
    progress.

    A pid is now persisted the instant ``Popen`` returns (review finding
    #2), so any nonterminal row with a pid may have a real, still-running
    child — even one whose start token never made it to the database before
    the crash. We attempt a verified kill (falling back to a liveness probe
    when no token is on record) whenever a pid is present, rather than only
    when a token happens to match; a materialization row is never declared
    failed while it might still be leaking a live orphan.
    """
    for row in store.nonterminal_env_sessions():
        id = row["id"]
        if id in _MATERIALIZING:
            continue
        pid = row.get("pid")
        token = row.get("process_start_token")
        if pid:
            _spawn.verified_killpg(int(pid), token)
        _finish_env_session(
            id, "failed", "daemon restarted during materialization; rebuild required", row,
        )


def cancel_materialization(id: str, *, reason: str = "cancelled") -> bool:
    """Cancel a materialization mid-bootstrap or mid-seed, killing its child."""
    record = _MATERIALIZING.pop(id, None)
    if record is None:
        return False
    _kill_group(record.get("pid"), token=record.get("token"))
    _stop_output_watch(record)
    _finish_env_session(id, "failed", reason, record)
    return True


def reap_materializations(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Poll every locally-owned materialization; advance, timeout, or finish it."""
    finished: list[dict[str, Any]] = []
    for id, record in list(_MATERIALIZING.items()):
        proc = record["proc"]
        code = proc.poll()
        # A dedicated watcher thread enforces the output cap on a tight poll
        # interval, not just once per tick (review finding #5) — it already
        # killed the child the instant it hit the cap. The exit-time check
        # here is a synchronous authoritative backstop, independent of
        # watcher-thread timing (a fast child can exit before the watcher's
        # next scheduled poll).
        over_output = _output_cap_exceeded(record, cfg)
        if over_output:
            store.mark_env_session_output_capped(id)
        if code is None:
            over_budget = monotonic() >= record["deadline"]
            if over_budget or over_output:
                _kill_group(record.get("pid"), token=record.get("token"))
                reason = "bootstrap_timeout" if over_budget else "max_output_bytes exceeded"
                _finish_env_session(id, "failed", reason, record)
                finished.append({"id": id, "state": "failed", "reason": reason})
                del _MATERIALIZING[id]
            continue
        # Exited: kill any orphaned grandchildren left behind in the group
        # before evaluating the outcome (§2.1.7 "forks a child and exits").
        _kill_group(record.get("pid"), token=record.get("token"))
        _stop_output_watch(record)
        if over_output:
            _finish_env_session(id, "failed", "max_output_bytes exceeded", record)
            finished.append({"id": id, "state": "failed"})
            del _MATERIALIZING[id]
            continue
        if code != 0:
            _finish_env_session(
                id, "failed",
                f"{record['phase']} exited {code}: {_log_tail(record['log_path'])}", record,
            )
            finished.append({"id": id, "state": "failed"})
            del _MATERIALIZING[id]
            continue
        if record["phase"] == "bootstrap" and record.get("seed_argv"):
            if monotonic() >= record["deadline"]:
                _finish_env_session(id, "failed", "bootstrap_timeout before seed", record)
                finished.append({"id": id, "state": "failed"})
                del _MATERIALIZING[id]
                continue
            try:
                _spawn_phase(id, record, phase="seed", argv=record["seed_argv"], cfg=cfg)
            except Exception as exc:
                _finish_env_session(id, "failed", f"seed spawn failed: {exc}", record)
                finished.append({"id": id, "state": "failed"})
                del _MATERIALIZING[id]
            continue
        _finish_env_session(id, "ready", None, record)
        finished.append({"id": id, "state": "ready"})
        del _MATERIALIZING[id]
    return finished


# ---------------------------------------------------------------------------
# App sessions: app-up as a supervised child bound to a leased port.
# ---------------------------------------------------------------------------

# Locally-owned live app processes, keyed by app_session id. A row with no
# entry here and ``pid IS NULL`` is still queued for a port; a row with a
# pid but no entry here (after a restart) is reconciled by
# ``restore_app_sessions``.
_APP_RUNNING: dict[str, dict[str, Any]] = {}


def _substitute_port(argv: list[str], port: int) -> list[str]:
    return [item.replace("${PORT}", str(port)) for item in argv]


def _monotonic_deadline_from_iso(started_iso: str | None, total_seconds: int) -> float:
    """Convert a persisted wall-clock start time into a fresh monotonic deadline."""
    elapsed = 0.0
    if started_iso:
        try:
            started = datetime.fromisoformat(str(started_iso).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        except (TypeError, ValueError):
            elapsed = 0.0
    return monotonic() + max(0.0, float(total_seconds) - elapsed)


def _poll_health(port: int, path: str, expected_status: int, *, timeout: float = 2.0) -> bool:
    """Probe the app's declared healthcheck on the exact port we leased it.

    Bounded per-call timeout keeps one tick from blocking on a hung app. If
    the app bound a different port than allocated, this simply never
    succeeds against the leased port, which correctly times out the session
    rather than trusting the app's own claim.
    """
    url = f"http://127.0.0.1:{int(port)}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status) == int(expected_status)
    except urllib.error.HTTPError as exc:
        return int(exc.code) == int(expected_status)
    except Exception:
        return False


def _finish_app_session(aid: str, state: str, error: str | None, record: dict[str, Any]) -> None:
    store.update_app_session_state(aid, state, last_error=error)
    if record.get("port_lease_key"):
        store.release_resource_lease(record["port_lease_key"])


def request_app_start(
    *, env_session_id: str, request_key: str, expected_instance_id: str,
    expected_head_sha: str, cfg: dict[str, Any],
) -> dict[str, Any]:
    """Idempotently request an app-up session on a materialized environment.

    A retry with the same ``request_key`` returns the existing row rather
    than double-spawning. Port binding is attempted immediately but is not
    required to succeed here — an exhausted port range leaves the row
    queued (``state='starting'``, no pid) for the next ``tick`` to retry.
    """
    if not isinstance(expected_instance_id, str) or not expected_instance_id:
        raise EnvironmentError("app session requires an expected instance identity")
    if not isinstance(expected_head_sha, str) or not expected_head_sha:
        raise EnvironmentError("app session requires an expected candidate head identity")
    env_row = store.env_session_row(env_session_id)
    if env_row is None or env_row["state"] != "ready":
        raise EnvironmentError(f"env_session {env_session_id} is not ready")
    id = uuid.uuid4().hex
    log_path = _logs_root() / f"{id}-app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    row = store.insert_app_session(
        id, env_session_id=env_session_id, request_key=request_key,
        workspace_path=env_row["workspace_path"],
        expected_instance_id=expected_instance_id, expected_head_sha=expected_head_sha,
        stdout_path=str(log_path), stderr_path=None,
    )
    if row["state"] == "starting" and not row.get("pid") and row["id"] not in _APP_RUNNING:
        _try_bind_and_spawn(row["id"], cfg)
        row = store.app_session_row(row["id"]) or row
    return row


def _try_bind_and_spawn(aid: str, cfg: dict[str, Any]) -> bool:
    """Attempt to lease a port and spawn the app child; ``False`` means queued."""
    row = store.app_session_row(aid)
    if row is None or row["state"] != "starting" or row.get("pid"):
        return False
    if not row.get("expected_instance_id") or not row.get("expected_head_sha"):
        store.update_app_session_state(
            aid, "crashed", last_error="app request has no durable candidate identity",
        )
        return False
    env_row = store.env_session_row(row["env_session_id"])
    if env_row is None:
        store.update_app_session_state(aid, "crashed", last_error="materialization missing")
        return False
    workspace = Path(row["workspace_path"])
    try:
        manifest = load_runtime_manifest(workspace, env_row["base_sha"], env_row["manifest_path"])
        scripts = materialize_pinned_scripts(workspace, manifest, _scripts_root())
    except EnvironmentError as exc:
        store.update_app_session_state(aid, "crashed", last_error=f"manifest unavailable: {exc}")
        return False
    port_lease_key = row.get("port_lease_key") or f"port:app:{aid}"
    port_lease_seconds = (
        int(cfg["startup_timeout_seconds"]) + int(cfg["shutdown_timeout_seconds"]) + 300
    )
    port = store.acquire_port_lease(
        int(cfg["port_min"]), int(cfg["port_max"]), key=port_lease_key,
        lease_seconds=port_lease_seconds,
        metadata={"app_session_id": aid},
    )
    if port is None:
        return False  # port range exhausted: queue, do not fail (§2.1.7)
    app_config = manifest.document["app"]
    app_url = f"http://127.0.0.1:{port}{app_config['healthcheck']['path']}"
    store.mark_app_session_bound(aid, port=port, port_lease_key=port_lease_key, app_url=app_url)
    argv = _substitute_port(app_config["start_argv"], port)
    resolved = [str(scripts[app_config["start_argv"][0]]), *argv[1:]]
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["SHIPFACTORY_PYTHON"] = sys.executable  # finding #89: no PATH gamble
    # These reserved values are durable request fields supplied only by the
    # Factory parent. They are never inferred from the attacker-visible
    # request key or inherited from ambient operator state.
    env["SHIPFACTORY_INSTANCE_ID"] = str(row["expected_instance_id"])
    env["SHIPFACTORY_HEAD_SHA"] = str(row["expected_head_sha"])
    enforcement_level = _apply_network_policy(env, manifest.document["bootstrap"]["network"])
    log_path = Path(row["stdout_path"])
    log_file = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            # Default app binding is 127.0.0.1, not all interfaces (§2.1.5);
            # enforcement is the app-start script's contract, not ours to
            # rewrite — we only ever health-check the loopback leased port.
            resolved, cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=log_file,
            stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
    except Exception as exc:
        log_file.close()
        store.release_resource_lease(port_lease_key)
        store.update_app_session_state(aid, "crashed", last_error=f"app spawn failed: {exc}")
        return False
    log_file.close()
    pid = proc.pid
    # Persist the pid the instant Popen returns, before the (up to two
    # second) start-token observation below (review finding #2).
    store.mark_app_session_pid(aid, pid)
    store.update_app_session_network_enforcement(aid, enforcement_level)
    record: dict[str, Any] = {
        "proc": proc, "pid": pid, "token": None, "phase": "starting", "port": port,
        "port_lease_key": port_lease_key, "port_lease_seconds": port_lease_seconds,
        "log_path": log_path,
        "health_path": app_config["healthcheck"]["path"],
        "expected_status": app_config["healthcheck"]["expected_status"],
        "stop_signal": app_config["stop_signal"],
        "deadline": monotonic() + int(cfg["startup_timeout_seconds"]),
    }

    def _on_output_cap_exceeded() -> None:
        store.mark_app_session_output_capped(aid)
        _kill_group(pid, token=record.get("token"))

    record["output_watch"] = _watch_output_cap(
        log_path, cfg["max_output_bytes"], _on_output_cap_exceeded,
    )
    token = _spawn._capture_start_token(pid, proc)
    store.mark_app_session_token(aid, token)
    record["token"] = token
    _APP_RUNNING[aid] = record
    return True


def _advance_one_app(
    aid: str, record: dict[str, Any], cfg: dict[str, Any], *, health_result: bool | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    proc = record["proc"]
    code = proc.poll()
    # See _output_cap_exceeded: the watcher thread is a fast path, this call
    # is the authoritative synchronous backstop (finding #5).
    over_output = _output_cap_exceeded(record, cfg)
    if over_output:
        store.mark_app_session_output_capped(aid)
    if code is not None or over_output:
        _kill_group(record.get("pid"), token=record.get("token"))
        _stop_output_watch(record)
        if over_output:
            _finish_app_session(aid, "crashed", "max_output_bytes exceeded", record)
            events.append({"id": aid, "event": "crashed"})
        elif record["phase"] == "stopping":
            _finish_app_session(aid, "stopped", None, record)
            events.append({"id": aid, "event": "stopped"})
        else:
            _finish_app_session(
                aid, "crashed", f"app exited {code}: {_log_tail(record['log_path'])}", record,
            )
            events.append({"id": aid, "event": "crashed"})
        del _APP_RUNNING[aid]
        return events
    if record["phase"] in ("starting", "healthy") and record.get("port_lease_key"):
        # The process is alive and (for "starting") the most recent probe —
        # taken concurrently by tick(), never serially here (finding #6) — is
        # in health_result. A live/healthy session's port lease must never be
        # left to expire on a wall-clock timer while it keeps answering;
        # renew it on every successful poll, and let expiry reap only a
        # session whose liveness check actually failed (finding #4).
        store.renew_resource_lease(
            record["port_lease_key"], lease_seconds=record["port_lease_seconds"],
        )
    if record["phase"] == "starting":
        if health_result:
            store.update_app_session_state(aid, "healthy", health_status="ok")
            record["phase"] = "healthy"
            events.append({"id": aid, "event": "healthy"})
        elif monotonic() >= record["deadline"]:
            _kill_group(record.get("pid"), token=record.get("token"))
            _stop_output_watch(record)
            _finish_app_session(
                aid, "crashed",
                "healthcheck never became healthy before startup_timeout_seconds", record,
            )
            events.append({"id": aid, "event": "crashed"})
            del _APP_RUNNING[aid]
        return events
    if record["phase"] == "stopping" and monotonic() >= record["deadline"] and not record.get("kill_sent"):
        _kill_group(record.get("pid"), signal.SIGKILL, token=record.get("token"))
        record["kill_sent"] = True
    return events


def tick(cfg: dict[str, Any]) -> dict[str, Any]:
    """Advance every locally-owned or newly-adopted app session by one step.

    Every "starting" session's healthcheck is probed concurrently through a
    bounded thread pool rather than one blocking ``urlopen`` call at a time
    on this thread: with several apps starting together, a serial probe
    blocks the whole daemon tick for up to ``healthcheck_timeout_seconds``
    per session (review finding #6). The timeout itself is validated
    operator config, not a hardcoded constant.
    """
    restore_app_sessions(cfg)
    events: list[dict[str, Any]] = []
    rows = store.nonterminal_app_sessions()
    probe_timeout = float(cfg.get("healthcheck_timeout_seconds", 2))
    probe_concurrency = max(1, int(cfg.get("healthcheck_probe_concurrency", 8)))
    pending: dict[str, Any] = {}
    pool = ThreadPoolExecutor(max_workers=probe_concurrency, thread_name_prefix="sf-probe")
    try:
        for row in rows:
            aid = row["id"]
            if row["state"] == "starting" and not row.get("pid") and aid not in _APP_RUNNING:
                if _try_bind_and_spawn(aid, cfg):
                    events.append({"id": aid, "event": "spawned"})
                continue
            record = _APP_RUNNING.get(aid)
            if record is None or record["phase"] != "starting" or record["proc"].poll() is not None:
                continue
            pending[aid] = pool.submit(
                _poll_health, record["port"], record["health_path"], record["expected_status"],
                timeout=probe_timeout,
            )
        health_results: dict[str, bool] = {}
        for aid, future in pending.items():
            try:
                health_results[aid] = bool(future.result(timeout=probe_timeout + 1.0))
            except Exception:
                health_results[aid] = False
    finally:
        pool.shutdown(wait=False)
    for row in rows:
        aid = row["id"]
        record = _APP_RUNNING.get(aid)
        if record is None:
            continue
        events.extend(_advance_one_app(aid, record, cfg, health_result=health_results.get(aid)))
    return {"events": events}


def request_stop(app_session_id: str, cfg: dict[str, Any]) -> bool:
    """Begin graceful shutdown: ``stop_signal`` now, escalate to KILL later."""
    row = store.app_session_row(app_session_id)
    if row is None or row["state"] not in ("starting", "healthy"):
        return False
    store.update_app_session_state(app_session_id, "stopping")
    record = _APP_RUNNING.get(app_session_id)
    if record is None:
        if row.get("port_lease_key"):
            store.release_resource_lease(row["port_lease_key"])
        store.update_app_session_state(app_session_id, "stopped")
        return True
    record["phase"] = "stopping"
    record["kill_sent"] = False
    record["deadline"] = monotonic() + int(cfg["shutdown_timeout_seconds"])
    try:
        sig = getattr(signal, f"SIG{record['stop_signal']}")
    except AttributeError:
        sig = signal.SIGTERM
    _kill_group(record.get("pid"), sig, token=record.get("token"))
    return True


def restore_app_sessions(cfg: dict[str, Any]) -> None:
    """Adopt-or-crash live app sessions across a daemon restart.

    A pid/start-token match is adopted with its healthcheck contract
    re-derived from the pinned manifest (never trusted from a stale DB
    snapshot). A daemon crash can also land between ``Popen`` returning and
    the OS start token being durably recorded (up to two seconds); the pid
    is already persisted by then (review finding #2), but the token may
    still be null. Rather than skip that pid (leaving a live orphan holding
    its port forever) or trust it blindly, every row with a pid gets a
    verified-kill attempt — token match, or a liveness probe when no token
    is on record — before its port lease is released; a stale PID is never
    signalled on a guess, but a live one is never left running untracked
    either (§2.1.7, review finding #2/#3).
    """
    for row in store.nonterminal_app_sessions():
        aid = row["id"]
        if aid in _APP_RUNNING:
            continue
        pid = row.get("pid")
        token = row.get("process_start_token")
        if not pid:
            continue
        if not (token and _spawn._process_start_token(int(pid)) == token):
            _spawn.verified_killpg(int(pid), token)
            store.update_app_session_state(
                aid, "crashed", last_error="pid reused or dead across daemon restart",
            )
            if row.get("port_lease_key"):
                store.release_resource_lease(row["port_lease_key"])
            continue
        env_row = store.env_session_row(row["env_session_id"])
        manifest = None
        if env_row is not None:
            try:
                manifest = load_runtime_manifest(
                    Path(row["workspace_path"]), env_row["base_sha"], env_row["manifest_path"],
                )
            except EnvironmentError:
                manifest = None
        app_config = manifest.document["app"] if manifest is not None else {
            "healthcheck": {"path": "/", "expected_status": 200}, "stop_signal": "TERM",
        }
        phase = "stopping" if row["state"] == "stopping" else (
            "healthy" if row["state"] == "healthy" else "starting"
        )
        deadline = (
            _monotonic_deadline_from_iso(row.get("stopping_at"), int(cfg["shutdown_timeout_seconds"]))
            if phase == "stopping" else
            _monotonic_deadline_from_iso(row.get("started_at"), int(cfg["startup_timeout_seconds"]))
        )
        log_path = Path(row["stdout_path"]) if row.get("stdout_path") else None
        record: dict[str, Any] = {
            "proc": _spawn._AdoptedProcess(int(pid), token), "pid": int(pid), "token": token,
            "phase": phase, "port": row.get("port"), "port_lease_key": row.get("port_lease_key"),
            "port_lease_seconds": (
                int(cfg["startup_timeout_seconds"]) + int(cfg["shutdown_timeout_seconds"]) + 300
            ),
            "log_path": log_path,
            "health_path": app_config["healthcheck"]["path"],
            "expected_status": app_config["healthcheck"]["expected_status"],
            "stop_signal": app_config["stop_signal"],
            "deadline": deadline, "kill_sent": False,
        }
        if log_path is not None:
            def _on_output_cap_exceeded(aid=aid, record=record) -> None:
                store.mark_app_session_output_capped(aid)
                _kill_group(record.get("pid"), token=record.get("token"))

            record["output_watch"] = _watch_output_cap(
                log_path, cfg["max_output_bytes"], _on_output_cap_exceeded,
            )
        _APP_RUNNING[aid] = record


__all__ = [
    "RUNTIME_SCHEMA", "DEFAULT_MANIFEST_PATH", "RuntimeManifest",
    "EnvironmentError", "ManifestError", "PathSafetyError",
    "validate_runtime_manifest", "load_runtime_manifest", "control_plane_paths",
    "candidate_control_plane_diff", "materialize_pinned_scripts",
    "compute_tracked_input_hash", "materialization_key",
    "request_materialization", "restore_materializations",
    "cancel_materialization", "reap_materializations",
    "request_app_start", "request_stop", "restore_app_sessions", "tick",
]
