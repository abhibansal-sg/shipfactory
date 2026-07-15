"""Factory-owned sealing and verified read-back for typed recipe artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

from shipfactory import store


DEFAULT_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024


class ArtifactValidationError(ValueError):
    """A candidate or sealed artifact failed a fail-closed check."""


class ArtifactMissing(ArtifactValidationError):
    """A required declared artifact is not sealed and readable."""


class ArtifactStale(ArtifactValidationError):
    """A required artifact was produced against an older instance base."""


class ArtifactSealError(ArtifactValidationError):
    """A recoverable daemon/filesystem failure interrupted artifact sealing."""


def artifact_id(instance_id: str, step_id: str, activation: int, kind: str) -> str:
    """Return the normative immutable artifact identity."""
    value = "|".join((instance_id, step_id, str(int(activation)), kind))
    return hashlib.sha256(value.encode()).hexdigest()


def artifact_set_hash(artifacts: Iterable[dict[str, Any]]) -> str:
    """Hash sorted ``kind:sha256`` pairs for one declared artifact set."""
    pairs = sorted(f"{item['kind']}:{item['sha256']}" for item in artifacts)
    return hashlib.sha256("|".join(pairs).encode()).hexdigest()


def artifact_is_stale(artifact: dict[str, Any], instance: dict[str, Any]) -> bool:
    """Return whether a sealed artifact is bound to an older instance base."""
    current = instance.get("current_base_sha") or instance.get("base_sha")
    if not isinstance(current, str) or not current:
        raise ValueError("instance current base_sha is required")
    return artifact.get("base_sha") != current


def _schema_version(schema: str) -> int:
    try:
        prefix, version = schema.rsplit("/v", 1)
        if not prefix.startswith("shipfactory.") or not version.isdigit() or int(version) < 1:
            raise ValueError
        return int(version)
    except (AttributeError, ValueError) as exc:
        raise ArtifactValidationError(f"invalid artifact schema {schema!r}") from exc


def _require_keys(document: dict[str, Any], schema: str, keys: set[str]) -> None:
    missing = sorted(keys - set(document))
    if missing:
        raise ArtifactValidationError(
            f"{schema} missing required fields: {', '.join(missing)}"
        )


def _string_list(document: dict[str, Any], schema: str, fields: Iterable[str]) -> None:
    for field in fields:
        value = document[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ArtifactValidationError(f"{schema} field {field} must be a string list")


def _hash_string(value: Any, lengths: tuple[int, ...]) -> bool:
    return (
        isinstance(value, str) and len(value) in lengths
        and bool(re.fullmatch(r"[0-9a-fA-F]+", value))
    )


def _validate_exploration(document: dict[str, Any]) -> None:
    schema = "shipfactory.exploration/v1"
    required = {
        "schema", "intent_sha256", "base_sha", "repo_tree_sha", "references",
        "direct_callers", "constraints", "untrusted_directives", "unknowns",
    }
    _require_keys(document, schema, required)
    if set(document) != required:
        raise ArtifactValidationError(f"{schema} has unknown fields")
    if not _hash_string(document["intent_sha256"], (64,)):
        raise ArtifactValidationError(f"{schema} field intent_sha256 must be a sha256")
    for field in ("base_sha", "repo_tree_sha"):
        if not _hash_string(document[field], (40, 64)):
            raise ArtifactValidationError(f"{schema} field {field} must be a git hash")
    if not isinstance(document["references"], list):
        raise ArtifactValidationError(f"{schema} references must be a list")
    _string_list(
        document, schema,
        ("direct_callers", "constraints", "untrusted_directives", "unknowns"),
    )
    statuses = {"existing", "proposed", "generated", "external"}
    for index, reference in enumerate(document["references"]):
        if not isinstance(reference, dict) or reference.get("status") not in statuses:
            raise ArtifactValidationError(f"{schema} reference {index} has invalid status")
        if not isinstance(reference.get("id"), str) or not isinstance(reference.get("kind"), str):
            raise ArtifactValidationError(f"{schema} reference {index} has invalid identity")
        if reference["status"] == "existing":
            _require_keys(
                reference, f"{schema} reference {index}",
                {"path", "git_blob_sha", "start_line", "end_line", "text_sha256"},
            )
            if (not isinstance(reference["git_blob_sha"], str)
                    or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", reference["git_blob_sha"])
                    or not isinstance(reference["text_sha256"], str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", reference["text_sha256"])):
                raise ArtifactValidationError(
                    f"{schema} reference {index} has invalid hashes"
                )
        if reference["status"] == "proposed":
            _require_keys(
                reference, f"{schema} reference {index}",
                {"path", "reason", "intended_parent_directory"},
            )
        for field in ("path", "reason", "intended_parent_directory"):
            if field in reference and (
                not isinstance(reference[field], str) or not reference[field]
            ):
                raise ArtifactValidationError(
                    f"{schema} reference {index} field {field} must be a nonempty string"
                )


def _validate_task_spec(document: dict[str, Any]) -> None:
    schema = "shipfactory.task-spec/v1"
    required = {
        "schema", "intent_artifact_id", "problem", "non_goals", "requirements",
        "target_files", "forbidden_paths", "risk_tags", "acceptance_cases",
        "rollback_notes", "assumptions", "clarifications",
    }
    _require_keys(document, schema, required)
    if set(document) != required:
        raise ArtifactValidationError(f"{schema} has unknown fields")
    for field in ("intent_artifact_id", "problem", "rollback_notes"):
        if not isinstance(document[field], str) or not document[field]:
            raise ArtifactValidationError(f"{schema} field {field} must be a string")
    if not _hash_string(document["intent_artifact_id"], (64,)):
        raise ArtifactValidationError(f"{schema} intent_artifact_id must be an artifact id")
    for field in (
        "non_goals", "target_files", "forbidden_paths", "risk_tags",
        "acceptance_cases", "assumptions", "clarifications",
    ):
        if not isinstance(document[field], list):
            raise ArtifactValidationError(f"{schema} field {field} must be a list")
    _string_list(
        document, schema,
        (
            "non_goals", "target_files", "forbidden_paths", "risk_tags",
            "acceptance_cases", "assumptions", "clarifications",
        ),
    )
    if not isinstance(document["requirements"], list):
        raise ArtifactValidationError(f"{schema} requirements must be a list")
    ids: set[str] = set()
    for requirement in document["requirements"]:
        if (not isinstance(requirement, dict)
                or set(requirement) != {"id", "behavior", "oracle", "risk"}
                or not all(isinstance(requirement[key], str) and requirement[key]
                           for key in requirement)):
            raise ArtifactValidationError(f"{schema} has invalid requirement")
        if not re.fullmatch(r"REQ-[1-9][0-9]*", requirement["id"]):
            raise ArtifactValidationError(f"{schema} has invalid requirement id")
        if requirement["id"] in ids:
            raise ArtifactValidationError(f"{schema} has duplicate requirement id")
        ids.add(requirement["id"])


def _plan_path(value: Any, *, label: str) -> str:
    """Return a normalized repository-relative plan path or glob."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ArtifactValidationError(f"{label} must be a repository-relative path")
    parsed = PurePosixPath(value)
    if (parsed.is_absolute() or not parsed.parts or ".." in parsed.parts
            or parsed.parts[0] == ".git"):
        raise ArtifactValidationError(f"{label} must be a repository-relative path")
    return parsed.as_posix()


def _scope_prefix(path: str) -> str:
    wildcard = min((path.find(char) for char in "*[" if char in path), default=-1)
    return path if wildcard < 0 else path[:wildcard].rstrip("/")


def _write_scopes_overlap(left: str, right: str) -> bool:
    """Conservatively identify write scopes that can name the same path."""
    if left == right or fnmatchcase(left, right) or fnmatchcase(right, left):
        return True
    left_prefix = _scope_prefix(left).rstrip("/")
    right_prefix = _scope_prefix(right).rstrip("/")
    if not left_prefix or not right_prefix:
        return True
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(right_prefix + "/")
        or right_prefix.startswith(left_prefix + "/")
    )


def _overlap_is_declared(declarations: set[str], left: str, right: str) -> bool:
    return any(
        declaration in {left, right}
        or (_write_scopes_overlap(declaration, left)
            and _write_scopes_overlap(declaration, right))
        for declaration in declarations
    )


def _has_high_risk_tag(node: dict[str, Any]) -> bool:
    normalized = {tag.strip().lower().replace("_", "-") for tag in node["risk_tags"]}
    return "control-plane" in normalized or "high-risk" in normalized


def _static_control_path(path: str) -> bool:
    lowered = path.lower()
    parts = PurePosixPath(lowered).parts
    name = parts[-1] if parts else ""
    stem = name.rsplit(".", 1)[0]
    return (
        lowered == ".shipfactory" or lowered.startswith(".shipfactory/")
        or lowered == ".github/workflows" or lowered.startswith(".github/workflows/")
        or "policy" in parts or stem in {"policy", "verification", "verify"}
        or any(part in {"deploy", "deployment", "deployments"} for part in parts)
        or stem.startswith("deploy")
    )


def _node_control_reason(
    node: dict[str, Any], runtime_control_paths: set[str] | None = None,
) -> str | None:
    kind = node["kind"].strip().lower().replace("_", "-")
    if kind in {"deploy", "deployment", "release"} or kind.startswith("deployment-"):
        return f"deployment kind {node['kind']!r}"
    for path in node["allowed_paths"]:
        if _static_control_path(path):
            return f"control path {path!r}"
        if any(_write_scopes_overlap(path, control) for control in runtime_control_paths or ()):
            return f"runtime-manifest script {path!r}"
    return None


def _validate_plan(document: dict[str, Any]) -> None:
    schema = "shipfactory.plan/v1"
    required = {
        "schema", "task_spec_sha256", "base_sha", "nodes", "integration_order",
        "shared_file_overlaps", "residual_risks",
    }
    _require_keys(document, schema, required)
    if set(document) != required:
        raise ArtifactValidationError(f"{schema} has unknown fields")
    if (not _hash_string(document["task_spec_sha256"], (64,))
            or not _hash_string(document["base_sha"], (40, 64))):
        raise ArtifactValidationError(f"{schema} revision fields must be strings")
    if not isinstance(document["nodes"], list):
        raise ArtifactValidationError(f"{schema} nodes must be a list")
    nodes: dict[str, dict[str, Any]] = {}
    required_node_keys = {
        "id", "title", "needs", "kind", "requirements", "allowed_paths",
        "expected_outputs", "test_cases", "risk_tags",
    }
    for node in document["nodes"]:
        if (not isinstance(node, dict)
                or set(node) not in (required_node_keys, required_node_keys | {"budget"})):
            raise ArtifactValidationError(f"{schema} has invalid node shape")
        if (not isinstance(node["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", node["id"])
                or node["id"] in nodes
                or not isinstance(node["title"], str) or not node["title"]
                or not isinstance(node["kind"], str) or not node["kind"]):
            raise ArtifactValidationError(f"{schema} has invalid or duplicate node id")
        for field in ("needs", "requirements", "allowed_paths", "expected_outputs", "test_cases", "risk_tags"):
            if not isinstance(node[field], list) or not all(isinstance(x, str) for x in node[field]):
                raise ArtifactValidationError(f"{schema} node {node['id']} field {field} is invalid")
        if any(not value for field in ("allowed_paths", "expected_outputs", "test_cases")
               for value in node[field]):
            raise ArtifactValidationError(
                f"{schema} node {node['id']} path/output/test fields must be nonempty strings"
            )
        node["allowed_paths"] = [
            _plan_path(
                path, label=f"{schema} node {node['id']} allowed_paths entry",
            )
            for path in node["allowed_paths"]
        ]
        if "budget" in node:
            budget = node["budget"]
            if (not isinstance(budget, dict)
                    or set(budget) != {"token_pool", "tokens"}
                    or not isinstance(budget["token_pool"], str)
                    or not budget["token_pool"]
                    or not isinstance(budget["tokens"], int)
                    or isinstance(budget["tokens"], bool)
                    or budget["tokens"] < 1):
                raise ArtifactValidationError(
                    f"{schema} node {node['id']} budget must contain a nonempty "
                    "token_pool and positive integer tokens"
                )
        nodes[node["id"]] = node
    budgeted = {node_id for node_id, node in nodes.items() if "budget" in node}
    if budgeted and budgeted != set(nodes):
        raise ArtifactValidationError(
            f"{schema} every node must declare budget when any node declares budget"
        )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ArtifactValidationError(f"{schema} dependency cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for parent in nodes[node_id]["needs"]:
            if parent not in nodes:
                raise ArtifactValidationError(f"{schema} unknown node need {parent!r}")
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)
    _string_list(
        document, schema,
        ("integration_order", "residual_risks"),
    )
    if (len(set(document["integration_order"])) != len(document["integration_order"])
            or any(node_id not in nodes for node_id in document["integration_order"])):
        raise ArtifactValidationError(f"{schema} integration_order has unknown nodes")
    _string_list(document, schema, ("shared_file_overlaps",))
    overlaps = [
        _plan_path(path, label=f"{schema} shared_file_overlaps entry")
        for path in document["shared_file_overlaps"]
    ]
    if len(set(overlaps)) != len(overlaps):
        raise ArtifactValidationError(f"{schema} shared_file_overlaps has duplicates")
    declarations = set(overlaps)
    observed: list[tuple[str, str]] = []
    ordered_nodes = list(nodes.values())
    for index, left_node in enumerate(ordered_nodes):
        for right_node in ordered_nodes[index + 1:]:
            for left_path in left_node["allowed_paths"]:
                for right_path in right_node["allowed_paths"]:
                    if not _write_scopes_overlap(left_path, right_path):
                        continue
                    observed.append((left_path, right_path))
                    if not _overlap_is_declared(declarations, left_path, right_path):
                        raise ArtifactValidationError(
                            f"{schema} undeclared write overlap {left_path!r} between "
                            f"nodes {left_node['id']!r} and {right_node['id']!r}; "
                            "declare it in shared_file_overlaps"
                        )
    for declaration in declarations:
        if not any(_overlap_is_declared({declaration}, left, right)
                   for left, right in observed):
            raise ArtifactValidationError(
                f"{schema} shared_file_overlaps entry {declaration!r} "
                "does not identify a node overlap"
            )
    for node in nodes.values():
        reason = _node_control_reason(node)
        if reason and not _has_high_risk_tag(node):
            raise ArtifactValidationError(
                f"{schema} node {node['id']} touches {reason} without a "
                "control-plane or high-risk tag"
            )


_VALIDATORS = {
    ("exploration", 1): _validate_exploration,
    ("task-spec", 1): _validate_task_spec,
    ("plan", 1): _validate_plan,
}


def _validate_document(document: Any, *, kind: str, schema: str) -> int:
    version = _schema_version(schema)
    validator = _VALIDATORS.get((kind, version))
    if validator is None:
        raise ArtifactValidationError(
            f"unsupported artifact kind/schema_version {kind!r}/v{version}"
        )
    if not isinstance(document, dict):
        raise ArtifactValidationError(f"{schema} candidate must contain a JSON object")
    if document.get("schema") != schema:
        raise ArtifactValidationError(
            f"artifact schema mismatch: expected {schema!r}, got {document.get('schema')!r}"
        )
    validator(document)
    return version


def _candidate_parts(path: str) -> tuple[str, ...]:
    parsed = PurePosixPath(path)
    if (parsed.is_absolute() or len(parsed.parts) < 2
            or parsed.parts[0] != ".shipfactory-output"
            or ".." in parsed.parts or "\\" in path):
        raise ArtifactValidationError(
            "candidate path must stay under .shipfactory-output/"
        )
    return parsed.parts


def _read_candidate(workspace: Path, candidate_path: str, max_bytes: int) -> bytes:
    """Open every candidate component with O_NOFOLLOW semantics."""
    parts = _candidate_parts(candidate_path)
    descriptors: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        descriptors.append(current)
        for part in parts[:-1]:
            try:
                current = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
                    dir_fd=current,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ArtifactValidationError(
                        f"candidate path contains a symlink: {candidate_path}"
                    ) from exc
                raise
            descriptors.append(current)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise ArtifactValidationError(
                    f"candidate path is a symlink: {candidate_path}"
                ) from exc
            if exc.errno == errno.ENOENT:
                raise ArtifactValidationError(
                    f"candidate path is missing: {candidate_path}"
                ) from exc
            raise
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactValidationError(f"candidate path is not a regular file: {candidate_path}")
        if info.st_size > int(max_bytes):
            raise ArtifactValidationError(
                f"artifact size {info.st_size} exceeds configured ceiling {int(max_bytes)}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, int(max_bytes) + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > int(max_bytes):
                raise ArtifactValidationError(
                    f"artifact size exceeds configured ceiling {int(max_bytes)}"
                )
        return b"".join(chunks)
    except ArtifactValidationError:
        raise
    except OSError as exc:
        raise ArtifactSealError(f"candidate open failed: {exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _git(workspace: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=workspace, text=True,
            stderr=subprocess.PIPE, timeout=10,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ArtifactValidationError(f"repository validation failed: {exc}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactSealError(f"repository validation unavailable: {exc}") from exc


def _git_bytes(workspace: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=workspace, stderr=subprocess.PIPE, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        raise ArtifactValidationError(f"repository validation failed: {exc}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactSealError(f"repository validation unavailable: {exc}") from exc


def _repository_path(path: Any, *, label: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise ArtifactValidationError(f"{label} must be a repository-relative path")
    parsed = PurePosixPath(path)
    if (parsed.is_absolute() or not parsed.parts or ".." in parsed.parts
            or parsed.parts[0] == ".git"):
        raise ArtifactValidationError(f"{label} must be a repository-relative path")
    return parsed.as_posix()


def _validate_exploration_repository(document: dict[str, Any], workspace: Path) -> None:
    """Bind every existing exploration citation to bytes at its declared base."""
    base_sha = document["base_sha"]
    expected_tree = _git(workspace, "rev-parse", f"{base_sha}^{{tree}}")
    if document["repo_tree_sha"].lower() != expected_tree.lower():
        raise ArtifactValidationError(
            "shipfactory.exploration/v1 repo_tree_sha does not match base_sha"
        )
    for index, reference in enumerate(document["references"]):
        status = reference["status"]
        if status not in {"existing", "proposed", "generated"}:
            continue
        path = _repository_path(
            reference.get("path"), label=f"exploration reference {index} path",
        )
        if status == "generated":
            # "generated" must not be usable to relabel a real, already
            # tracked file (e.g. quietly excusing a hand-authored test's
            # removal) with no corroboration. If the path resolves at
            # base_sha, its declared git_blob_sha must honestly match that
            # blob; a path absent at base_sha is a legitimate not-yet-built
            # output and needs no further check (finding #33).
            try:
                tracked_blob_sha = _git(workspace, "rev-parse", f"{base_sha}:{path}")
            except ArtifactValidationError:
                continue
            declared_blob_sha = reference.get("git_blob_sha")
            if (not isinstance(declared_blob_sha, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", declared_blob_sha)
                    or declared_blob_sha.lower() != tracked_blob_sha.lower()):
                raise ArtifactValidationError(
                    f"exploration reference {index} classifies tracked path {path!r} "
                    "as generated without a matching git_blob_sha"
                )
            continue
        if status != "existing":
            continue
        try:
            blob_sha = _git(workspace, "rev-parse", f"{base_sha}:{path}")
            content = _git_bytes(workspace, "show", f"{base_sha}:{path}")
        except ArtifactValidationError as exc:
            raise ArtifactValidationError(
                f"existing exploration path {path!r} is absent at base_sha"
            ) from exc
        if reference["git_blob_sha"].lower() != blob_sha.lower():
            raise ArtifactValidationError(
                f"exploration reference {index} git_blob_sha mismatch"
            )
        start = reference["start_line"]
        end = reference["end_line"]
        if (not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(end, int) or isinstance(end, bool) or end < start):
            raise ArtifactValidationError(
                f"exploration reference {index} has invalid line range"
            )
        lines = content.splitlines(keepends=True)
        if end > len(lines):
            raise ArtifactValidationError(
                f"exploration reference {index} line range exceeds blob"
            )
        cited = b"".join(lines[start - 1:end])
        text_sha = hashlib.sha256(cited).hexdigest()
        if reference["text_sha256"].lower() != text_sha:
            raise ArtifactValidationError(
                f"exploration reference {index} text_sha256 mismatch"
            )


def _repository_identity(workspace: Path, document: dict[str, Any] | None = None) -> tuple[str, str, str]:
    document = document or {}
    current_head = _git(workspace, "rev-parse", "HEAD")
    base_sha = document.get("base_sha") or current_head
    head_sha = document.get("head_sha") or current_head
    repo_tree_sha = document.get("repo_tree_sha") or _git(workspace, "rev-parse", "HEAD^{tree}")
    for label, value, suffix in (
        ("base_sha", base_sha, "^{commit}"),
        ("head_sha", head_sha, "^{commit}"),
        ("repo_tree_sha", repo_tree_sha, "^{tree}"),
    ):
        if not isinstance(value, str) or not value:
            raise ArtifactValidationError(f"repository reference {label} is missing")
        try:
            subprocess.run(
                ["git", "cat-file", "-e", value + suffix], cwd=workspace,
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.CalledProcessError as exc:
            raise ArtifactValidationError(
                f"repository reference {label}={value!r} is absent from worktree repo"
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactSealError(
                f"repository reference validation unavailable for {label}: {exc}"
            ) from exc
    return base_sha, head_sha, repo_tree_sha


def _storage_path(instance_id: str, step_id: str, activation: int, kind: str) -> Path:
    for label, value in (("instance", instance_id), ("step", step_id), ("kind", kind)):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ArtifactValidationError(f"unsafe artifact {label} path segment")
    return (
        store._db_path().parent / "artifacts" / instance_id / step_id
        / str(int(activation)) / f"{kind}.json"
    )


def _copy_once(path: Path, data: bytes) -> bytes:
    """Durably publish bytes through a same-directory atomic rename.

    Matching bytes are adopted.  Different pre-existing bytes are treated as
    an interrupted prior attempt and replaced only after the full candidate
    has been fsynced under a unique temporary name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ArtifactSealError(f"sealed artifact verification failed: {exc}") from exc
    else:
        if stat.S_ISREG(info.st_mode):
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise ArtifactSealError(
                    f"sealed artifact verification failed: {exc}"
                ) from exc
        else:
            # Rename replaces a non-regular torn target without following it.
            existing = None
    if existing is not None and hashlib.sha256(existing).digest() == hashlib.sha256(data).digest():
        return existing

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ArtifactSealError("short write while sealing artifact")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.rename(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except ArtifactSealError:
        raise
    except OSError as exc:
        raise ArtifactSealError(f"artifact publish failed: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    try:
        sealed = path.read_bytes()
    except OSError as exc:
        raise ArtifactSealError(f"sealed artifact verification failed: {exc}") from exc
    if hashlib.sha256(sealed).digest() != hashlib.sha256(data).digest():
        raise ArtifactSealError("sealed artifact differs from candidate")
    return sealed


def _row_by_id(ident: str) -> dict[str, Any] | None:
    with store._connect() as db:
        row = db.execute("SELECT * FROM artifacts WHERE id=?", (ident,)).fetchone()
    return dict(row) if row else None


def _verified_row(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("state") != "sealed" or not row.get("sealed_path"):
        raise ArtifactValidationError(f"artifact {row.get('id')} is not sealed")
    try:
        data = Path(row["sealed_path"]).read_bytes()
    except OSError as exc:
        raise ArtifactValidationError(f"sealed artifact read failed: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != row.get("sha256"):
        raise ArtifactValidationError(
            f"sealed artifact sha256 mismatch: expected {row.get('sha256')}, got {actual}"
        )
    if len(data) != row.get("size_bytes"):
        raise ArtifactValidationError("sealed artifact size mismatch")
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("sealed artifact is not valid JSON") from exc
    schema = f"shipfactory.{row['kind']}/v{int(row['schema_version'])}"
    _validate_document(document, kind=row["kind"], schema=schema)
    return row


def read_artifact(ident: str) -> dict[str, Any]:
    """Read a sealed row and verify its bytes, size, and schema again."""
    row = _row_by_id(ident)
    if row is None:
        raise ArtifactMissing(f"artifact {ident!r} is missing")
    return _verified_row(row)


def artifact_document(artifact: dict[str, Any] | str) -> dict[str, Any]:
    """Return verified JSON for a sealed artifact row or identity."""
    row = read_artifact(artifact) if isinstance(artifact, str) else _verified_row(artifact)
    try:
        document = json.loads(Path(row["sealed_path"]).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"sealed artifact document read failed: {exc}") from exc
    assert isinstance(document, dict)
    return document


def _latest_sealed(db: Any, instance_id: str, kind: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM artifacts WHERE instance_id=? AND kind=? AND state='sealed' "
        "ORDER BY activation DESC,sealed_at DESC LIMIT 1",
        (instance_id, kind),
    ).fetchone()
    return dict(row) if row else None


def _trusted_runtime_control_paths(workspace: Path, base_sha: str) -> set[str]:
    """Read executable paths from the trusted runtime manifest, when present."""
    try:
        data = subprocess.check_output(
            ["git", "show", f"{base_sha}:.shipfactory/runtime.yaml"],
            cwd=workspace, stderr=subprocess.PIPE, timeout=10,
        )
    except subprocess.CalledProcessError:
        return set()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactSealError(
            f"runtime manifest inspection unavailable: {exc}"
        ) from exc
    try:
        manifest = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ArtifactValidationError(
            "shipfactory.plan/v1 trusted runtime manifest is invalid"
        ) from exc
    if not isinstance(manifest, dict):
        raise ArtifactValidationError(
            "shipfactory.plan/v1 trusted runtime manifest is not a mapping"
        )
    def manifest_value(section: str, field: str) -> Any:
        value = manifest.get(section)
        return value.get(field) if isinstance(value, dict) else None

    argv_values = (
        manifest_value("bootstrap", "argv"),
        manifest_value("app", "start_argv"),
        manifest_value("seed", "argv"),
    )
    paths: set[str] = set()
    for argv in argv_values:
        if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
            continue
        executable = argv[0]
        # Bare commands are supplied by the trusted environment, not repository
        # scripts a plan can rewrite.
        if "/" not in executable:
            continue
        paths.add(_repository_path(executable, label="runtime manifest script"))
    return paths


def _validate_plan_budget(
    document: dict[str, Any], instance: dict[str, Any], recipe: dict[str, Any], db: Any,
) -> None:
    """Reject declared first-activation reservations that cannot be admitted."""
    declarations = [node["budget"] for node in document["nodes"] if "budget" in node]
    if not declarations:
        return
    budgets = recipe.get("budgets", {})
    if recipe.get("schema") != "shipfactory.recipe/v2":
        raise ArtifactValidationError(
            "shipfactory.plan/v1 node budgets require a v2 recipe instance"
        )
    remaining_activations = int(budgets["max_activations"]) - int(instance["activation_count"])
    if len(declarations) > remaining_activations:
        raise ArtifactValidationError(
            "shipfactory.plan/v1 budget infeasible: declared node first activations "
            f"{len(declarations)} exceed instance remaining activations "
            f"{remaining_activations}"
        )
    declared_total = sum(int(item["tokens"]) for item in declarations)
    remaining_total = int(budgets["max_tokens"]) - int(instance["tokens_charged"])
    if declared_total > remaining_total:
        raise ArtifactValidationError(
            "shipfactory.plan/v1 budget infeasible: declared node tokens "
            f"{declared_total} exceed instance remaining tokens {remaining_total}"
        )
    declared_by_pool: dict[str, int] = {}
    for item in declarations:
        pool = item["token_pool"]
        if pool not in budgets["token_pools"]:
            raise ArtifactValidationError(
                f"shipfactory.plan/v1 budget infeasible: unknown token pool {pool!r}"
            )
        declared_by_pool[pool] = declared_by_pool.get(pool, 0) + int(item["tokens"])
    for pool, declared in sorted(declared_by_pool.items()):
        charged = int(db.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM budget_charges "
            "WHERE instance_id=? AND token_pool=?",
            (instance["id"], pool),
        ).fetchone()[0])
        remaining = int(budgets["token_pools"][pool]) - charged
        if declared > remaining:
            raise ArtifactValidationError(
                f"shipfactory.plan/v1 budget infeasible: token pool {pool!r} "
                f"declares {declared} tokens but only {remaining} remain"
            )


def _validate_plan_context(
    document: dict[str, Any], instance_id: str, workspace: Path,
) -> None:
    """Validate plan coverage, risk, budget, and revision binding."""
    with store._connect() as db:
        task_spec = _latest_sealed(db, instance_id, "task-spec")
        exploration = _latest_sealed(db, instance_id, "exploration")
        instance_row = db.execute(
            "SELECT * FROM recipe_instances WHERE id=?", (instance_id,),
        ).fetchone()
        if instance_row is None:
            raise ArtifactValidationError("shipfactory.plan/v1 requires a recipe instance")
        instance = dict(instance_row)
        from shipfactory.recipes.instantiate import recipe_for_instance
        recipe = recipe_for_instance(instance).document
        _validate_plan_budget(document, instance, recipe, db)
    if task_spec is None:
        raise ArtifactValidationError("shipfactory.plan/v1 requires a sealed task-spec")
    if document["task_spec_sha256"].lower() != str(task_spec["sha256"]).lower():
        raise ArtifactValidationError("shipfactory.plan/v1 task_spec_sha256 mismatch")
    bases = {str(task_spec["base_sha"]), document["base_sha"]}
    if exploration is not None:
        bases.add(str(exploration["base_sha"]))
    if len(bases) != 1:
        raise ArtifactValidationError(
            "shipfactory.plan/v1 base_sha differs from exploration or task-spec"
        )
    runtime_control_paths = _trusted_runtime_control_paths(workspace, document["base_sha"])
    for node in document["nodes"]:
        reason = _node_control_reason(node, runtime_control_paths)
        if reason and not _has_high_risk_tag(node):
            raise ArtifactValidationError(
                f"shipfactory.plan/v1 node {node['id']} touches {reason} without a "
                "control-plane or high-risk tag"
            )
    spec = artifact_document(task_spec)
    requirement_ids = {item["id"] for item in spec["requirements"]}
    covered: set[str] = set()
    for node in document["nodes"]:
        node_requirements = set(node["requirements"])
        unknown = node_requirements - requirement_ids
        if unknown:
            raise ArtifactValidationError(
                f"shipfactory.plan/v1 node {node['id']} has unknown requirements"
            )
        covered.update(node_requirements)
        for test_case in node["test_cases"]:
            mapped = set(re.findall(r"REQ-[1-9][0-9]*", test_case))
            if not mapped or not mapped <= node_requirements:
                raise ArtifactValidationError(
                    f"shipfactory.plan/v1 test case {test_case!r} is not mapped to a requirement"
                )
    if covered != requirement_ids:
        raise ArtifactValidationError(
            "shipfactory.plan/v1 does not cover every task-spec requirement"
        )


def task_spec_has_clarifications(artifact: dict[str, Any]) -> bool:
    """Return whether a verified task-spec still contains unresolved questions."""
    if artifact.get("kind") != "task-spec":
        raise ArtifactValidationError("clarification check requires a task-spec artifact")
    document = artifact_document(artifact)
    return bool(document["clarifications"])


def seal_artifact(
    *, instance_id: str, step_id: str, activation: int, run_id: int | None,
    output: dict[str, Any], workspace: str | Path, producer: str,
    trust_domain: str | None = None,
    max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES,
) -> dict[str, Any]:
    """Seal one predetermined v2 output, preserving failures and retries."""
    store.init_db()
    kind = output["kind"]
    schema = output["schema"]
    candidate_path = output["path"]
    version = _schema_version(schema)
    ident = artifact_id(instance_id, step_id, activation, kind)
    existing = _row_by_id(ident)
    if existing and existing["state"] == "sealed":
        return _verified_row(existing)
    if existing and existing["state"] == "rejected":
        raise ArtifactValidationError(existing["validation_error"] or "artifact rejected")

    worktree = Path(workspace)
    fallback_base, fallback_head, fallback_tree = _repository_identity(worktree)
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO artifacts"
            "(id,instance_id,step_id,activation,run_id,kind,schema_version,state,"
            "candidate_path,producer,trust_domain,base_sha,head_sha,repo_tree_sha,created_at) "
            "VALUES(?,?,?,?,?,?,?,'candidate',?,?,?,?,?,?,?)",
            (
                ident, instance_id, step_id, int(activation), run_id, kind, version,
                candidate_path, producer, trust_domain, fallback_base, fallback_head,
                fallback_tree, store._now(),
            ),
        )
    try:
        data = _read_candidate(worktree, candidate_path, int(max_bytes))
        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("candidate is not valid JSON") from exc
        _validate_document(document, kind=kind, schema=schema)
        base_sha, head_sha, tree_sha = _repository_identity(worktree, document)
        if kind == "exploration":
            _validate_exploration_repository(document, worktree)
        elif kind == "plan":
            _validate_plan_context(document, instance_id, worktree)
        sealed_path = _storage_path(instance_id, step_id, activation, kind)
    except ArtifactSealError:
        # Operational failures keep the durable candidate row retryable.
        raise
    except ArtifactValidationError as exc:
        error = str(exc)[:2000] or exc.__class__.__name__
        with store._connect() as db:
            db.execute(
                "UPDATE artifacts SET state='rejected',validation_error=? "
                "WHERE id=? AND state='candidate'",
                (error, ident),
            )
        raise

    try:
        # Rows predating migration 5 bind to their first validated artifact.
        # Newly instantiated rows already have a trusted base, so this is a
        # no-op in the normal path.
        with store._connect() as db:
            db.execute(
                "UPDATE recipe_instances SET base_sha=?,updated_base_at=? "
                "WHERE id=? AND base_sha IS NULL",
                (base_sha, store._now(), instance_id),
            )
        sealed = _copy_once(sealed_path, data)
        digest = hashlib.sha256(sealed).hexdigest()
        size = len(sealed)
        with store._connect() as db:
            changed = db.execute(
                "UPDATE artifacts SET state='sealed',sealed_path=?,sha256=?,size_bytes=?,"
                "base_sha=?,head_sha=?,repo_tree_sha=?,validation_error=NULL,sealed_at=? "
                "WHERE id=? AND state='candidate'",
                (
                    str(sealed_path), digest, size, base_sha, head_sha, tree_sha,
                    store._now(), ident,
                ),
            ).rowcount
            if changed != 1:
                row = db.execute("SELECT * FROM artifacts WHERE id=?", (ident,)).fetchone()
                if row is None or row["state"] != "sealed":
                    raise ArtifactSealError("artifact seal lost candidate state")
        row = _row_by_id(ident)
        assert row is not None
        return _verified_row(row)
    except Exception as exc:
        error = str(exc)[:2000] or exc.__class__.__name__
        if isinstance(exc, ArtifactSealError):
            raise
        raise ArtifactSealError(error) from exc


def record_artifact_edge(parent_artifact_id: str, child_artifact_id: str,
                         relation: str) -> None:
    """Record one immutable derivation relation idempotently."""
    if not all(isinstance(value, str) and value for value in (
        parent_artifact_id, child_artifact_id, relation,
    )):
        raise ValueError("artifact edge values must be nonempty strings")
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO artifact_edges"
            "(parent_artifact_id,child_artifact_id,relation) VALUES(?,?,?)",
            (parent_artifact_id, child_artifact_id, relation),
        )


def input_artifacts(db: Any, instance_id: str,
                    definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve and verify the latest sealed artifacts declared as inputs."""
    declared = definition.get("inputs", [])
    if not declared:
        return []
    instance_row = db.execute(
        "SELECT * FROM recipe_instances WHERE id=?", (instance_id,),
    ).fetchone()
    if instance_row is None:
        raise ArtifactStale("artifact_stale: recipe instance has no current base")
    instance = dict(instance_row)
    resolved: list[dict[str, Any]] = []
    for item in declared:
        producer = db.execute(
            "SELECT activation FROM recipe_steps WHERE instance_id=? AND step_id=? "
            "AND state='done' ORDER BY activation DESC LIMIT 1",
            (instance_id, item["from"]),
        ).fetchone()
        row = None
        if producer is not None:
            row = db.execute(
                "SELECT * FROM artifacts WHERE instance_id=? AND step_id=? "
                "AND activation=? AND kind=? AND state='sealed'",
                (instance_id, item["from"], producer["activation"], item["kind"]),
            ).fetchone()
        if row is None:
            if item["required"]:
                raise ArtifactMissing(
                    f"artifact_missing:{item['from']}:{item['kind']}"
                )
            continue
        artifact = dict(row)
        try:
            stale = artifact_is_stale(artifact, instance)
        except ValueError as exc:
            stale = True
            stale_error = str(exc)
        else:
            stale_error = (
                f"artifact base {artifact.get('base_sha')} does not match "
                f"instance base {instance.get('base_sha')}"
            )
        if stale:
            if item["required"]:
                raise ArtifactStale(
                    f"artifact_stale:{item['from']}:{item['kind']}: {stale_error}"
                )
            continue
        try:
            resolved.append(_verified_row(artifact))
        except ArtifactValidationError as exc:
            if item["required"]:
                raise ArtifactMissing(
                    f"artifact_missing:{item['from']}:{item['kind']}: {exc}"
                ) from exc
    return resolved


def output_artifacts(db: Any, instance_id: str, step_id: str, activation: int,
                     definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve and verify every output required before v2 completion."""
    resolved: list[dict[str, Any]] = []
    for item in definition.get("outputs", []):
        row = db.execute(
            "SELECT * FROM artifacts WHERE instance_id=? AND step_id=? "
            "AND activation=? AND kind=? AND state='sealed'",
            (instance_id, step_id, int(activation), item["kind"]),
        ).fetchone()
        if row is None:
            raise ArtifactMissing(f"declared output {item['kind']} is not sealed")
        resolved.append(_verified_row(dict(row)))
    return resolved


def seal_declared_outputs_for_task(
    *, task_id: str, run_id: int, workspace: str | Path,
    max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Seal one v2 task's outputs after exit and before board completion."""
    store.init_db()
    with store._connect() as db:
        row = db.execute(
            "SELECT s.*,i.recipe_id,i.recipe_version,v.normalized_yaml "
            "FROM recipe_steps s JOIN recipe_instances i ON i.id=s.instance_id "
            "JOIN recipe_versions v ON v.id=i.recipe_id AND v.version=i.recipe_version "
            "WHERE s.kanban_task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return []
        recipe = json.loads(row["normalized_yaml"])
        if recipe.get("schema") != "shipfactory.recipe/v2":
            return []
        definition = next(item for item in recipe["steps"] if item["id"] == row["step_id"])
        inputs = input_artifacts(db, row["instance_id"], definition)
    sealed: list[dict[str, Any]] = []
    failures: list[str] = []
    for output in definition["outputs"]:
        try:
            child = seal_artifact(
                instance_id=row["instance_id"], step_id=row["step_id"],
                activation=int(row["activation"]), run_id=int(run_id), output=output,
                workspace=workspace, producer=f"run:{int(run_id)}",
                max_bytes=int(max_bytes),
            )
            sealed.append(child)
            for parent in inputs:
                record_artifact_edge(parent["id"], child["id"], "derived-from")
        except ArtifactValidationError as exc:
            failures.append(f"{output['kind']}: {exc}")
    if failures:
        raise ArtifactValidationError("; ".join(failures))
    return sealed


__all__ = [
    "ArtifactMissing", "ArtifactSealError", "ArtifactStale",
    "ArtifactValidationError", "DEFAULT_ARTIFACT_MAX_BYTES",
    "artifact_document", "artifact_id", "artifact_is_stale", "artifact_set_hash", "input_artifacts",
    "output_artifacts", "read_artifact", "record_artifact_edge", "seal_artifact",
    "seal_declared_outputs_for_task", "task_spec_has_clarifications",
]
