"""Deterministic, non-model verification and sealed evidence bundles (SF-9)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Callable

import yaml

from shipfactory import store
from shipfactory.artifacts import _copy_once


VERIFICATION_SCHEMA = "shipfactory.verification/v1"
EVIDENCE_SCHEMA = "shipfactory.evidence/v1"
DEFAULT_MANIFEST_PATH = ".shipfactory/verification.yaml"

_TOP = {"schema", "cases", "capture"}
_CAPTURE = {"video", "trace", "screenshots"}
_COMMAND_CASE = {"id", "requirement_ids", "driver", "argv", "oracle"}
_PLAYWRIGHT_CASE = {"id", "requirement_ids", "driver", "script", "assertions"}
_CASE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_BLOB_MODES = {"100644", "100755"}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{12,}\b"),
)


class VerificationError(RuntimeError):
    """Base class for fail-closed verification errors."""


class VerificationManifestError(VerificationError):
    """The pinned repository verification manifest is invalid or was tampered with."""


class EvidenceInvariantError(VerificationError):
    """Persisted evidence does not match its sealed bundle."""


class CommitBindingError(VerificationError):
    """The workspace no longer represents the revision selected for verification."""


@dataclass(frozen=True)
class VerificationManifest:
    document: dict[str, Any]
    blob_sha: str
    base_sha: str
    relpath: str
    raw: bytes


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _safe_relpath(value: str, label: str) -> str:
    parsed = PurePosixPath(value)
    if (not isinstance(value, str) or not value or parsed.is_absolute()
            or "\\" in value or ".." in parsed.parts):
        raise VerificationManifestError(f"{label} must be a safe repo-relative path")
    return value


def _git(repo: Path, *args: str, binary: bool = False) -> Any:
    try:
        result = subprocess.check_output(
            ["git", *args], cwd=repo, stderr=subprocess.PIPE, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationManifestError(f"git {' '.join(args)} failed: {exc}") from exc
    return result if binary else result.decode("utf-8").strip()


def _blob_at(repo: Path, ref: str, relpath: str) -> tuple[str, bytes]:
    listing = _git(repo, "ls-tree", ref, "--", relpath)
    if not listing:
        raise VerificationManifestError(f"{relpath!r} is absent from trusted base {ref}")
    header, _, _name = listing.partition("\t")
    try:
        mode, kind, blob_sha = header.split()
    except ValueError as exc:
        raise VerificationManifestError(f"invalid git tree entry for {relpath!r}") from exc
    if kind != "blob" or mode not in _BLOB_MODES:
        raise VerificationManifestError(f"{relpath!r} is not a safe regular tracked file")
    return blob_sha, _git(repo, "cat-file", "-p", blob_sha, binary=True)


def _exact(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise VerificationManifestError(f"{label} keys must be exactly {sorted(expected)}")


def _validate_oracle(oracle: Any, label: str) -> None:
    if not isinstance(oracle, dict) or not isinstance(oracle.get("type"), str):
        raise VerificationManifestError(f"{label} oracle must be a mapping with type")
    kind = oracle["type"]
    if kind == "exit_code":
        if (set(oracle) != {"type", "equals"} or not isinstance(oracle["equals"], int)
                or isinstance(oracle["equals"], bool)):
            raise VerificationManifestError(f"{label} exit_code oracle is invalid")
    elif kind == "output_contains":
        if set(oracle) not in ({"type", "contains"}, {"type", "contains", "stream"}):
            raise VerificationManifestError(f"{label} output_contains oracle is invalid")
        if not isinstance(oracle["contains"], str) or not oracle["contains"]:
            raise VerificationManifestError(f"{label} output_contains value is invalid")
        if oracle.get("stream", "combined") not in {"stdout", "stderr", "combined"}:
            raise VerificationManifestError(f"{label} output_contains stream is invalid")
    else:
        raise VerificationManifestError(f"unknown oracle type {kind!r}")


def validate_verification_manifest(
    document: Any, *, required_requirement_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Strictly validate ``shipfactory.verification/v1`` and requirement coverage."""
    _exact(document, _TOP, "verification manifest")
    if document["schema"] != VERIFICATION_SCHEMA:
        raise VerificationManifestError(
            f"unsupported verification schema {document['schema']!r}"
        )
    cases = document["cases"]
    if not isinstance(cases, list) or not cases:
        raise VerificationManifestError("verification cases must be a non-empty list")
    seen: set[str] = set()
    covered: set[str] = set()
    for index, case in enumerate(cases):
        label = f"case {index}"
        if not isinstance(case, dict):
            raise VerificationManifestError(f"{label} must be a mapping")
        driver = case.get("driver")
        if driver == "command":
            _exact(case, _COMMAND_CASE, label)
            argv = case["argv"]
            if not isinstance(argv, list) or not argv or not all(
                isinstance(item, str) and item for item in argv
            ):
                raise VerificationManifestError(f"{label} argv must be an argv array")
            executable = PurePosixPath(argv[0]).name.lower()
            if ((executable in {"sh", "bash", "zsh", "dash", "ksh", "fish"}
                 and any(item in {"-c", "-lc"} for item in argv[1:]))
                    or (executable in {"cmd", "cmd.exe"} and any(
                        item.lower() == "/c" for item in argv[1:]
                    ))
                    or (executable in {"powershell", "powershell.exe", "pwsh"} and any(
                        item.lower() in {"-command", "-c"} for item in argv[1:]
                    ))):
                raise VerificationManifestError(f"{label} shell interpolation is forbidden")
            _validate_oracle(case["oracle"], label)
        elif driver == "playwright":
            _exact(case, _PLAYWRIGHT_CASE, label)
            _safe_relpath(case["script"], f"{label} script")
            assertions = case["assertions"]
            if not isinstance(assertions, list) or not assertions:
                raise VerificationManifestError(f"{label} assertions must be non-empty")
            for assertion in assertions:
                if not isinstance(assertion, dict) or not isinstance(assertion.get("type"), str):
                    raise VerificationManifestError(f"{label} assertion is invalid")
                if assertion["type"] == "visible":
                    _exact(assertion, {"type", "selector"}, f"{label} visible assertion")
                    if not isinstance(assertion["selector"], str) or not assertion["selector"]:
                        raise VerificationManifestError(f"{label} selector is invalid")
                elif assertion["type"] == "api-status":
                    _exact(assertion, {"type", "request", "status"}, f"{label} api-status assertion")
                    if (not isinstance(assertion["request"], str)
                            or not isinstance(assertion["status"], int)
                            or isinstance(assertion["status"], bool)):
                        raise VerificationManifestError(f"{label} api-status assertion is invalid")
                else:
                    raise VerificationManifestError(
                        f"unknown playwright assertion type {assertion['type']!r}"
                    )
        else:
            raise VerificationManifestError(f"unknown verification driver {driver!r}")
        ident = case.get("id")
        if not isinstance(ident, str) or not _CASE_ID.fullmatch(ident) or ident in seen:
            raise VerificationManifestError("case ids must be unique lowercase identifiers")
        seen.add(ident)
        requirements = case.get("requirement_ids")
        if not isinstance(requirements, list) or not requirements or not all(
            isinstance(item, str) and item for item in requirements
        ):
            raise VerificationManifestError(f"{label} must map to requirement_ids")
        covered.update(requirements)
    _exact(document["capture"], _CAPTURE, "capture")
    capture = document["capture"]
    if (not isinstance(capture["video"], bool) or not isinstance(capture["trace"], bool)
            or capture["screenshots"] not in {"always", "on-failure", "never"}):
        raise VerificationManifestError("invalid capture policy")
    missing = set(required_requirement_ids or ()) - covered
    if missing:
        raise VerificationManifestError(
            "required requirements are uncovered: " + ", ".join(sorted(missing))
        )
    return document


def load_verification_manifest(
    repo_root: str | Path, base_sha: str,
    relpath: str = DEFAULT_MANIFEST_PATH, *,
    required_requirement_ids: set[str] | None = None,
    expected_blob_sha: str | None = None,
    verify_worktree_copy: bool = True,
) -> VerificationManifest:
    """Load manifest bytes exclusively from the trusted base commit and pin them."""
    repo = Path(repo_root)
    relpath = _safe_relpath(relpath, "manifest path")
    blob_sha, raw = _blob_at(repo, base_sha, relpath)
    if expected_blob_sha is not None and blob_sha != expected_blob_sha:
        raise VerificationManifestError("verification manifest blob SHA mismatch")
    if verify_worktree_copy:
        try:
            candidate = (repo / relpath).read_bytes()
        except OSError as exc:
            raise VerificationManifestError("verification manifest worktree copy is unavailable") from exc
        if candidate != raw:
            raise VerificationManifestError("verification manifest bytes differ from pinned blob SHA")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise VerificationManifestError(f"verification manifest is not valid YAML: {exc}") from exc
    validate_verification_manifest(
        document, required_requirement_ids=required_requirement_ids,
    )
    return VerificationManifest(document, blob_sha, base_sha, relpath, raw)


def control_plane_paths(manifest: VerificationManifest) -> frozenset[str]:
    """Paths whose candidate modification is a verification control-plane risk."""
    scripts = {
        case["script"] for case in manifest.document["cases"]
        if case["driver"] == "playwright"
    }
    return frozenset({manifest.relpath, *scripts})


def _repository_identity(workspace: Path) -> tuple[str, str, str]:
    status = _git(workspace, "status", "--porcelain")
    if status:
        raise CommitBindingError("workspace is not clean")
    head = _git(workspace, "rev-parse", "HEAD").lower()
    tree = _git(workspace, "write-tree").lower()
    if not _HASH.fullmatch(head) or not _HASH.fullmatch(tree):
        raise CommitBindingError("workspace Git identity is invalid")
    return status, head, tree


def assert_commit_binding(workspace: str | Path, head_sha: str, tree_sha: str) -> None:
    """Apply the normative clean/HEAD/tree checks at a verification boundary."""
    _status, actual_head, actual_tree = _repository_identity(Path(workspace))
    if actual_head != str(head_sha).lower():
        raise CommitBindingError(
            f"head_sha mismatch: expected {head_sha}, observed {actual_head}"
        )
    if actual_tree != str(tree_sha).lower():
        raise CommitBindingError(
            f"tree_sha mismatch: expected {tree_sha}, observed {actual_tree}"
        )


def _bundle_id(instance_id: str, step_id: str, activation: int) -> str:
    return hashlib.sha256(f"{instance_id}|{step_id}|{int(activation)}|evidence".encode()).hexdigest()


def _evidence_root(instance_id: str, step_id: str, activation: int) -> Path:
    for value in (instance_id, step_id, str(int(activation))):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise EvidenceInvariantError("unsafe evidence identity path segment")
    return (
        store._db_path().parent / "runs" / instance_id / step_id
        / str(int(activation)) / "evidence"
    )


def _redact(data: bytes, secret_values: tuple[str, ...] = ()) -> tuple[bytes, bool]:
    text = data.decode("utf-8", errors="replace")
    original = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    for value in secret_values:
        if value and len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    return text.encode("utf-8"), text != original


def _environment_digest(env: dict[str, str]) -> str:
    allowed = {
        key: value for key, value in env.items()
        if key.startswith("SHIPFACTORY_") or key in {"PATH", "LANG", "LC_ALL", "PORT"}
    }
    return hashlib.sha256(_canonical(allowed)).hexdigest()


def _output_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8", errors="replace")


def _kill_child(proc: subprocess.Popen[bytes], token: str | None) -> None:
    from shipfactory import spawn
    spawn.verified_killpg(proc.pid, token, signal.SIGKILL)


Driver = Callable[[dict[str, Any], Path, dict[str, str], int], dict[str, Any]]


def _command_driver(
    case: dict[str, Any], workspace: Path, env: dict[str, str], timeout: int,
) -> dict[str, Any]:
    started = store._now()
    started_monotonic = monotonic()
    run_id = store.record_run_start(
        env.get("SHIPFACTORY_CASE_RUN_KEY", "verification/unknown"),
        "verification", "verification", "", workspace_path=workspace,
        provider="shipfactory", resolved_model="non-model",
        executor_version=VERIFICATION_SCHEMA,
    )
    try:
        proc = subprocess.Popen(
            case["argv"], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        store.record_run_end(
            run_id, -1, None, None, monotonic() - started_monotonic,
            "infrastructure_error",
        )
        return {
            "classification": "infrastructure_error", "error": str(exc),
            "stdout": b"", "stderr": b"", "exit_code": None,
            "started_at": started, "ended_at": store._now(), "run_id": run_id,
        }
    from shipfactory import spawn
    process_start_token = spawn._capture_start_token(proc.pid, proc)
    store.record_run_spawned(run_id, proc.pid, process_start_token)
    try:
        stdout, stderr = proc.communicate(timeout=max(1, int(timeout)))
    except subprocess.TimeoutExpired:
        _kill_child(proc, process_start_token)
        stdout, stderr = proc.communicate()
        store.record_run_end(
            run_id, proc.returncode, None, None, monotonic() - started_monotonic, "timeout",
        )
        return {
            "classification": "timeout", "stdout": stdout, "stderr": stderr,
            "exit_code": proc.returncode, "started_at": started, "ended_at": store._now(),
            "run_id": run_id, "process_start_token": process_start_token,
        }
    oracle = case["oracle"]
    if oracle["type"] == "exit_code":
        passed = proc.returncode == int(oracle["equals"])
    else:
        stream = oracle.get("stream", "combined")
        selected = stdout if stream == "stdout" else stderr if stream == "stderr" else stdout + stderr
        passed = oracle["contains"].encode("utf-8") in selected
    classification = "passed" if passed else "failed"
    store.record_run_end(
        run_id, proc.returncode, None, None, monotonic() - started_monotonic, classification,
    )
    return {
        "classification": classification,
        "stdout": stdout, "stderr": stderr, "exit_code": proc.returncode,
        "started_at": started, "ended_at": store._now(),
        "run_id": run_id, "process_start_token": process_start_token,
    }


def _playwright_unavailable(
    case: dict[str, Any], workspace: Path, env: dict[str, str], timeout: int,
) -> dict[str, Any]:
    now = store._now()
    return {
        "classification": "infrastructure_error",
        "error": "playwright driver is not installed in this action runner",
        "stdout": b"", "stderr": b"", "exit_code": None,
        "started_at": now, "ended_at": now,
        "assertion_types": [item["type"] for item in case["assertions"]],
    }


DRIVERS: dict[str, Driver] = {
    "command": _command_driver,
    "playwright": _playwright_unavailable,
}


def restore_runs() -> list[int]:
    """Fence orphaned verification children by exact A1 process identity."""
    from shipfactory import spawn

    crashed: list[int] = []
    for row in store.nonterminal_verification_runs():
        pid = row.get("pid")
        token = row.get("process_start_token")
        if pid:
            spawn.verified_killpg(int(pid), token, signal.SIGKILL)
        store.record_run_crashed(int(row["id"]), "daemon restarted during verification")
        crashed.append(int(row["id"]))
    return crashed


def _insert_bundle(
    *, bundle_id: str, instance_id: str, step_id: str, activation: int,
    input_revision_hash: str, base_sha: str, head_sha: str, tree_sha: str,
    environment_session_id: str | None, manifest: VerificationManifest,
) -> dict[str, Any]:
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO evidence_bundles"
            "(id,instance_id,step_id,activation,input_revision_hash,base_sha,head_sha,tree_sha,"
            "environment_session_id,manifest_relpath,manifest_blob_sha,state,redaction_state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'ready','not_required',?)",
            (bundle_id, instance_id, step_id, int(activation), input_revision_hash,
             base_sha, head_sha, tree_sha, environment_session_id, manifest.relpath,
             manifest.blob_sha, store._now()),
        )
        row = db.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
    return dict(row)


def _set_bundle_state(bundle_id: str, state: str, reason: str | None = None) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_bundles SET state=?,invalid_reason=? WHERE id=?",
            (state, reason, bundle_id),
        )


def _item_id(bundle_id: str, case_id: str, attempt: int, kind: str) -> str:
    return hashlib.sha256(
        f"{bundle_id}|{case_id}|{int(attempt)}|{kind}".encode()
    ).hexdigest()


def _persist_log_item(
    *, bundle_id: str, case_id: str, attempt: int, data: bytes, root: Path,
    command: list[str] | None, env_digest: str, exit_code: int | None,
    started_at: str, ended_at: str, metadata: dict[str, Any],
) -> dict[str, Any]:
    ident = _item_id(bundle_id, case_id, attempt, "log")
    path = root / "items" / f"{ident}.log"
    sealed = _copy_once(path, data)
    digest = hashlib.sha256(sealed).hexdigest()
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO evidence_items"
            "(id,bundle_id,case_id,kind,path,sha256,size_bytes,mime_type,producer,"
            "command_json,cwd_relpath,env_digest,exit_code,started_at,ended_at,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, bundle_id, case_id, "log", str(path), digest, len(sealed),
             "text/plain; charset=utf-8", "verification-runner",
             json.dumps(command) if command is not None else None, ".", env_digest,
             exit_code, started_at, ended_at, json.dumps(metadata, sort_keys=True)),
        )
    return {"id": ident, "sha256": digest, "size_bytes": len(sealed), "kind": "log"}


def _record_case(
    *, bundle_id: str, case_id: str, attempt: int, case: dict[str, Any],
    status: str, item_ids: list[str], started_at: str, ended_at: str,
) -> None:
    oracle = case.get("oracle") or {
        "type": "playwright_assertions", "assertions": case.get("assertions", []),
    }
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO verification_cases"
            "(bundle_id,case_id,attempt,requirement_ids_json,oracle_type,oracle_json,status,"
            "evidence_item_ids_json,started_at,ended_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bundle_id, case_id, int(attempt),
             json.dumps(case["requirement_ids"], sort_keys=True), oracle["type"],
             json.dumps(oracle, sort_keys=True), status,
             json.dumps(item_ids, sort_keys=True), started_at, ended_at),
        )


def _bundle_payload(bundle_id: str, *, phase_b_eligible: bool,
                    outcome_state: str, invalid_reason: str | None) -> dict[str, Any]:
    with store._connect() as db:
        bundle = dict(db.execute(
            "SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,),
        ).fetchone())
        items = [dict(row) for row in db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=? ORDER BY id", (bundle_id,),
        ).fetchall()]
        cases = [dict(row) for row in db.execute(
            "SELECT * FROM verification_cases WHERE bundle_id=? ORDER BY case_id,attempt",
            (bundle_id,),
        ).fetchall()]
    return {
        "schema": EVIDENCE_SCHEMA,
        "id": bundle_id,
        "input_revision_hash": bundle["input_revision_hash"],
        "base_sha": bundle["base_sha"], "head_sha": bundle["head_sha"],
        "tree_sha": bundle["tree_sha"],
        "manifest_relpath": bundle["manifest_relpath"],
        "manifest_blob_sha": bundle["manifest_blob_sha"],
        "environment_session_id": bundle["environment_session_id"],
        "redaction_state": bundle["redaction_state"],
        "phase_b_eligible": bool(phase_b_eligible),
        "outcome_state": outcome_state,
        "invalid_reason": invalid_reason,
        "cases": [{key: row[key] for key in (
            "case_id", "attempt", "requirement_ids_json", "oracle_type", "oracle_json",
            "status", "evidence_item_ids_json", "started_at", "ended_at",
        )} for row in cases],
        "items": [{key: row[key] for key in (
            "id", "case_id", "kind", "sha256", "size_bytes", "producer", "command_json",
            "cwd_relpath", "env_digest", "exit_code", "started_at", "ended_at", "metadata_json",
        )} for row in items],
    }


def _seal_bundle(bundle_id: str, *, final_state: str, reason: str | None,
                 phase_b_eligible: bool) -> dict[str, Any]:
    payload = _bundle_payload(
        bundle_id, phase_b_eligible=phase_b_eligible,
        outcome_state=final_state, invalid_reason=reason,
    )
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    payload["bundle_sha256"] = digest
    with store._connect() as db:
        row = db.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
        root = _evidence_root(row["instance_id"], row["step_id"], int(row["activation"]))
    _copy_once(root / "bundle.json", _canonical(payload) + b"\n")
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_bundles SET state=?,bundle_sha256=?,sealed_at=?,invalid_reason=? "
            "WHERE id=?",
            (final_state, digest, store._now(), reason, bundle_id),
        )
        row = db.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
    return dict(row)


def run_verification(
    *, instance_id: str, step_id: str, activation: int, input_revision_hash: str,
    base_sha: str, head_sha: str, tree_sha: str, workspace: str | Path,
    manifest: VerificationManifest, profile: dict[str, Any],
    environment_session_id: str | None = None,
    environment_identity: dict[str, Any] | None = None,
    protected_manifest: VerificationManifest | None = None,
    drivers: dict[str, Driver] | None = None,
    child_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute candidate and protected cases, redact, and atomically seal evidence."""
    workspace = Path(workspace)
    bundle_id = _bundle_id(instance_id, step_id, activation)
    bundle = _insert_bundle(
        bundle_id=bundle_id, instance_id=instance_id, step_id=step_id,
        activation=activation, input_revision_hash=input_revision_hash,
        base_sha=base_sha, head_sha=head_sha, tree_sha=tree_sha,
        environment_session_id=environment_session_id, manifest=manifest,
    )
    if environment_session_id and bundle.get("environment_session_id") is None:
        with store._connect() as db:
            db.execute(
                "UPDATE evidence_bundles SET environment_session_id=? WHERE id=?",
                (environment_session_id, bundle_id),
            )
        bundle["environment_session_id"] = environment_session_id
    if bundle["state"] in {"done", "blocked", "failed"} and bundle.get("bundle_sha256"):
        verify_evidence_bundle(bundle_id)
        return bundle
    _set_bundle_state(bundle_id, "preparing_environment")
    try:
        if manifest.base_sha != base_sha:
            raise CommitBindingError("manifest is not bound to the instance trusted base")
        assert_commit_binding(workspace, head_sha, tree_sha)
    except VerificationError as exc:
        _set_bundle_state(bundle_id, "failed", f"evidence_invariant: {exc}")
        return _seal_bundle(
            bundle_id, final_state="failed", reason=f"evidence_invariant: {exc}",
            phase_b_eligible=False,
        )
    _set_bundle_state(bundle_id, "running")
    root = _evidence_root(instance_id, step_id, activation)
    registry = {**DRIVERS, **(drivers or {})}
    runtime = max(1, int(profile["max_runtime_seconds"]))
    retries = min(1, max(0, int(profile.get("infrastructure_retries", 0))))
    remaining_logs = max(0, min(
        int(profile["max_log_bytes"]), int(profile["max_evidence_bytes"]),
    ))
    env = dict(os.environ if child_env is None else child_env)
    env.update({
        "SHIPFACTORY_INSTANCE_ID": instance_id,
        "SHIPFACTORY_HEAD_SHA": head_sha,
        "SHIPFACTORY_EVIDENCE_BUNDLE_ID": bundle_id,
    })
    for key, value in (environment_identity or {}).items():
        env[f"SHIPFACTORY_ENV_{str(key).upper()}"] = str(value)
    env_digest = _environment_digest(env)
    secret_values = tuple(
        value for key, value in env.items()
        if any(token in key.upper() for token in ("SECRET", "TOKEN", "PASSWORD", "API_KEY"))
    )
    redacted_any = False
    infra_recovered = False
    failure_reason: str | None = None
    case_sources = [("candidate", manifest)]
    if protected_manifest is not None:
        case_sources.append(("protected", protected_manifest))
    for provenance, source_manifest in case_sources:
        for case in source_manifest.document["cases"]:
            persisted_case_id = case["id"] if provenance == "candidate" else f"protected:{case['id']}"
            attempt = 1
            while True:
                env["SHIPFACTORY_CASE_ID"] = persisted_case_id
                env["SHIPFACTORY_CASE_ATTEMPT"] = str(attempt)
                env["SHIPFACTORY_CASE_RUN_KEY"] = (
                    f"verification/{bundle_id}/{persisted_case_id}/{attempt}"
                )
                driver = registry.get(case["driver"])
                if driver is None:
                    result = {
                        "classification": "infrastructure_error",
                        "error": f"unknown driver {case['driver']!r}", "stdout": b"", "stderr": b"",
                        "exit_code": None, "started_at": store._now(), "ended_at": store._now(),
                    }
                else:
                    slot_key = None
                    if case["driver"] == "playwright":
                        slot_key = f"browser_slot:{bundle_id}:{persisted_case_id}:{attempt}"
                        acquired = store.acquire_resource_lease(
                            "browser_slot", int(profile["browser_slots"]), key=slot_key,
                            lease_seconds=runtime + 60,
                            instance_id=instance_id, step_id=step_id,
                            activation=int(activation),
                            metadata={"bundle_id": bundle_id, "case_id": persisted_case_id},
                        )
                        if acquired is None:
                            now = store._now()
                            result = {
                                "classification": "infrastructure_error",
                                "error": "browser slot capacity unavailable",
                                "stdout": b"", "stderr": b"", "exit_code": None,
                                "started_at": now, "ended_at": now,
                            }
                        else:
                            try:
                                result = driver(case, workspace, env, runtime)
                            finally:
                                store.release_resource_lease(slot_key)
                    else:
                        result = driver(case, workspace, env, runtime)
                raw = (
                    b"[stdout]\n" + _output_bytes(result.get("stdout", b""))
                    + b"\n[stderr]\n" + _output_bytes(result.get("stderr", b""))
                )
                if result.get("error"):
                    raw += b"\n[runner-error]\n" + str(result["error"]).encode("utf-8", errors="replace")
                redacted, changed = _redact(raw, secret_values)
                redacted_any |= changed
                capped = len(redacted) > remaining_logs
                data = redacted[:remaining_logs]
                remaining_logs -= len(data)
                item = _persist_log_item(
                    bundle_id=bundle_id, case_id=persisted_case_id, attempt=attempt,
                    data=data, root=root,
                    command=case.get("argv") or ([case["script"]] if "script" in case else None),
                    env_digest=env_digest, exit_code=result.get("exit_code"),
                    started_at=result["started_at"], ended_at=result["ended_at"],
                    metadata={
                        "driver": case["driver"], "provenance": provenance,
                        "capped": capped, "assertion_types": result.get("assertion_types", []),
                        "environment_identity": environment_identity or {},
                        "run_id": result.get("run_id"),
                        "process_start_token": result.get("process_start_token"),
                    },
                )
                status = str(result["classification"])
                _record_case(
                    bundle_id=bundle_id, case_id=persisted_case_id, attempt=attempt,
                    case=case, status=status, item_ids=[item["id"]],
                    started_at=result["started_at"], ended_at=result["ended_at"],
                )
                if status == "infrastructure_error" and attempt <= retries:
                    infra_recovered = True
                    attempt += 1
                    continue
                if status != "passed" and failure_reason is None:
                    if status == "timeout":
                        failure_reason = "test_timeout"
                    elif status == "infrastructure_error":
                        failure_reason = "test_infrastructure_error"
                    else:
                        failure_reason = "test_failed"
                    if provenance == "protected":
                        failure_reason = f"protected_baseline_{failure_reason}"
                break
    _set_bundle_state(bundle_id, "collecting")
    try:
        assert_commit_binding(workspace, head_sha, tree_sha)
    except VerificationError as exc:
        failure_reason = f"evidence_invariant: post-test mutation: {exc}"
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_bundles SET state='redacting',redaction_state=? WHERE id=?",
            ("redacted" if redacted_any else "clean", bundle_id),
        )
    _set_bundle_state(bundle_id, "sealing")
    final_state = "done" if failure_reason is None else (
        "failed" if failure_reason.startswith("evidence_invariant") else "blocked"
    )
    return _seal_bundle(
        bundle_id, final_state=final_state, reason=failure_reason,
        phase_b_eligible=(failure_reason is None and not infra_recovered),
    )


def _preparation_failure_bundle(payload: dict[str, Any], manifest: VerificationManifest,
                                reason: str) -> dict[str, Any]:
    bundle_id = _bundle_id(payload["instance_id"], payload["step_id"], payload["activation"])
    _insert_bundle(
        bundle_id=bundle_id, instance_id=payload["instance_id"],
        step_id=payload["step_id"], activation=int(payload["activation"]),
        input_revision_hash=payload["input_revision_hash"], base_sha=payload["base_sha"],
        head_sha=payload["head_sha"], tree_sha=payload["tree_sha"],
        environment_session_id=None, manifest=manifest,
    )
    return _seal_bundle(
        bundle_id, final_state="blocked", reason=reason, phase_b_eligible=False,
    )


def run_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one journaled verification action or report environment preparation."""
    workspace = Path(payload["workspace"])
    try:
        manifest = load_verification_manifest(
            workspace, payload["base_sha"], payload["manifest_relpath"],
            required_requirement_ids=set(payload.get("required_requirement_ids", [])),
            expected_blob_sha=payload.get("manifest_blob_sha"),
        )
    except VerificationError as exc:
        placeholder = VerificationManifest(
            {}, str(payload.get("manifest_blob_sha") or "unavailable"),
            payload["base_sha"], payload["manifest_relpath"], b"",
        )
        bundle_id = _bundle_id(
            payload["instance_id"], payload["step_id"], int(payload["activation"]),
        )
        _insert_bundle(
            bundle_id=bundle_id, instance_id=payload["instance_id"],
            step_id=payload["step_id"], activation=int(payload["activation"]),
            input_revision_hash=payload["input_revision_hash"],
            base_sha=payload["base_sha"], head_sha=payload["head_sha"],
            tree_sha=payload["tree_sha"], environment_session_id=None,
            manifest=placeholder,
        )
        failed = _seal_bundle(
            bundle_id, final_state="failed",
            reason=f"evidence_invariant: {exc}", phase_b_eligible=False,
        )
        return {"status": "failed", "bundle_id": failed["id"]}
    prepared_id = _bundle_id(
        payload["instance_id"], payload["step_id"], int(payload["activation"]),
    )
    prepared = _insert_bundle(
        bundle_id=prepared_id, instance_id=payload["instance_id"],
        step_id=payload["step_id"], activation=int(payload["activation"]),
        input_revision_hash=payload["input_revision_hash"], base_sha=payload["base_sha"],
        head_sha=payload["head_sha"], tree_sha=payload["tree_sha"],
        environment_session_id=payload.get("environment_session_id"), manifest=manifest,
    )
    if prepared["state"] not in {"done", "blocked", "failed"}:
        _set_bundle_state(prepared_id, "preparing_environment")
    environment_id = payload.get("environment_session_id")
    environment_identity: dict[str, Any] = {}
    if payload.get("environment") == "app":
        from shipfactory import environments

        cfg = payload["environment_config"]
        app_key = (
            f"verification/{payload['instance_id']}/{payload['step_id']}/"
            f"{int(payload['activation'])}"
        )
        app = store.app_session_by_request_key(app_key)
        if app is None:
            env_row = environments.request_materialization(
                repo_root=workspace, workspace=workspace,
                base_sha=payload["base_sha"], candidate_sha=payload["head_sha"], cfg=cfg,
            )
            if env_row is None:
                return {"status": "pending", "reason": "environment capacity queued"}
            if env_row["state"] == "failed":
                bundle = _preparation_failure_bundle(payload, manifest, "environment_failed")
                return {"status": "blocked", "bundle_id": bundle["id"]}
            if env_row["state"] != "ready":
                return {"status": "pending", "reason": "environment materializing"}
            app = environments.request_app_start(
                env_session_id=env_row["id"], request_key=app_key, cfg=cfg,
            )
        if app["state"] in {"crashed", "stopped"}:
            bundle = _preparation_failure_bundle(payload, manifest, "environment_failed")
            return {"status": "blocked", "bundle_id": bundle["id"]}
        if app["state"] != "healthy":
            return {"status": "pending", "reason": "application starting"}
        environment_id = app["env_session_id"]
        environment_identity = {
            "app_session_id": app["id"], "env_session_id": app["env_session_id"],
            "app_url": app["app_url"], "port": app["port"],
        }
    bundle = run_verification(
        instance_id=payload["instance_id"], step_id=payload["step_id"],
        activation=int(payload["activation"]),
        input_revision_hash=payload["input_revision_hash"], base_sha=payload["base_sha"],
        head_sha=payload["head_sha"], tree_sha=payload["tree_sha"],
        workspace=workspace, manifest=manifest, profile=payload["profile"],
        environment_session_id=environment_id,
        environment_identity=environment_identity,
    )
    return {"status": bundle["state"], "bundle_id": bundle["id"]}


def verify_evidence_bundle(bundle_id: str, *, db: Any | None = None) -> dict[str, Any]:
    """Verify a bundle by ID, including its exact sealed item membership."""
    if db is None:
        store.init_db()
        with store._connect() as fresh:
            row = fresh.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
            items = [dict(item) for item in fresh.execute(
                "SELECT * FROM evidence_items WHERE bundle_id=? ORDER BY id", (bundle_id,),
            ).fetchall()]
    else:
        row = db.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
        items = [dict(item) for item in db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=? ORDER BY id", (bundle_id,),
        ).fetchall()]
    if row is None:
        raise EvidenceInvariantError("unknown evidence bundle")
    bundle = dict(row)
    if not bundle.get("bundle_sha256") or not bundle.get("sealed_at"):
        raise EvidenceInvariantError("evidence bundle is not sealed")
    root = _evidence_root(bundle["instance_id"], bundle["step_id"], int(bundle["activation"]))
    bundle_path = root / "bundle.json"
    try:
        document = json.loads(bundle_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise EvidenceInvariantError("sealed bundle manifest is unreadable") from exc
    if document.get("schema") != EVIDENCE_SCHEMA or document.get("id") != bundle_id:
        raise EvidenceInvariantError("sealed bundle identity mismatch")
    if (document.get("outcome_state") != bundle["state"]
            or document.get("invalid_reason") != bundle["invalid_reason"]):
        raise EvidenceInvariantError("sealed bundle outcome mismatch")
    claimed = document.get("items")
    if not isinstance(claimed, list):
        raise EvidenceInvariantError("sealed bundle item set is invalid")
    claimed_pairs = {(item.get("id"), item.get("sha256")) for item in claimed}
    sealed_pairs = {(item["id"], item["sha256"]) for item in items}
    if claimed_pairs != sealed_pairs or len(claimed_pairs) != len(claimed):
        raise EvidenceInvariantError("bundle references evidence outside its sealed set")
    for item in items:
        path = Path(item["path"])
        try:
            path.relative_to(root)
            info = path.lstat()
            data = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise EvidenceInvariantError(f"evidence item {item['id']} path is invalid") from exc
        if (not stat.S_ISREG(info.st_mode) or len(data) != int(item["size_bytes"])
                or hashlib.sha256(data).hexdigest() != item["sha256"]):
            raise EvidenceInvariantError(f"evidence item {item['id']} hash/size mismatch")
    unsigned = dict(document)
    claimed_digest = unsigned.pop("bundle_sha256", None)
    actual_digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    if claimed_digest != actual_digest or bundle["bundle_sha256"] != actual_digest:
        raise EvidenceInvariantError("bundle SHA-256 mismatch")
    if bundle["state"] == "done":
        latest: dict[str, tuple[int, str]] = {}
        for case in document.get("cases", []):
            case_id = case.get("case_id")
            attempt = case.get("attempt")
            status = case.get("status")
            if isinstance(case_id, str) and isinstance(attempt, int):
                if case_id not in latest or attempt > latest[case_id][0]:
                    latest[case_id] = (attempt, status)
        if not latest or any(status != "passed" for _attempt, status in latest.values()):
            raise EvidenceInvariantError("done bundle contains a non-passing required case")
    return bundle


def read_evidence_item(item_id: str) -> tuple[dict[str, Any], bytes]:
    """Serve sealed evidence by opaque ID; callers never provide a path."""
    store.init_db()
    with store._connect() as db:
        row = db.execute("SELECT * FROM evidence_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise EvidenceInvariantError("unknown evidence item")
    item = dict(row)
    verify_evidence_bundle(item["bundle_id"])
    return item, Path(item["path"]).read_bytes()


__all__ = [
    "VERIFICATION_SCHEMA", "EVIDENCE_SCHEMA", "DEFAULT_MANIFEST_PATH", "DRIVERS",
    "VerificationError", "VerificationManifestError", "EvidenceInvariantError",
    "CommitBindingError", "VerificationManifest", "validate_verification_manifest",
    "load_verification_manifest", "control_plane_paths", "assert_commit_binding",
    "run_verification", "run_action", "restore_runs", "verify_evidence_bundle",
    "read_evidence_item",
]
