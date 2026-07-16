"""Deterministic, non-model verification and sealed evidence bundles (SF-9)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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
_COMMAND_CASE_WITH_BEHAVIOR = _COMMAND_CASE | {"surface_behavior"}
_PLAYWRIGHT_CASE = {"id", "requirement_ids", "driver", "script", "assertions"}
_CASE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_BLOB_MODES = {"100644", "100755"}
_MIGRATION_BEHAVIOR_TOKENS = {
    "migration_down": {"down", "downgrade", "rollback", "revert"},
    "migration_up": {"apply", "migrate", "up", "upgrade"},
}
_TRIVIAL_COMMANDS = {"echo", "false", "printf", "test", "true"}
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    # Stops at quote/brace/bracket too, not just whitespace/,/; -- otherwise
    # this greedily eats past a JSON string's closing quote into the next
    # key when scanning structured (HAR-shaped) payloads (finding #10,
    # verification adversarial lane).
    (re.compile(r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;"}\]]+'), r"\1[REDACTED]"),
    (re.compile(r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
    # HAR captures headers/cookies as JSON {"name": ..., "value": ...} pairs,
    # not "Header: value" text -- §2.4.9 requires cookies and auth headers to
    # be stripped from HAR specifically, which the plain-text patterns above
    # cannot see (finding #10, verification adversarial lane).
    (re.compile(
        r'(?i)("name"\s*:\s*"(?:cookie|set-cookie|authorization)"\s*,\s*"value"\s*:\s*")[^"]*(")'
    ), r"\1[REDACTED]\2"),
    (re.compile(r"(?i)((?:^|[;\n])\s*(?:cookie|set-cookie)\s*:\s*)[^\r\n]+"), r"\1[REDACTED]"),
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
    git_home = store._db_path().parent / "verification-git-home"
    git_home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(git_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        result = subprocess.check_output(
            ["git", *args], cwd=repo, env=env,
            stderr=subprocess.PIPE, timeout=15,
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
    elif kind == "pytest_summary":
        if set(oracle) not in ({"type"}, {"type", "min_passed"}):
            raise VerificationManifestError(f"{label} pytest_summary oracle is invalid")
        min_passed = oracle.get("min_passed", 1)
        if not isinstance(min_passed, int) or isinstance(min_passed, bool) or min_passed < 1:
            raise VerificationManifestError(f"{label} pytest_summary min_passed is invalid")
    else:
        raise VerificationManifestError(f"unknown oracle type {kind!r}")


def _migration_tool_identity(argv: list[str]) -> tuple[str, ...]:
    executable = PurePosixPath(argv[0]).name.casefold()
    if executable in _TRIVIAL_COMMANDS:
        raise VerificationManifestError("migration behavior cannot use a no-op command")
    if executable.startswith("python"):
        if len(argv) < 3 or argv[1] in {"-c", "-m"}:
            raise VerificationManifestError(
                "migration behavior must invoke a concrete migration tool"
            )
        return executable, argv[1]
    return (executable,)


def _migration_primary_subcommand(argv: list[str]) -> str:
    executable = PurePosixPath(argv[0]).name.casefold()
    index = 2 if executable.startswith("python") else 1
    if len(argv) <= index:
        raise VerificationManifestError(
            "migration behavior requires a primary migration subcommand"
        )
    return argv[index].casefold().lstrip("-")


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
            keys = set(case)
            if keys != _COMMAND_CASE and keys != _COMMAND_CASE_WITH_BEHAVIOR:
                raise VerificationManifestError(
                    f"{label} keys must be exactly {sorted(_COMMAND_CASE)} or "
                    f"{sorted(_COMMAND_CASE_WITH_BEHAVIOR)}"
                )
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
            behavior = case.get("surface_behavior")
            if behavior is not None:
                if behavior not in _MIGRATION_BEHAVIOR_TOKENS:
                    raise VerificationManifestError(f"{label} surface behavior is invalid")
                if case["oracle"] != {"type": "exit_code", "equals": 0}:
                    raise VerificationManifestError(
                        f"{label} migration behavior must require exit code zero"
                    )
                _migration_tool_identity(argv)
                primary_subcommand = _migration_primary_subcommand(argv)
                if primary_subcommand not in _MIGRATION_BEHAVIOR_TOKENS[behavior]:
                    raise VerificationManifestError(
                        f"{label} primary migration subcommand does not execute "
                        f"declared {behavior} behavior"
                    )
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


def load_verification_manifest_if_present(
    repo_root: str | Path, revision_sha: str,
    relpath: str = DEFAULT_MANIFEST_PATH, *,
    required_requirement_ids: set[str] | None = None,
    expected_blob_sha: str | None = None,
) -> VerificationManifest | None:
    """Load a candidate manifest, returning ``None`` only when the blob is absent."""
    repo = Path(repo_root)
    relpath = _safe_relpath(relpath, "manifest path")
    if not _git(repo, "ls-tree", revision_sha, "--", relpath):
        return None
    return load_verification_manifest(
        repo, revision_sha, relpath,
        required_requirement_ids=required_requirement_ids,
        expected_blob_sha=expected_blob_sha,
        verify_worktree_copy=True,
    )


def control_plane_paths(manifest: VerificationManifest) -> frozenset[str]:
    """Paths whose candidate modification is a verification control-plane risk."""
    scripts = {
        case["script"] for case in manifest.document["cases"]
        if case["driver"] == "playwright"
    }
    return frozenset({manifest.relpath, *scripts})


# §2.4.7 deterministic surface policy: the model may raise the required
# verification profile, never lower it. Ordered weakest -> strictest;
# "stricter" is the floor for anything the deterministic path rules cannot
# classify, since an unrecognized surface might touch anything.
_SURFACE_LEVELS = {"api": 0, "migration": 1, "browser": 2, "stricter": 3}
_UI_PATH_MARKERS = ("/dashboard/", "/frontend/", "/ui/", "/components/", "/pages/", "/views/")
_UI_SUFFIXES = (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html")
_API_PATH_MARKERS = ("/api/", "/routes/", "/endpoints/")
_API_SUFFIXES = ("_api.py", "_routes.py")
_MIGRATION_PATH_MARKERS = ("/migrations/", "/migrate/")


def classify_path_surface(path: str) -> str:
    """Deterministically classify one changed repo-relative path (§2.4.7)."""
    lower = f"/{path.lower()}"
    if any(marker in lower for marker in _MIGRATION_PATH_MARKERS):
        return "migration"
    if any(marker in lower for marker in _UI_PATH_MARKERS) or lower.endswith(_UI_SUFFIXES):
        return "browser"
    if any(marker in lower for marker in _API_PATH_MARKERS) or lower.endswith(_API_SUFFIXES):
        return "api"
    return "stricter"


def classify_required_surface(
    paths: list[str], *, model_risk_surface: str | None = None,
) -> str:
    """Return the floor verification surface for a changed-path set.

    ``model_risk_surface`` (a model's own risk classification) may only
    raise the deterministic floor, per §2.4.7 -- it is combined with
    ``max``, never substituted for the deterministic result.
    """
    levels = {classify_path_surface(path) for path in paths} or {"stricter"}
    floor = max(levels, key=lambda level: _SURFACE_LEVELS[level])
    if model_risk_surface in _SURFACE_LEVELS:
        floor = max([floor, model_risk_surface], key=lambda level: _SURFACE_LEVELS[level])
    return floor


def surface_paths_from_documents(*documents: Any) -> list[str]:
    """Extract repository-path claims from sealed spec/plan JSON."""
    found: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            candidates = [value]
            if " " in value:
                candidates = re.findall(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", value)
            for candidate in candidates:
                if ("path" in key or "/" in candidate
                        or candidate.casefold().endswith(_UI_SUFFIXES + _API_SUFFIXES)):
                    try:
                        found.add(_safe_relpath(candidate, "sealed spec/plan path"))
                    except VerificationManifestError:
                        continue

    for document in documents:
        visit(document)
    return sorted(found)


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
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for value in secret_values:
        if value and len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    return text.encode("utf-8"), text != original


def _environment_digest(env: dict[str, str]) -> str:
    return hashlib.sha256(_canonical(env)).hexdigest()


_CAPTURE_KINDS = frozenset({"video", "trace", "screenshot", "har"})
_CAPTURE_TEXT_KINDS = frozenset({"trace", "har"})
_CAPTURE_BINARY_KINDS = frozenset({"video", "screenshot"})
_CAPTURE_MAGIC = b"SFEV1\n"


class CaptureContainerError(EvidenceInvariantError):
    """A captured evidence item's container is malformed, foreign, or truncated."""


def build_capture_container(
    kind: str, payload: bytes, *, instance_id: str, head_sha: str, bundle_id: str,
    case_id: str, attempt: int, captured_at: str, redaction_state: str | None = None,
) -> bytes:
    """Wrap capture bytes with a runner-owned identity header the driver cannot influence.

    Real video/trace/screenshot capture should burn a runner-generated
    overlay (instance ID, head SHA, case ID, timestamp) into evidence so a
    copy of stale bytes into a fresh evidence directory is detectable even
    though the copy's own sha256 is internally self-consistent (§2.4.6,
    finding #2 verification adversarial lane -- attacks #2/#14/#19). This
    container is the deterministic, codec-independent analog of that
    overlay: the header's identity is compared against the bundle's own
    trusted row at verify time, and the header's declared payload hash/
    length are compared against the actual trailing bytes to catch
    truncation after a valid header was written.
    """
    if kind not in _CAPTURE_KINDS:
        raise ValueError(f"unknown capture kind {kind!r}")
    header = _canonical({
        "schema": "shipfactory.capture-identity/v1", "kind": kind,
        "instance_id": instance_id, "head_sha": head_sha, "bundle_id": bundle_id,
        "case_id": case_id, "attempt": int(attempt), "captured_at": captured_at,
        "redaction_state": redaction_state,
        "payload_sha256": hashlib.sha256(payload).hexdigest(), "payload_length": len(payload),
    })
    return _CAPTURE_MAGIC + len(header).to_bytes(4, "big") + header + payload


def _parse_capture_container(data: bytes) -> tuple[dict[str, Any], bytes]:
    if not data.startswith(_CAPTURE_MAGIC):
        raise CaptureContainerError("capture container magic is missing or corrupt")
    rest = data[len(_CAPTURE_MAGIC):]
    if len(rest) < 4:
        raise CaptureContainerError("capture container header length is truncated")
    header_len = int.from_bytes(rest[:4], "big")
    header_bytes = rest[4:4 + header_len]
    if len(header_bytes) != header_len:
        raise CaptureContainerError("capture container header is truncated")
    try:
        header = json.loads(header_bytes)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureContainerError("capture container header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise CaptureContainerError("capture container header is not an object")
    return header, rest[4 + header_len:]


def _validate_capture_container(
    data: bytes, *, expected_instance_id: str, expected_head_sha: str,
    expected_bundle_id: str, expected_case_id: str, expected_kind: str | None = None,
    expected_attempt: int | None = None, expected_captured_at: str | None = None,
) -> dict[str, Any]:
    header, payload = _parse_capture_container(data)
    if (header.get("instance_id") != expected_instance_id
            or header.get("head_sha") != expected_head_sha
            or header.get("bundle_id") != expected_bundle_id
            or header.get("case_id") != expected_case_id
            or (expected_kind is not None and header.get("kind") != expected_kind)
            or (expected_attempt is not None and header.get("attempt") != int(expected_attempt))
            or (expected_captured_at is not None
                and header.get("captured_at") != expected_captured_at)):
        raise CaptureContainerError(
            "capture container identity does not match this evidence bundle"
        )
    if (header.get("payload_sha256") != hashlib.sha256(payload).hexdigest()
            or header.get("payload_length") != len(payload)):
        raise CaptureContainerError("capture container payload was truncated or replaced")
    try:
        datetime.fromisoformat(str(header.get("captured_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CaptureContainerError("capture container timestamp is invalid") from exc
    return header


def _redact_json_value(value: Any, secret_values: tuple[str, ...], *, parent: str = "") -> tuple[Any, bool]:
    """Structurally redact JSON headers/cookies independent of key ordering."""
    changed = False
    if isinstance(value, list):
        result = []
        for item in value:
            redacted, item_changed = _redact_json_value(item, secret_values, parent=parent)
            result.append(redacted)
            changed |= item_changed
        return result, changed
    if isinstance(value, dict):
        result = dict(value)
        lowered = {str(key).casefold(): key for key in value}
        name_key = lowered.get("name")
        value_key = lowered.get("value")
        if name_key is not None and value_key is not None:
            name = str(value[name_key]).casefold()
            if name in {"authorization", "proxy-authorization", "cookie", "set-cookie"} or "cookie" in parent:
                if result[value_key] != "[REDACTED]":
                    result[value_key] = "[REDACTED]"
                    changed = True
        for key, item in list(result.items()):
            key_name = str(key).casefold()
            if key_name in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
                if item != "[REDACTED]":
                    result[key] = "[REDACTED]"
                    changed = True
                continue
            redacted, item_changed = _redact_json_value(
                item, secret_values, parent=key_name,
            )
            result[key] = redacted
            changed |= item_changed
        return result, changed
    if isinstance(value, str):
        redacted, item_changed = _redact(value.encode("utf-8"), secret_values)
        return redacted.decode("utf-8"), item_changed
    return value, False


def _structured_json_redaction(
    payload: bytes, secret_values: tuple[str, ...],
) -> tuple[bytes, str]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload, "uncertain"
    redacted, changed = _redact_json_value(document, secret_values)
    return _canonical(redacted), ("redacted" if changed else "clean")


def _redact_capture_payload(
    kind: str, payload: bytes, secret_values: tuple[str, ...] = (),
) -> tuple[bytes, str]:
    """Redact one captured artifact's payload, or mark it uncertain if it cannot be scanned.

    Binary captures (screenshot, video) cannot be text-scanned for secrets;
    per §2.4.9 an uncertain redaction must block sealing rather than claim a
    clean pass. Text-shaped captures (trace, HAR) reuse the same pattern
    scanner as command output.
    """
    if kind in _CAPTURE_BINARY_KINDS:
        return payload, "uncertain"
    # HAR and provider traces are structured artifacts.  Invalid JSON or a
    # binary trace is never replacement-decoded and relabelled clean.
    if kind in {"har", "trace"}:
        return _structured_json_redaction(payload, secret_values)
    return payload, "uncertain"


def _persist_trusted_capture_container(
    *, bundle_id: str, instance_id: str, head_sha: str, case_id: str, attempt: int,
    kind: str, container_path: Path, root: Path, mime_type: str,
    started_at: str, ended_at: str, max_bytes: int,
) -> dict[str, Any]:
    """Persist a container already stamped by the trusted capture subprocess.

    The parent never takes arbitrary driver bytes and gives them a fresh
    identity.  It accepts only a pre-stamped container from the runner-owned
    capture directory and validates every identity field before copying it.
    """
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(container_path, flags)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = source.read(min(65536, int(max_bytes) + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > int(max_bytes):
                    break
            after = os.fstat(source.fileno())
        data = b"".join(chunks)
    except OSError as exc:
        raise CaptureContainerError("trusted capture output is unreadable") from exc
    stable = (
        before.st_size == after.st_size == len(data)
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )
    if not stat.S_ISREG(before.st_mode) or len(data) > int(max_bytes):
        raise CaptureContainerError("trusted capture output exceeds evidence budget")
    if not stable:
        raise CaptureContainerError("trusted capture output was modified while being read")
    header = _validate_capture_container(
        data, expected_instance_id=instance_id, expected_head_sha=head_sha,
        expected_bundle_id=bundle_id, expected_case_id=case_id,
        expected_kind=kind, expected_attempt=attempt,
        expected_captured_at=started_at,
    )
    redaction_state = header.get("redaction_state")
    if redaction_state not in {"clean", "redacted", "uncertain"}:
        raise CaptureContainerError("capture container redaction state is invalid")
    _parsed_header, payload = _parse_capture_container(data)
    verified_payload, verified_state = _redact_capture_payload(kind, payload)
    redaction_attested = (
        verified_state == redaction_state
        or (verified_state == "clean" and redaction_state == "redacted"
            and b"[REDACTED]" in payload)
    )
    if verified_payload != payload or not redaction_attested:
        raise CaptureContainerError(
            "capture container was not structurally redacted by the trusted runner"
        )
    ident = _item_id(bundle_id, case_id, attempt, kind)
    path = root / "items" / f"{ident}.{kind}"
    sealed = _copy_once(path, data)
    digest = hashlib.sha256(sealed).hexdigest()
    metadata = {"redaction_state": redaction_state, "attempt": int(attempt)}
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO evidence_items"
            "(id,bundle_id,case_id,kind,path,sha256,size_bytes,mime_type,producer,"
            "command_json,cwd_relpath,env_digest,exit_code,started_at,ended_at,metadata_json,attempt) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, bundle_id, case_id, kind, str(path), digest, len(sealed), mime_type,
             "verification-capture-runner", None, ".", None, None, started_at, ended_at,
             json.dumps(metadata, sort_keys=True), int(attempt)),
        )
    return {"id": ident, "sha256": digest, "size_bytes": len(sealed), "kind": kind,
            "redaction_state": redaction_state}


def _uncertain_capture_reason(bundle_id: str) -> str | None:
    with store._connect() as db:
        rows = db.execute(
            "SELECT id,metadata_json FROM evidence_items WHERE bundle_id=? ORDER BY id",
            (bundle_id,),
        ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("redaction_state") == "uncertain":
            return f"redaction_failed: evidence item {row['id']} redaction is uncertain"
    return None


def _minimal_case_env(
    workspace: Path, profile: dict[str, Any], bundle_id: str,
) -> dict[str, str]:
    """Build the complete allowlisted environment for candidate-owned commands."""
    workspace_key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
    home = store._db_path().parent / "verification-homes" / workspace_key / bundle_id
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
    }
    for key, value in (profile.get("env", {}) or {}).items():
        if (not isinstance(key, str) or not key or "=" in key or "\x00" in key
                or key == "HOME" or key.startswith("SHIPFACTORY_")
                or not isinstance(value, str) or "\x00" in value):
            raise VerificationManifestError(
                f"verification profile env contains unsafe variable {key!r}"
            )
        env[key] = value
    return env


def _runner_env(bundle_id: str) -> dict[str, str]:
    """Build a secret-free environment for the trusted verification runner."""
    home = store._db_path().parent / "verification-runner-homes" / bundle_id
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "HERMES_HOME": str(store._db_path().parent.parent),
    }
    python_paths = [str(Path(__file__).resolve().parent.parent)]
    if os.environ.get("PYTHONPATH"):
        python_paths.append(os.environ["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return env


def _output_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8", errors="replace")


def _kill_child(proc: subprocess.Popen[bytes], token: str | None) -> None:
    from shipfactory import spawn
    spawn.verified_killpg(proc.pid, token, signal.SIGKILL)


class _ProcessTreeTracker:
    """Continuously capture descendant PID/start-token pairs for safe cleanup."""

    def __init__(self, proc: subprocess.Popen[bytes], scope: str):
        try:
            import psutil
        except ImportError as exc:
            raise VerificationError("psutil is required for supervised process cleanup") from exc
        self._psutil = psutil
        self.proc = proc
        self.scope = scope
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.identities: dict[int, tuple[str, int]] = {}
        self.available = True
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout=2):
            raise VerificationError("process-tree supervisor failed to start")

    def _scan(self) -> None:
        from shipfactory import spawn
        try:
            descendants = self._psutil.Process(self.proc.pid).children(recursive=True)
        except (RuntimeError, SystemError):
            self.available = False
            descendants = []
        except (self._psutil.Error, ProcessLookupError, OSError, PermissionError):
            descendants = []
        # A child can call setsid() and be reparented before an ancestry poll.
        # The runner-owned nonce is inherited in its environment, so a full
        # process scan still finds that detached scope without relying on a
        # stale PID or a parent relationship.
        try:
            for candidate in self._psutil.process_iter(["pid"]):
                if int(candidate.pid) == os.getpid():
                    continue
                try:
                    if candidate.environ().get("SHIPFACTORY_SUPERVISION_SCOPE") == self.scope:
                        descendants.append(candidate)
                except (RuntimeError, SystemError):
                    # psutil's macOS proc_environ bridge can fail transiently
                    # after a process exits. Do not abort the runner, but mark
                    # the scope scan incomplete so the case fails closed.
                    self.available = False
                    continue
                except (self._psutil.Error, OSError, PermissionError):
                    continue
        except (self._psutil.Error, OSError, PermissionError, RuntimeError, SystemError):
            self.available = False
        for child in descendants:
            token = spawn._process_start_token(int(child.pid))
            if not token:
                continue
            try:
                pgid = os.getpgid(int(child.pid))
            except OSError:
                pgid = -1
            self.identities[int(child.pid)] = (token, int(pgid))

    def _watch(self) -> None:
        self._scan()
        self.ready.set()
        while not self._stop.wait(0.01):
            self._scan()

    def cleanup(self) -> None:
        from shipfactory import spawn
        self._stop.set()
        self._thread.join(timeout=2)
        # psutil's global process-iteration cache is not a useful source of
        # truth when two scans race. Stop the watcher, then take one final
        # serialized scope scan before signalling anything.
        self._scan()
        # Descendants that created their own session/process group are fenced
        # as group leaders; ordinary descendants are fenced individually.
        for pid, (token, pgid) in sorted(self.identities.items(), reverse=True):
            if pgid == pid:
                spawn.verified_killpg(pid, token, signal.SIGKILL)
            else:
                spawn.verified_kill(pid, token, signal.SIGKILL)


def _communicate_token_fenced(
    proc: subprocess.Popen[bytes], token: str | None, timeout: float,
    tracker: _ProcessTreeTracker | None = None,
) -> tuple[bytes, bytes, bool]:
    """Drain output while retaining a waitable leader until group cleanup."""
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def drain(stream: Any, target: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            target.append(chunk)

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, stderr_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = monotonic() + max(0.1, float(timeout))
    timed_out = False
    while True:
        try:
            waited = os.waitid(
                os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except (AttributeError, ChildProcessError, OSError):
            waited = None
            if proc.poll() is not None:
                break
        if waited is not None:
            # The leader remains waitable, so its start token still fences the
            # process-group signal.  No raw killpg after reap is needed.
            from shipfactory import spawn
            spawn.verified_killpg(proc.pid, token, signal.SIGKILL)
            break
        if monotonic() >= deadline:
            timed_out = True
            _kill_child(proc, token)
            break
        threading.Event().wait(0.01)
    if tracker is not None:
        tracker.cleanup()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_child(proc, token)
        proc.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=5)
    return b"".join(stdout_chunks), b"".join(stderr_chunks), timed_out


def _is_pytest_argv(argv: list[str]) -> bool:
    executable = PurePosixPath(argv[0]).name.casefold()
    if executable in {"pytest", "py.test"}:
        return True
    return (
        executable.startswith("python") and len(argv) >= 3
        and argv[1:3] == ["-m", "pytest"]
    )


def _trusted_interpreter_path(raw: str, workspace: Path) -> str:
    """Resolve an executable without allowing candidate-owned interpreter bytes."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        if candidate.parent != Path("."):
            raise VerificationError("pytest interpreter must not be workspace-relative")
        found = shutil.which(raw)
        if not found:
            raise VerificationError("pytest interpreter is not available on trusted PATH")
        candidate = Path(found)
    try:
        candidate_absolute = candidate.absolute()
        resolved = candidate.resolve(strict=True)
        workspace_resolved = workspace.resolve(strict=True)
    except OSError as exc:
        raise VerificationError("pytest interpreter identity is unreadable") from exc
    if (candidate_absolute == workspace_resolved or workspace_resolved in candidate_absolute.parents
            or resolved == workspace_resolved or workspace_resolved in resolved.parents
            or not resolved.is_file() or not os.access(resolved, os.X_OK)):
        raise VerificationError("pytest interpreter must be executable and outside workspace")
    # Preserve an absolute virtualenv launcher path: resolving its symlink to
    # the base interpreter discards pyvenv.cfg discovery and its site packages.
    return str(candidate_absolute)


def _isolated_pytest_argv(argv: list[str], workspace: Path) -> list[str]:
    """Replace a claimed pytest executable with the trusted isolated runner."""
    executable = PurePosixPath(argv[0]).name.casefold()
    if executable.startswith("python") and argv[1:3] == ["-m", "pytest"]:
        interpreter = _trusted_interpreter_path(argv[0], workspace)
        pytest_args = argv[3:]
    else:
        pytest_executable = _trusted_interpreter_path(argv[0], workspace)
        try:
            first = Path(pytest_executable).read_bytes().splitlines()[0].decode("utf-8")
        except (OSError, UnicodeDecodeError, IndexError) as exc:
            raise VerificationError("pytest interpreter identity is unreadable") from exc
        if not first.startswith("#!"):
            raise VerificationError("pytest executable has no trusted interpreter identity")
        shebang = shlex.split(first[2:].strip())
        if not shebang:
            raise VerificationError("pytest executable has no trusted interpreter identity")
        if Path(shebang[0]).name == "env":
            if len(shebang) != 2:
                raise VerificationError("pytest env shebang is ambiguous")
            interpreter = _trusted_interpreter_path(shebang[1], workspace)
        else:
            interpreter = _trusted_interpreter_path(shebang[0], workspace)
        pytest_args = argv[1:]
    return [
        interpreter, "-I", str(Path(__file__).with_name("pytest_runner.py")), *pytest_args,
    ]


def _pytest_evidence_ok(path: Path, nonce: str, min_passed: int) -> bool:
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(
        evidence.get("schema") == "shipfactory.pytest-evidence/v1"
        and evidence.get("nonce") == nonce
        and evidence.get("exitstatus") == 0
        and isinstance(evidence.get("collected"), int)
        and evidence["collected"] >= int(min_passed)
        and evidence.get("passed", 0) >= int(min_passed)
        and evidence.get("failed", 0) == 0
        and evidence.get("errors", 0) == 0
        and evidence.get("deselected", 0) < evidence["collected"] + evidence.get("deselected", 0)
    )


def run_supervised_sidecar(
    argv: list[str], *, cwd: Path, env: dict[str, str], grace_seconds: float = 2.0,
    ready_path: Path | None = None, ready_timeout: float = 5.0,
) -> tuple[subprocess.Popen[bytes], str | None]:
    """Start a background capture-style sidecar (e.g. a future ffmpeg wrapper).

    Returns the process and its verified OS start token; the caller MUST
    call :func:`stop_supervised_sidecar` before treating the case as
    finished, or a hung sidecar (finding #10, verification adversarial lane
    -- attack #12, "ffmpeg hangs after tests finish") outlives evidence
    collection.
    """
    scope = uuid.uuid4().hex
    supervised_env = dict(env)
    supervised_env["SHIPFACTORY_SUPERVISION_SCOPE"] = scope
    proc = subprocess.Popen(
        argv, cwd=cwd, env=supervised_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    from shipfactory import spawn
    token = spawn._capture_start_token(proc.pid, proc)
    _SIDECAR_TRACKERS[proc.pid] = _ProcessTreeTracker(proc, scope)
    if ready_path is not None:
        deadline = monotonic() + max(0.1, float(ready_timeout))
        while not ready_path.exists():
            if proc.poll() is not None:
                _SIDECAR_TRACKERS.pop(proc.pid).cleanup()
                raise VerificationError("capture sidecar exited before readiness")
            if monotonic() >= deadline:
                stop_supervised_sidecar(proc, token, grace_seconds=grace_seconds)
                raise VerificationError("capture sidecar readiness timed out")
            threading.Event().wait(0.01)
    return proc, token


def stop_supervised_sidecar(
    proc: subprocess.Popen[bytes], token: str | None, *, grace_seconds: float = 2.0,
) -> int:
    """Terminate a sidecar deterministically: SIGTERM, then SIGKILL escalation.

    Never blocks indefinitely on a sidecar that ignores SIGTERM (a hung
    ffmpeg-equivalent) -- evidence collection must proceed regardless.
    """
    from shipfactory import spawn
    if proc.poll() is None:
        spawn.verified_killpg(proc.pid, token, signal.SIGTERM)
        try:
            proc.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            spawn.verified_killpg(proc.pid, token, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    tracker = _SIDECAR_TRACKERS.pop(proc.pid, None)
    if tracker is not None:
        tracker.cleanup()
    return -1 if proc.returncode is None else proc.returncode


Driver = Callable[[dict[str, Any], Path, dict[str, str], int], dict[str, Any]]
_SIDECAR_TRACKERS: dict[int, _ProcessTreeTracker] = {}


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
    pytest_path: Path | None = None
    pytest_nonce: str | None = None
    command_env = dict(env)
    supervision_scope = uuid.uuid4().hex
    command_env["SHIPFACTORY_SUPERVISION_SCOPE"] = supervision_scope
    if case["oracle"]["type"] == "pytest_summary":
        if not _is_pytest_argv(case["argv"]):
            now = store._now()
            store.record_run_end(
                run_id, -1, None, None, monotonic() - started_monotonic, "failed",
            )
            return {
                "classification": "failed",
                "error": "pytest_summary requires a real pytest argv",
                "stdout": b"", "stderr": b"", "exit_code": None,
                "started_at": started, "ended_at": now, "run_id": run_id,
            }
        try:
            command_argv = _isolated_pytest_argv(case["argv"], workspace)
        except VerificationError as exc:
            now = store._now()
            store.record_run_end(
                run_id, -1, None, None, monotonic() - started_monotonic, "failed",
            )
            return {
                "classification": "failed", "error": str(exc), "stdout": b"", "stderr": b"",
                "exit_code": None, "started_at": started, "ended_at": now, "run_id": run_id,
            }
        pytest_path = Path(command_env["HOME"]) / f"pytest-evidence-{uuid.uuid4().hex}.json"
        pytest_nonce = uuid.uuid4().hex
        command_env["SHIPFACTORY_PYTEST_EVIDENCE_PATH"] = str(pytest_path)
        command_env["SHIPFACTORY_PYTEST_EVIDENCE_NONCE"] = pytest_nonce
        command_env["PYTHONDONTWRITEBYTECODE"] = "1"
        command_env["PYTHONNOUSERSITE"] = "1"
        command_env["PYTEST_ADDOPTS"] = " ".join(filter(None, [
            command_env.get("PYTEST_ADDOPTS", ""), "-p no:cacheprovider",
        ]))
    else:
        command_argv = case["argv"]
    try:
        proc = subprocess.Popen(
            command_argv, cwd=workspace, env=command_env, stdin=subprocess.DEVNULL,
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
    tracker = _ProcessTreeTracker(proc, supervision_scope)
    stdout, stderr, timed_out = _communicate_token_fenced(
        proc, process_start_token, max(1, int(timeout)), tracker,
    )
    if not tracker.available:
        store.record_run_end(
            run_id, proc.returncode, None, None, monotonic() - started_monotonic,
            "infrastructure_error",
        )
        return {
            "classification": "infrastructure_error",
            "error": "process-tree supervision was incomplete",
            "stdout": stdout, "stderr": stderr, "exit_code": proc.returncode,
            "started_at": started, "ended_at": store._now(), "run_id": run_id,
            "process_start_token": process_start_token,
            "process_tree_supervision": "incomplete",
        }
    if timed_out:
        store.record_run_end(
            run_id, proc.returncode, None, None, monotonic() - started_monotonic, "timeout",
        )
        return {
            "classification": "timeout", "stdout": stdout, "stderr": stderr,
            "exit_code": proc.returncode, "started_at": started, "ended_at": store._now(),
            "run_id": run_id, "process_start_token": process_start_token,
            "process_tree_supervision": "complete" if tracker.available else "ancestry_only",
        }
    oracle = case["oracle"]
    if oracle["type"] == "exit_code":
        passed = proc.returncode == int(oracle["equals"])
    elif oracle["type"] == "pytest_summary":
        passed = bool(
            proc.returncode == 0 and pytest_path is not None and pytest_nonce is not None
            and _pytest_evidence_ok(
                pytest_path, pytest_nonce, int(oracle.get("min_passed", 1)),
            )
        )
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
        "process_tree_supervision": "complete" if tracker.available else "ancestry_only",
    }


def _playwright_python() -> str | None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        executable = shutil.which("playwright")
        if not executable:
            return None
        try:
            first = Path(executable).read_bytes().splitlines()[0].decode("utf-8")
        except (OSError, UnicodeDecodeError, IndexError):
            return None
        if not first.startswith("#!"):
            return None
        candidate = first[2:].strip().split()[0]
        path = Path(candidate)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            return None
        return str(path)
    return sys.executable


def _playwright_browsers_path(interpreter: str) -> str | None:
    """Resolve the installed browser cache before the child HOME is isolated."""
    probe = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
    )
    try:
        result = subprocess.run(
            [interpreter, "-I", "-c", probe], text=True, capture_output=True,
            timeout=5, check=False,
            env={
                "HOME": str(Path.home()),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            },
        )
        if result.returncode != 0:
            return None
        executable = Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None
    for parent in executable.parents:
        if parent.name == "ms-playwright":
            return str(parent)
    return None


def _playwright_driver(
    case: dict[str, Any], workspace: Path, env: dict[str, str], timeout: int,
) -> dict[str, Any]:
    started = store._now()
    interpreter = _playwright_python()
    app_url = env.get("SHIPFACTORY_ENV_APP_URL")
    if interpreter is None or not app_url:
        return {
            "classification": "infrastructure_error",
            "error": "required Playwright/browser or application URL is unavailable",
            "stdout": b"", "stderr": b"", "exit_code": None,
            "started_at": started, "ended_at": store._now(),
            "assertion_types": [item["type"] for item in case["assertions"]],
        }
    bundle_id = env["SHIPFACTORY_EVIDENCE_BUNDLE_ID"]
    case_id = env["SHIPFACTORY_CASE_ID"]
    attempt = int(env["SHIPFACTORY_CASE_ATTEMPT"])
    action_root = (
        store._db_path().parent / "verification-captures" / bundle_id
        / hashlib.sha256(f"{case_id}|{attempt}".encode()).hexdigest()
    )
    action_root.mkdir(parents=True, exist_ok=True)
    request_path = action_root / "request.json"
    result_path = action_root / "result.json"
    ready_path = action_root / "runner.ready"
    request = {
        "case": case, "app_url": app_url, "timeout": int(timeout),
        "operation_timeout_ms": max(250, min(10_000, int(timeout * 1000 * 0.4))),
        "instance_id": env["SHIPFACTORY_INSTANCE_ID"],
        "head_sha": env["SHIPFACTORY_HEAD_SHA"], "bundle_id": bundle_id,
        "case_id": case_id, "attempt": attempt, "captured_at": started,
        "capture": json.loads(env.get("SHIPFACTORY_CAPTURE_POLICY", "{}")),
        "output_dir": str(action_root), "result_path": str(result_path),
        "ready_path": str(ready_path),
    }
    _copy_once(request_path, _canonical(request) + b"\n")
    child_env = _runner_env(bundle_id)
    browsers_path = _playwright_browsers_path(interpreter)
    if browsers_path is not None:
        # Keep the runner's HOME private while granting access only to the
        # already-resolved trusted browser cache.
        child_env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    child_env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(Path(__file__).resolve().parent.parent), child_env.get("PYTHONPATH", ""),
    ]))
    try:
        proc, token = run_supervised_sidecar(
            [interpreter, str(Path(__file__).resolve()), "--playwright-runner", str(request_path)],
            cwd=workspace, env=child_env, ready_path=ready_path,
        )
        supervision_tracker: _ProcessTreeTracker | None = None
        try:
            stdout, stderr = proc.communicate(timeout=max(1, int(timeout)))
        except subprocess.TimeoutExpired:
            supervision_tracker = _SIDECAR_TRACKERS.get(proc.pid)
            stop_supervised_sidecar(proc, token, grace_seconds=1.0)
            stdout, stderr = proc.communicate()
            return {
                "classification": "timeout", "stdout": stdout, "stderr": stderr,
                "exit_code": proc.returncode, "started_at": started, "ended_at": store._now(),
                "assertion_types": [item["type"] for item in case["assertions"]],
            }
        finally:
            tracker = supervision_tracker or _SIDECAR_TRACKERS.get(proc.pid)
            if proc.poll() is None:
                stop_supervised_sidecar(proc, token, grace_seconds=1.0)
            else:
                registered = _SIDECAR_TRACKERS.pop(proc.pid, None)
                if registered is not None:
                    registered.cleanup()
                    tracker = registered
            if tracker is None or not tracker.available:
                raise VerificationError("process-tree supervision was incomplete")
        if proc.returncode != 0:
            raise VerificationError(f"Playwright runner exited {proc.returncode}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, VerificationError, json.JSONDecodeError) as exc:
        return {
            "classification": "infrastructure_error", "error": str(exc),
            "stdout": locals().get("stdout", b""), "stderr": locals().get("stderr", b""),
            "exit_code": locals().get("proc").returncode if locals().get("proc") else None,
            "started_at": started, "ended_at": store._now(),
            "assertion_types": [item["type"] for item in case["assertions"]],
        }
    return {
        "classification": result["classification"],
        "error": result.get("error"), "stdout": stdout, "stderr": stderr,
        "exit_code": proc.returncode, "started_at": started, "ended_at": store._now(),
        "assertion_types": [item["type"] for item in case["assertions"]],
        "capture_containers": result.get("capture_containers", []),
    }


DRIVERS: dict[str, Driver] = {
    "command": _command_driver,
    "playwright": _playwright_driver,
}


_RUNNING: dict[str, dict[str, Any]] = {}
_RESTORED_HOMES: set[str] = set()


def reap_runs() -> list[dict[str, Any]]:
    """Poll locally-owned runner children without waiting for case completion."""
    finished: list[dict[str, Any]] = []
    for bundle_id, record in list(_RUNNING.items()):
        code = record["proc"].poll()
        if code is None:
            continue
        store.record_run_end(
            record["run_id"], code, None, None,
            monotonic() - record["started_monotonic"],
            "done" if code == 0 else "verification_runner_failed",
        )
        with store._connect() as db:
            bundle = db.execute(
                "SELECT state,bundle_sha256 FROM evidence_bundles WHERE id=?", (bundle_id,),
            ).fetchone()
        if not bundle or bundle["state"] not in {"done", "blocked", "failed"}:
            _runner_failure_bundle(
                record["payload"], f"verification runner exited {code} before sealing"
            )
        finished.append({"bundle_id": bundle_id, "exit_code": code})
        del _RUNNING[bundle_id]
    return finished


def restore_runs() -> list[int]:
    """Fence orphaned verification children by exact A1 process identity."""
    from shipfactory import spawn

    reap_runs()
    home_key = str(store._db_path())
    if home_key in _RESTORED_HOMES:
        return []
    crashed: list[int] = []
    for row in store.nonterminal_verification_runs():
        pid = row.get("pid")
        token = row.get("process_start_token")
        if pid:
            spawn.verified_killpg(int(pid), token, signal.SIGKILL)
        store.record_run_crashed(int(row["id"]), "daemon restarted during verification")
        crashed.append(int(row["id"]))
    _RESTORED_HOMES.add(home_key)
    return crashed


def _insert_bundle(
    *, bundle_id: str, instance_id: str, step_id: str, activation: int,
    input_revision_hash: str, base_sha: str, head_sha: str, tree_sha: str,
    environment_session_id: str | None, manifest: VerificationManifest,
    workspace_path: str | None = None, workspace_owner_task_id: str | None = None,
    workspace_owner_activation: int | None = None, workspace_owner_run_id: int | None = None,
    required_surface: str | None = None,
    environment_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO evidence_bundles"
            "(id,instance_id,step_id,activation,input_revision_hash,base_sha,head_sha,tree_sha,"
            "environment_session_id,manifest_relpath,manifest_blob_sha,state,redaction_state,created_at,"
            "workspace_path,workspace_owner_task_id,workspace_owner_activation,workspace_owner_run_id,"
            "required_surface,environment_identity_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'ready','not_required',?,?,?,?,?,?,?)",
            (bundle_id, instance_id, step_id, int(activation), input_revision_hash,
             base_sha, head_sha, tree_sha, environment_session_id, manifest.relpath,
             manifest.blob_sha, store._now(), workspace_path, workspace_owner_task_id,
             workspace_owner_activation, workspace_owner_run_id, required_surface,
             json.dumps(environment_identity or {}, sort_keys=True)),
        )
        db.execute(
            "UPDATE evidence_bundles SET "
            "environment_session_id=COALESCE(environment_session_id,?),"
            "workspace_path=COALESCE(workspace_path,?),"
            "workspace_owner_task_id=COALESCE(workspace_owner_task_id,?),"
            "workspace_owner_activation=COALESCE(workspace_owner_activation,?),"
            "workspace_owner_run_id=COALESCE(workspace_owner_run_id,?),"
            "required_surface=COALESCE(required_surface,?),"
            "environment_identity_json=CASE WHEN environment_identity_json='{}' THEN ? ELSE environment_identity_json END "
            "WHERE id=?",
            (environment_session_id, workspace_path, workspace_owner_task_id,
             workspace_owner_activation, workspace_owner_run_id, required_surface,
             json.dumps(environment_identity or {}, sort_keys=True), bundle_id),
        )
        row = db.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
        security = {
            "instance_id": instance_id, "step_id": step_id, "activation": int(activation),
            "input_revision_hash": input_revision_hash, "base_sha": base_sha,
            "head_sha": head_sha, "tree_sha": tree_sha,
            "manifest_relpath": manifest.relpath, "manifest_blob_sha": manifest.blob_sha,
        }
        for field, expected in security.items():
            if row[field] != expected:
                raise EvidenceInvariantError(f"bundle identity drift for {field}")
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
            "command_json,cwd_relpath,env_digest,exit_code,started_at,ended_at,metadata_json,attempt) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, bundle_id, case_id, "log", str(path), digest, len(sealed),
             "text/plain; charset=utf-8", "verification-runner",
             json.dumps(command) if command is not None else None, ".", env_digest,
             exit_code, started_at, ended_at, json.dumps(metadata, sort_keys=True), int(attempt)),
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
                    outcome_state: str, invalid_reason: str | None,
                    required_case_ids: list[str]) -> dict[str, Any]:
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
        "instance_id": bundle["instance_id"], "step_id": bundle["step_id"],
        "activation": int(bundle["activation"]),
        "input_revision_hash": bundle["input_revision_hash"],
        "base_sha": bundle["base_sha"], "head_sha": bundle["head_sha"],
        "tree_sha": bundle["tree_sha"],
        "manifest_relpath": bundle["manifest_relpath"],
        "manifest_blob_sha": bundle["manifest_blob_sha"],
        "environment_session_id": bundle["environment_session_id"],
        "environment_identity": json.loads(bundle["environment_identity_json"] or "{}"),
        "workspace_path": bundle["workspace_path"],
        "workspace_owner_task_id": bundle["workspace_owner_task_id"],
        "workspace_owner_activation": bundle["workspace_owner_activation"],
        "workspace_owner_run_id": bundle["workspace_owner_run_id"],
        "required_surface": bundle["required_surface"],
        "redaction_state": bundle["redaction_state"],
        "phase_b_eligible": bool(phase_b_eligible),
        "outcome_state": outcome_state,
        "invalid_reason": invalid_reason,
        "required_case_ids": sorted(required_case_ids),
        "cases": cases,
        "items": items,
    }


def _seal_bundle(bundle_id: str, *, final_state: str, reason: str | None,
                 phase_b_eligible: bool,
                 required_case_ids: list[str] | None = None,
                 extra_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    required = sorted(set(required_case_ids or ()))
    with store._connect() as db:
        bundle_row = dict(db.execute(
            "SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,),
        ).fetchone())
    # §2.4.9: a captured artifact that cannot be redacted with confidence
    # (binary screenshot/video) must block sealing outright, not seal silently
    # as if it were scanned clean (finding #2, verification adversarial lane).
    uncertain_reason = _uncertain_capture_reason(bundle_id)
    if uncertain_reason is not None:
        final_state, reason, phase_b_eligible = "blocked", uncertain_reason, False
    if final_state == "done":
        with store._connect() as db:
            rows = db.execute(
                "SELECT case_id,attempt,status FROM verification_cases "
                "WHERE bundle_id=? ORDER BY case_id,attempt", (bundle_id,),
            ).fetchall()
        latest: dict[str, tuple[int, str]] = {}
        for row in rows:
            latest[row["case_id"]] = (int(row["attempt"]), row["status"])
        if set(latest) != set(required) or any(
            status != "passed" for _attempt, status in latest.values()
        ):
            final_state = "failed"
            reason = "evidence_invariant: required verification case results are missing"
            phase_b_eligible = False
    # Autonomous-graduation eligibility is visible cross-activation history,
    # not a per-bundle fact alone: a step whose earlier activation failed or
    # was itself ineligible cannot quietly graduate just because a *later*
    # activation ran clean (finding #16, verification adversarial lane).
    with store._connect() as db:
        prior_rows = db.execute(
            "SELECT activation,state,phase_b_eligible FROM evidence_bundles "
            "WHERE instance_id=? AND step_id=? AND activation<? AND sealed_at IS NOT NULL "
            "ORDER BY activation",
            (bundle_row["instance_id"], bundle_row["step_id"], int(bundle_row["activation"])),
        ).fetchall()
    prior_failures = [
        {"activation": int(prior["activation"]), "state": prior["state"],
         "phase_b_eligible": bool(prior["phase_b_eligible"])}
        for prior in prior_rows
        if prior["state"] != "done" or not prior["phase_b_eligible"]
    ]
    if prior_failures and phase_b_eligible:
        phase_b_eligible = False
    payload = _bundle_payload(
        bundle_id, phase_b_eligible=phase_b_eligible,
        outcome_state=final_state, invalid_reason=reason,
        required_case_ids=required,
    )
    payload["prior_activation_failures"] = prior_failures
    payload.update(extra_payload or {})
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    payload["bundle_sha256"] = digest
    root = _evidence_root(
        bundle_row["instance_id"], bundle_row["step_id"], int(bundle_row["activation"]),
    )
    _copy_once(root / "bundle.json", _canonical(payload) + b"\n")
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_bundles SET state=?,bundle_sha256=?,sealed_at=?,invalid_reason=?,"
            "phase_b_eligible=? WHERE id=?",
            (final_state, digest, store._now(), reason, int(bool(phase_b_eligible)), bundle_id),
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
    run_candidate_cases: bool = True,
    drivers: dict[str, Driver] | None = None,
    child_env: dict[str, str] | None = None,
    workspace_owner_task_id: str | None = None,
    workspace_owner_activation: int | None = None,
    workspace_owner_run_id: int | None = None,
    required_surface: str | None = None,
    model_risk_surface: str | None = None,
) -> dict[str, Any]:
    """Execute candidate and protected cases, redact, and atomically seal evidence."""
    workspace = Path(workspace)
    bundle_id = _bundle_id(instance_id, step_id, activation)
    bundle = _insert_bundle(
        bundle_id=bundle_id, instance_id=instance_id, step_id=step_id,
        activation=activation, input_revision_hash=input_revision_hash,
        base_sha=base_sha, head_sha=head_sha, tree_sha=tree_sha,
        environment_session_id=environment_session_id, manifest=manifest,
        workspace_path=str(workspace.resolve()),
        workspace_owner_task_id=workspace_owner_task_id,
        workspace_owner_activation=workspace_owner_activation,
        workspace_owner_run_id=workspace_owner_run_id,
        required_surface=required_surface,
        environment_identity=environment_identity,
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
        assert_commit_binding(workspace, head_sha, tree_sha)
        if run_candidate_cases and manifest.base_sha != head_sha:
            raise CommitBindingError("candidate manifest is not bound to the candidate revision")
        if protected_manifest is not None and protected_manifest.base_sha != base_sha:
            raise CommitBindingError("protected manifest is not bound to the trusted base")
    except VerificationError as exc:
        _set_bundle_state(bundle_id, "failed", f"evidence_invariant: {exc}")
        return _seal_bundle(
            bundle_id, final_state="failed", reason=f"evidence_invariant: {exc}",
            phase_b_eligible=False,
        )
    diff_paths: list[str] = []
    if base_sha != head_sha:
        try:
            diff_output = _git(workspace, "diff", "--name-only", base_sha, head_sha)
            diff_paths = [line for line in diff_output.splitlines() if line]
        except VerificationManifestError:
            diff_paths = []
    control_plane_touched = bool(diff_paths and set(diff_paths) & control_plane_paths(manifest))
    computed_surface = classify_required_surface(
        diff_paths, model_risk_surface=model_risk_surface,
    )
    effective_surface = required_surface or computed_surface
    surface_reason = None
    if effective_surface not in _SURFACE_LEVELS:
        surface_reason = "evidence_invariant: required verification surface is missing or invalid"
    elif _SURFACE_LEVELS[effective_surface] < _SURFACE_LEVELS[computed_surface]:
        surface_reason = (
            f"evidence_invariant: supplied surface {effective_surface!r} is below "
            f"the deterministic floor {computed_surface!r} for this change"
        )
    declared_surface = profile.get("surface")
    if diff_paths and declared_surface not in _SURFACE_LEVELS:
        surface_reason = "evidence_invariant: verification profile must declare a surface"
    elif (diff_paths and _SURFACE_LEVELS.get(str(declared_surface), -1)
          < _SURFACE_LEVELS.get(effective_surface, 99)):
        surface_reason = (
            f"evidence_invariant: profile surface {declared_surface!r} is below "
            f"the deterministic floor {effective_surface!r} for this change"
        )
    if diff_paths and surface_reason is None:
        cases_for_surface = [
            case for source in filter(None, [manifest, protected_manifest])
            for case in source.document["cases"]
        ]
        has_browser = any(case["driver"] == "playwright" for case in cases_for_surface)
        has_api = any(
            case["driver"] == "playwright"
            and any(assertion["type"] == "api-status" for assertion in case["assertions"])
            for case in cases_for_surface
        )
        protected_cases = (
            protected_manifest.document["cases"] if protected_manifest is not None else []
        )
        migration_down = [
            case for case in protected_cases
            if case.get("surface_behavior") == "migration_down"
        ]
        migration_up = [
            case for case in protected_cases
            if case.get("surface_behavior") == "migration_up"
        ]
        has_rollback = any(
            down["argv"] != up["argv"]
            and _migration_tool_identity(down["argv"]) == _migration_tool_identity(up["argv"])
            for down in migration_down for up in migration_up
        )
        has_protected = bool(
            protected_manifest is not None and protected_manifest.document.get("cases")
        )
        missing_behaviors = []
        required_behaviors = {
            "browser": ("browser",), "api": ("api",), "migration": ("rollback",),
            "stricter": ("protected",),
        }[effective_surface]
        for behavior, present in (
            ("browser", has_browser), ("api", has_api), ("rollback", has_rollback),
            ("protected", has_protected),
        ):
            if behavior in required_behaviors and not present:
                missing_behaviors.append(behavior)
        if missing_behaviors:
            surface_reason = (
                "evidence_invariant: required surface behaviors are missing: "
                + ", ".join(missing_behaviors)
            )
    with store._connect() as db:
        db.execute(
            "UPDATE evidence_bundles SET required_surface=COALESCE(required_surface,?) WHERE id=?",
            (effective_surface, bundle_id),
        )
    if surface_reason is not None:
        _set_bundle_state(bundle_id, "failed", surface_reason)
        return _seal_bundle(
            bundle_id, final_state="failed", reason=surface_reason, phase_b_eligible=False,
            extra_payload={"control_plane_touched": control_plane_touched},
        )
    _set_bundle_state(bundle_id, "running")
    root = _evidence_root(instance_id, step_id, activation)
    registry = {**DRIVERS, **(drivers or {})}
    runtime = max(1, int(profile["max_runtime_seconds"]))
    retries = min(1, max(0, int(profile.get("infrastructure_retries", 0))))
    remaining_logs = max(0, min(
        int(profile["max_log_bytes"]), int(profile["max_evidence_bytes"]),
    ))
    remaining_evidence = max(0, int(profile["max_evidence_bytes"]))
    env = dict(
        _minimal_case_env(workspace, profile, bundle_id)
        if child_env is None else child_env
    )
    env.update({
        "SHIPFACTORY_INSTANCE_ID": instance_id,
        "SHIPFACTORY_HEAD_SHA": head_sha,
        "SHIPFACTORY_EVIDENCE_BUNDLE_ID": bundle_id,
        "SHIPFACTORY_CAPTURE_POLICY": json.dumps({
            "video": bool(profile.get("capture_video") and manifest.document["capture"]["video"]),
            "trace": bool(profile.get("capture_trace") and manifest.document["capture"]["trace"]),
            "har": bool(profile.get("capture_har")),
            "screenshots": manifest.document["capture"]["screenshots"],
        }, sort_keys=True),
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
    case_sources = [("candidate", manifest)] if run_candidate_cases else []
    if protected_manifest is not None:
        case_sources.append(("protected", protected_manifest))
    required_case_ids = [
        case["id"] if provenance == "candidate" else f"protected:{case['id']}"
        for provenance, source_manifest in case_sources
        for case in source_manifest.document["cases"]
    ]
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
                data = redacted[:min(remaining_logs, remaining_evidence)]
                remaining_logs -= len(data)
                remaining_evidence -= len(data)
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
                        "process_tree_supervision": result.get("process_tree_supervision"),
                    },
                )
                item_ids = [item["id"]]
                status = str(result["classification"])
                for capture in result.get("capture_containers", []):
                    try:
                        captured_item = _persist_trusted_capture_container(
                            bundle_id=bundle_id, instance_id=instance_id, head_sha=head_sha,
                            case_id=persisted_case_id, attempt=attempt,
                            kind=str(capture["kind"]), container_path=Path(capture["path"]),
                            root=root, mime_type=str(capture["mime_type"]),
                            started_at=result["started_at"], ended_at=result["ended_at"],
                            max_bytes=remaining_evidence,
                        )
                    except (KeyError, CaptureContainerError) as exc:
                        status = "failed"
                        failure_reason = f"evidence_invariant: capture: {exc}"
                        break
                    remaining_evidence -= int(captured_item["size_bytes"])
                    redacted_any |= captured_item["redaction_state"] == "redacted"
                    item_ids.append(captured_item["id"])
                _record_case(
                    bundle_id=bundle_id, case_id=persisted_case_id, attempt=attempt,
                    case=case, status=status, item_ids=item_ids,
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
        required_case_ids=required_case_ids,
        extra_payload={"control_plane_touched": control_plane_touched},
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
        workspace_path=str(Path(payload["workspace"]).resolve()),
        workspace_owner_task_id=payload.get("workspace_owner_task_id"),
        workspace_owner_activation=payload.get("workspace_owner_activation"),
        workspace_owner_run_id=payload.get("workspace_owner_run_id"),
        required_surface=payload.get("required_surface"),
    )
    return _seal_bundle(
        bundle_id, final_state="blocked", reason=reason, phase_b_eligible=False,
    )


def _load_action_manifests(
    payload: dict[str, Any],
) -> tuple[VerificationManifest | None, VerificationManifest, VerificationManifest]:
    workspace = Path(payload["workspace"])
    relpath = payload["manifest_relpath"]
    protected = load_verification_manifest(
        workspace, payload["base_sha"], relpath,
        expected_blob_sha=(
            payload.get("protected_manifest_blob_sha")
            or payload.get("manifest_blob_sha")
        ),
        verify_worktree_copy=False,
    )
    candidate = load_verification_manifest_if_present(
        workspace, payload["head_sha"], relpath,
        required_requirement_ids=set(payload.get("required_requirement_ids", [])),
        expected_blob_sha=payload.get("candidate_manifest_blob_sha"),
    )
    return candidate, protected, candidate or protected


def _manifest_failure_bundle(payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
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
        workspace_path=str(Path(payload["workspace"]).resolve()),
        workspace_owner_task_id=payload.get("workspace_owner_task_id"),
        workspace_owner_activation=payload.get("workspace_owner_activation"),
        workspace_owner_run_id=payload.get("workspace_owner_run_id"),
        required_surface=payload.get("required_surface"),
    )
    return _seal_bundle(
        bundle_id, final_state="failed",
        reason=f"evidence_invariant: {exc}", phase_b_eligible=False,
    )


def _runner_failure_bundle(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    bundle_id = _bundle_id(
        payload["instance_id"], payload["step_id"], int(payload["activation"]),
    )
    with store._connect() as db:
        existing = db.execute(
            "SELECT state FROM evidence_bundles WHERE id=?", (bundle_id,),
        ).fetchone()
    if not existing:
        try:
            _candidate, _protected, primary = _load_action_manifests(payload)
        except Exception as exc:
            return _manifest_failure_bundle(payload, exc)
        _insert_bundle(
            bundle_id=bundle_id, instance_id=payload["instance_id"],
            step_id=payload["step_id"], activation=int(payload["activation"]),
            input_revision_hash=payload["input_revision_hash"],
            base_sha=payload["base_sha"], head_sha=payload["head_sha"],
            tree_sha=payload["tree_sha"], environment_session_id=None,
            manifest=primary,
            workspace_path=str(Path(payload["workspace"]).resolve()),
            workspace_owner_task_id=payload.get("workspace_owner_task_id"),
            workspace_owner_activation=payload.get("workspace_owner_activation"),
            workspace_owner_run_id=payload.get("workspace_owner_run_id"),
            required_surface=payload.get("required_surface"),
        )
    return _seal_bundle(
        bundle_id, final_state="failed", reason=f"evidence_invariant: {reason}",
        phase_b_eligible=False,
    )


def _run_action_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Runner-child entry: execute cases after the daemon prepared the environment."""
    candidate, protected, primary = _load_action_manifests(payload)
    bundle = run_verification(
        instance_id=payload["instance_id"], step_id=payload["step_id"],
        activation=int(payload["activation"]),
        input_revision_hash=payload["input_revision_hash"], base_sha=payload["base_sha"],
        head_sha=payload["head_sha"], tree_sha=payload["tree_sha"],
        workspace=payload["workspace"], manifest=primary, profile=payload["profile"],
        environment_session_id=payload.get("resolved_environment_session_id"),
        environment_identity=payload.get("resolved_environment_identity", {}),
        protected_manifest=protected, run_candidate_cases=candidate is not None,
        workspace_owner_task_id=payload.get("workspace_owner_task_id"),
        workspace_owner_activation=payload.get("workspace_owner_activation"),
        workspace_owner_run_id=payload.get("workspace_owner_run_id"),
        required_surface=payload.get("required_surface"),
        model_risk_surface=payload.get("model_risk_surface"),
    )
    return {"status": bundle["state"], "bundle_id": bundle["id"]}


def _spawn_runner(payload: dict[str, Any]) -> dict[str, Any]:
    from shipfactory import spawn

    bundle_id = _bundle_id(
        payload["instance_id"], payload["step_id"], int(payload["activation"]),
    )
    if bundle_id in _RUNNING:
        return {"status": "pending", "bundle_id": bundle_id, "reason": "verification running"}
    action_root = store._db_path().parent / "verification-actions" / bundle_id
    payload_path = action_root / "payload.json"
    log_path = action_root / "runner.log"
    _copy_once(payload_path, _canonical(payload) + b"\n")
    action_root.mkdir(parents=True, exist_ok=True)
    run_id = store.record_run_start(
        f"verification-runner/{bundle_id}", "verification", "verification-runner", "",
        workspace_path=payload["workspace"], log_path=log_path,
        provider="shipfactory", resolved_model="non-model",
        executor_version=VERIFICATION_SCHEMA,
    )
    started_monotonic = monotonic()
    log_file = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "shipfactory.verification", "--runner", str(payload_path)],
            cwd=payload["workspace"], env=_runner_env(bundle_id), stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except Exception:
        store.record_run_end(
            run_id, -1, None, None, monotonic() - started_monotonic, "spawn_failed",
        )
        raise
    finally:
        log_file.close()
    record = {
        "proc": proc, "run_id": run_id, "payload": payload,
        "started_monotonic": started_monotonic, "token": None,
    }
    _RUNNING[bundle_id] = record
    store.record_run_spawned(run_id, proc.pid, None)
    token = spawn._capture_start_token(proc.pid, proc)
    store.record_run_spawned(run_id, proc.pid, token)
    record["token"] = token
    return {"status": "pending", "bundle_id": bundle_id, "reason": "verification running"}


def _assert_workspace_owner(payload: dict[str, Any]) -> None:
    """Cross-check a claimed workspace against its recorded task owner.

    ``assert_commit_binding`` only proves that whatever directory is passed
    in has the right HEAD/tree SHAs -- two different worktrees can share
    identical content (a clone, a stale sibling checkout) and both satisfy
    that check. When the caller declares which task's worktree this is
    supposed to be (``workspace_owner_task_id``), require it to match the
    workspace shipfactory itself actually recorded for that task's own run
    (finding #1, verification adversarial lane). Opt-in: payloads that omit
    the owner id (e.g. callers/tests with no task-run history yet) are
    unaffected.
    """
    owner_task_id = payload.get("workspace_owner_task_id")
    if not owner_task_id:
        return
    owner_activation = payload.get("workspace_owner_activation")
    owner_run_id = payload.get("workspace_owner_run_id")
    if not isinstance(owner_activation, int) or not isinstance(owner_run_id, int):
        raise CommitBindingError("exact workspace producer activation/run identity is missing")
    recorded_run = store.exact_workspace_run(
        str(owner_task_id), int(owner_run_id), int(owner_activation),
    )
    if recorded_run is None or not recorded_run.get("workspace_path"):
        raise CommitBindingError("exact workspace producer run is missing")
    recorded = recorded_run["workspace_path"]
    claimed = Path(payload["workspace"]).resolve()
    expected = Path(recorded).resolve()
    if claimed != expected:
        raise CommitBindingError(
            f"workspace {claimed} does not match the worktree recorded for "
            f"task {owner_task_id} ({expected})"
        )


def _live_app_identity(
    payload: dict[str, Any], app: dict[str, Any], env_row: dict[str, Any], workspace: Path,
    timeout: float,
) -> dict[str, Any]:
    """Bind a healthy app row to the exact environment and a live HTTP identity."""
    expected_workspace = workspace.resolve()
    fields_match = bool(
        env_row.get("base_sha") == payload["base_sha"]
        and env_row.get("candidate_sha") == payload["head_sha"]
        and Path(env_row.get("workspace_path") or "").resolve() == expected_workspace
        and Path(app.get("workspace_path") or "").resolve() == expected_workspace
        and app.get("env_session_id") == env_row.get("id")
    )
    if not fields_match:
        raise CommitBindingError("app session environment identity is stale")
    from shipfactory import spawn
    if (not app.get("pid") or not app.get("process_start_token")
            or spawn._process_start_token(int(app["pid"])) != app["process_start_token"]):
        raise CommitBindingError("app session process identity is not live")
    identity_url = urllib.parse.urljoin(
        str(app.get("app_url") or "").rstrip("/") + "/", ".shipfactory/identity",
    )
    try:
        request = urllib.request.Request(
            identity_url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        with urllib.request.urlopen(request, timeout=max(0.1, float(timeout))) as response:
            raw = response.read(65536)
            headers = response.headers
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, urllib.error.URLError) as exc:
        raise CommitBindingError(f"app session live identity probe failed: {exc}") from exc
    observed_instance = document.get("instance_id") or headers.get("X-ShipFactory-Instance-ID")
    observed_head = document.get("head_sha") or headers.get("X-ShipFactory-Head-SHA")
    if observed_instance != payload["instance_id"] or observed_head != payload["head_sha"]:
        raise CommitBindingError("app session live instance/head identity is stale")
    return {
        "instance_id": observed_instance, "head_sha": observed_head,
        "identity_url": identity_url,
    }


def run_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Prepare one action and start or probe its asynchronous runner child."""
    reap_runs()
    workspace = Path(payload["workspace"])
    try:
        _candidate, _protected, primary = _load_action_manifests(payload)
    except VerificationError as exc:
        failed = _manifest_failure_bundle(payload, exc)
        return {"status": "failed", "bundle_id": failed["id"]}
    try:
        _assert_workspace_owner(payload)
    except CommitBindingError as exc:
        failed = _manifest_failure_bundle(payload, exc)
        return {"status": "failed", "bundle_id": failed["id"]}
    prepared_id = _bundle_id(
        payload["instance_id"], payload["step_id"], int(payload["activation"]),
    )
    prepared = _insert_bundle(
        bundle_id=prepared_id, instance_id=payload["instance_id"],
        step_id=payload["step_id"], activation=int(payload["activation"]),
        input_revision_hash=payload["input_revision_hash"], base_sha=payload["base_sha"],
        head_sha=payload["head_sha"], tree_sha=payload["tree_sha"],
        environment_session_id=payload.get("environment_session_id"), manifest=primary,
        workspace_path=str(workspace.resolve()),
        workspace_owner_task_id=payload.get("workspace_owner_task_id"),
        workspace_owner_activation=payload.get("workspace_owner_activation"),
        workspace_owner_run_id=payload.get("workspace_owner_run_id"),
        required_surface=payload.get("required_surface"),
    )
    if prepared["state"] in {"done", "blocked", "failed"}:
        verify_evidence_bundle(prepared_id)
        return {"status": prepared["state"], "bundle_id": prepared_id}
    if prepared["state"] == "ready":
        _set_bundle_state(prepared_id, "preparing_environment")
    environment_id = payload.get("environment_session_id")
    environment_identity: dict[str, Any] = {}
    if payload.get("environment") == "app":
        from shipfactory import environments

        cfg = payload["environment_config"]
        app_key = (
            f"verification/{payload['instance_id']}/{payload['step_id']}/"
            f"{int(payload['activation'])}/"
            f"{hashlib.sha256((payload['base_sha'] + '|' + payload['head_sha'] + '|' + str(workspace.resolve())).encode()).hexdigest()[:20]}"
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
                bundle = _preparation_failure_bundle(payload, primary, "environment_failed")
                return {"status": "blocked", "bundle_id": bundle["id"]}
            if env_row["state"] != "ready":
                return {"status": "pending", "reason": "environment materializing"}
            app = environments.request_app_start(
                env_session_id=env_row["id"], request_key=app_key, cfg=cfg,
            )
        if app["state"] in {"crashed", "stopped"}:
            bundle = _preparation_failure_bundle(payload, primary, "environment_failed")
            return {"status": "blocked", "bundle_id": bundle["id"]}
        if app["state"] != "healthy":
            return {"status": "pending", "reason": "application starting"}
        env_row = store.env_session_row(app["env_session_id"])
        if env_row is None:
            bundle = _preparation_failure_bundle(payload, primary, "environment_identity_missing")
            return {"status": "blocked", "bundle_id": bundle["id"]}
        try:
            live_identity = _live_app_identity(
                payload, app, env_row, workspace,
                float(cfg.get("healthcheck_timeout_seconds", 2)),
            )
        except CommitBindingError as exc:
            bundle = _preparation_failure_bundle(
                payload, primary, f"environment_identity_mismatch: {exc}",
            )
            return {"status": "blocked", "bundle_id": bundle["id"]}
        environment_id = app["env_session_id"]
        environment_identity = {
            "app_session_id": app["id"], "env_session_id": app["env_session_id"],
            "app_url": app["app_url"], "port": app["port"],
            "base_sha": env_row["base_sha"], "candidate_sha": env_row["candidate_sha"],
            "workspace": str(workspace.resolve()), "live": live_identity,
        }
    runner_payload = {
        **payload,
        "resolved_environment_session_id": environment_id,
        "resolved_environment_identity": environment_identity,
    }
    return _spawn_runner(runner_payload)


def verify_evidence_bundle(bundle_id: str, *, db: Any | None = None) -> dict[str, Any]:
    """Verify a bundle by ID, including its exact sealed item membership."""
    if db is None:
        store.init_db()
        with store._connect() as fresh:
            row = fresh.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
            items = [dict(item) for item in fresh.execute(
                "SELECT * FROM evidence_items WHERE bundle_id=? ORDER BY id", (bundle_id,),
            ).fetchall()]
            cases = [dict(case) for case in fresh.execute(
                "SELECT * FROM verification_cases WHERE bundle_id=? ORDER BY case_id,attempt",
                (bundle_id,),
            ).fetchall()]
    else:
        row = db.execute("SELECT * FROM evidence_bundles WHERE id=?", (bundle_id,)).fetchone()
        items = [dict(item) for item in db.execute(
            "SELECT * FROM evidence_items WHERE bundle_id=? ORDER BY id", (bundle_id,),
        ).fetchall()]
        cases = [dict(case) for case in db.execute(
            "SELECT * FROM verification_cases WHERE bundle_id=? ORDER BY case_id,attempt",
            (bundle_id,),
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
    try:
        environment_identity = json.loads(bundle["environment_identity_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise EvidenceInvariantError("bundle environment identity is invalid") from exc
    expected_security = {
        "instance_id": bundle["instance_id"], "step_id": bundle["step_id"],
        "activation": int(bundle["activation"]),
        "input_revision_hash": bundle["input_revision_hash"],
        "base_sha": bundle["base_sha"], "head_sha": bundle["head_sha"],
        "tree_sha": bundle["tree_sha"],
        "manifest_relpath": bundle["manifest_relpath"],
        "manifest_blob_sha": bundle["manifest_blob_sha"],
        "environment_session_id": bundle["environment_session_id"],
        "environment_identity": environment_identity,
        "workspace_path": bundle["workspace_path"],
        "workspace_owner_task_id": bundle["workspace_owner_task_id"],
        "workspace_owner_activation": bundle["workspace_owner_activation"],
        "workspace_owner_run_id": bundle["workspace_owner_run_id"],
        "required_surface": bundle["required_surface"],
        "redaction_state": bundle["redaction_state"],
        "phase_b_eligible": bool(bundle["phase_b_eligible"]),
        "outcome_state": bundle["state"], "invalid_reason": bundle["invalid_reason"],
    }
    for field, expected in expected_security.items():
        if document.get(field) != expected:
            raise EvidenceInvariantError(f"sealed bundle security field mismatch: {field}")
    claimed = document.get("items")
    if not isinstance(claimed, list):
        raise EvidenceInvariantError("sealed bundle item set is invalid")
    if claimed != items:
        raise EvidenceInvariantError(
            "bundle references evidence outside its sealed set or item DB fields drifted"
        )
    if document.get("cases") != cases:
        raise EvidenceInvariantError("sealed bundle case manifest does not match the database")
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
        if item["kind"] in _CAPTURE_KINDS:
            try:
                metadata = json.loads(item["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise EvidenceInvariantError("capture item metadata is invalid") from exc
            expected_attempt = item.get("attempt")
            if expected_attempt is None:
                raise EvidenceInvariantError("capture item attempt is missing")
            expected_id = _item_id(
                bundle_id, str(item["case_id"]), int(expected_attempt), item["kind"],
            )
            if item["id"] != expected_id:
                raise EvidenceInvariantError("capture item identity is not bound to its attempt")
            header = _validate_capture_container(
                data, expected_instance_id=bundle["instance_id"],
                expected_head_sha=bundle["head_sha"], expected_bundle_id=bundle_id,
                expected_case_id=str(item["case_id"]),
                expected_kind=item["kind"], expected_attempt=int(expected_attempt),
                expected_captured_at=item["started_at"],
            )
            if header.get("redaction_state") != metadata.get("redaction_state"):
                raise EvidenceInvariantError("capture redaction identity mismatch")
    unsigned = dict(document)
    claimed_digest = unsigned.pop("bundle_sha256", None)
    actual_digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    if claimed_digest != actual_digest or bundle["bundle_sha256"] != actual_digest:
        raise EvidenceInvariantError("bundle SHA-256 mismatch")
    if bundle["state"] == "done":
        required_case_ids = document.get("required_case_ids")
        if (not isinstance(required_case_ids, list)
                or not required_case_ids
                or not all(isinstance(case_id, str) for case_id in required_case_ids)
                or len(set(required_case_ids)) != len(required_case_ids)):
            raise EvidenceInvariantError("done bundle required case set is invalid")
        latest: dict[str, tuple[int, str]] = {}
        for case in document.get("cases", []):
            case_id = case.get("case_id")
            attempt = case.get("attempt")
            status = case.get("status")
            if isinstance(case_id, str) and isinstance(attempt, int):
                if case_id not in latest or attempt > latest[case_id][0]:
                    latest[case_id] = (attempt, status)
        if (set(latest) != set(required_case_ids)
                or any(status != "passed" for _attempt, status in latest.values())):
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
    "CommitBindingError", "CaptureContainerError", "VerificationManifest",
    "validate_verification_manifest",
    "load_verification_manifest", "load_verification_manifest_if_present",
    "control_plane_paths", "assert_commit_binding", "run_verification", "run_action",
    "restore_runs", "reap_runs", "verify_evidence_bundle",
    "read_evidence_item", "build_capture_container", "classify_required_surface",
    "surface_paths_from_documents",
    "run_supervised_sidecar", "stop_supervised_sidecar",
]


def _runner_main(payload_path: str) -> int:
    try:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        _run_action_sync(payload)
    except Exception as exc:
        print(f"verification runner failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _playwright_runner_main(request_path: str) -> int:
    """Trusted browser subprocess; writes already identity-stamped containers."""
    try:
        from playwright.sync_api import sync_playwright

        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        _copy_once(Path(request["ready_path"]), b"ready\n")
        case = request["case"]
        operation_timeout_ms = int(request["operation_timeout_ms"])
        output_dir = Path(request["output_dir"])
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        capture = request.get("capture", {})
        har_path = raw_dir / "capture.har" if capture.get("har") else None
        context_options: dict[str, Any] = {
            "service_workers": "block",
            "ignore_https_errors": False,
        }
        if har_path is not None:
            context_options["record_har_path"] = str(har_path)
        if capture.get("video"):
            context_options["record_video_dir"] = str(raw_dir / "video")
        raw_items: list[tuple[str, Path, str]] = []
        classification = "passed"
        error = None
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(**context_options)
            context.set_extra_http_headers({"Cache-Control": "no-cache", "Pragma": "no-cache"})
            if capture.get("trace"):
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            video = page.video
            try:
                page.goto(
                    request["app_url"], wait_until="networkidle",
                    timeout=operation_timeout_ms,
                )
                # Every UI claim must survive a real reload in a fresh context;
                # service workers are blocked and cache is bypassed above.
                page.reload(wait_until="networkidle", timeout=operation_timeout_ms)
                for assertion in case["assertions"]:
                    if assertion["type"] == "visible":
                        page.locator(assertion["selector"]).wait_for(
                            state="visible", timeout=operation_timeout_ms,
                        )
                    elif assertion["type"] == "api-status":
                        url = urllib.parse.urljoin(request["app_url"].rstrip("/") + "/", assertion["request"].lstrip("/"))
                        response = context.request.fetch(
                            url, method="GET", fail_on_status_code=False,
                            timeout=operation_timeout_ms,
                        )
                        if response.status != int(assertion["status"]):
                            raise AssertionError(
                                f"API {assertion['request']} returned {response.status}, expected {assertion['status']}"
                            )
            except Exception as exc:
                classification, error = "failed", str(exc)
            screenshot_policy = capture.get("screenshots", "never")
            if screenshot_policy == "always" or (
                screenshot_policy == "on-failure" and classification != "passed"
            ):
                screenshot = raw_dir / "screenshot.png"
                page.screenshot(path=str(screenshot), full_page=True)
                raw_items.append(("screenshot", screenshot, "image/png"))
            if capture.get("trace"):
                trace = raw_dir / "trace.zip"
                context.tracing.stop(path=str(trace))
                raw_items.append(("trace", trace, "application/zip"))
            context.close()
            if har_path is not None and har_path.exists():
                raw_items.append(("har", har_path, "application/json"))
            if capture.get("video") and video is not None:
                try:
                    video_path = Path(video.path())
                except Exception:
                    video_path = None
                if video_path is not None and video_path.exists():
                    raw_items.append(("video", video_path, "video/webm"))
            browser.close()
        containers = []
        for kind, raw_path, mime_type in raw_items:
            raw = raw_path.read_bytes()
            redacted, redaction_state = _redact_capture_payload(kind, raw)
            container = build_capture_container(
                kind, redacted, instance_id=request["instance_id"],
                head_sha=request["head_sha"], bundle_id=request["bundle_id"],
                case_id=request["case_id"], attempt=int(request["attempt"]),
                captured_at=request["captured_at"], redaction_state=redaction_state,
            )
            container_path = output_dir / f"{kind}.sfev"
            _copy_once(container_path, container)
            containers.append({
                "kind": kind, "path": str(container_path), "mime_type": mime_type,
            })
        _copy_once(
            Path(request["result_path"]),
            _canonical({
                "classification": classification, "error": error,
                "capture_containers": containers,
            }) + b"\n",
        )
    except Exception as exc:
        print(f"playwright runner failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"--runner", "--playwright-runner"}:
        raise SystemExit("usage: python -m shipfactory.verification --runner PAYLOAD")
    raise SystemExit(
        _runner_main(sys.argv[2]) if sys.argv[1] == "--runner"
        else _playwright_runner_main(sys.argv[2])
    )
