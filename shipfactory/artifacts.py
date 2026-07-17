"""Factory-owned sealing and verified read-back for typed recipe artifacts."""

from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

from shipfactory import store
from shipfactory.artifact_contracts import (
    EXPLORATION_EXISTING_REFERENCE_KEYS,
    EXPLORATION_PROPOSED_REFERENCE_KEYS,
    EXPLORATION_REFERENCE_BASE_KEYS,
    PLAN_NODE_KEYS,
    REQUIRED_TOP_LEVEL,
    REVIEW_STORY_CHANGE_KEYS,
    TASK_SPEC_REQUIREMENT_KEYS,
    find_unresolved_output_placeholder,
)


DEFAULT_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024
_FACTORY_GIT_NAME = "Abhinav Bansal"
_FACTORY_GIT_EMAIL = "abhibansal-sg@users.noreply.github.com"
_FACTORY_COMMIT_PREFIX = "ShipFactory: canonical build"
_FACTORY_COMMIT_INTENT_KIND = "factory_commit_ref_update"
_FACTORY_COMMIT_INTENT_PREPARED = "prepared"


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


def _substantive_string_list(
    document: dict[str, Any], schema: str, fields: Iterable[str],
) -> None:
    _string_list(document, schema, fields)
    for field in fields:
        if any(not item.strip() for item in document[field]):
            raise ArtifactValidationError(
                f"{schema} field {field} entries must contain non-whitespace text"
            )


def _hash_string(value: Any, lengths: tuple[int, ...]) -> bool:
    return (
        isinstance(value, str) and len(value) in lengths
        and bool(re.fullmatch(r"[0-9a-fA-F]+", value))
    )


def _validate_exploration(document: dict[str, Any]) -> None:
    schema = "shipfactory.exploration/v1"
    required = REQUIRED_TOP_LEVEL[schema]
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
        if (not isinstance(reference, dict)
                or not EXPLORATION_REFERENCE_BASE_KEYS.issubset(reference)
                or reference.get("status") not in statuses):
            raise ArtifactValidationError(f"{schema} reference {index} has invalid status")
        if not isinstance(reference.get("id"), str) or not isinstance(reference.get("kind"), str):
            raise ArtifactValidationError(f"{schema} reference {index} has invalid identity")
        if reference["status"] == "existing":
            _require_keys(
                reference, f"{schema} reference {index}",
                EXPLORATION_EXISTING_REFERENCE_KEYS,
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
                EXPLORATION_PROPOSED_REFERENCE_KEYS,
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
    required = REQUIRED_TOP_LEVEL[schema]
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
                or set(requirement) != TASK_SPEC_REQUIREMENT_KEYS
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
    required = REQUIRED_TOP_LEVEL[schema]
    _require_keys(document, schema, required)
    if set(document) != required:
        raise ArtifactValidationError(f"{schema} has unknown fields")
    if (not _hash_string(document["task_spec_sha256"], (64,))
            or not _hash_string(document["base_sha"], (40, 64))):
        raise ArtifactValidationError(f"{schema} revision fields must be strings")
    if not isinstance(document["nodes"], list):
        raise ArtifactValidationError(f"{schema} nodes must be a list")
    nodes: dict[str, dict[str, Any]] = {}
    required_node_keys = PLAN_NODE_KEYS
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
    _string_list(document, schema, ("integration_order",))
    _substantive_string_list(document, schema, ("residual_risks",))
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


def _validate_change_set(document: dict[str, Any]) -> None:
    """Validate the strict, model-untrusted change-set document shape."""
    schema = "shipfactory.change-set/v1"
    required = {
        "schema", "base_sha", "head_sha", "tree_sha", "commits",
        "changed_paths", "allowed_paths", "dirty_tree",
    }
    _require_keys(document, schema, required)
    if set(document) != required:
        raise ArtifactValidationError(f"{schema} has unknown fields")
    for field in ("base_sha", "head_sha", "tree_sha"):
        if not _hash_string(document[field], (40, 64)):
            raise ArtifactValidationError(f"{schema} field {field} must be a git hash")
    _string_list(document, schema, ("commits", "allowed_paths"))
    if (not document["commits"]
            or any(not _hash_string(value, (40, 64)) for value in document["commits"])):
        raise ArtifactValidationError(f"{schema} commits must be an ordered git-hash list")
    if not document["allowed_paths"]:
        raise ArtifactValidationError(f"{schema} allowed_paths must not be empty")
    normalized_allowed = [
        _plan_path(value, label=f"{schema} allowed_paths entry")
        for value in document["allowed_paths"]
    ]
    if normalized_allowed != document["allowed_paths"] or len(set(normalized_allowed)) != len(
        normalized_allowed
    ):
        raise ArtifactValidationError(f"{schema} allowed_paths must be normalized and unique")
    if document["dirty_tree"] is not False:
        raise ArtifactValidationError(f"{schema} dirty_tree must be false")
    changes = document["changed_paths"]
    if not isinstance(changes, list) or not changes:
        raise ArtifactValidationError(f"{schema} changed_paths must be a nonempty list")
    seen: set[tuple[str | None, str]] = set()
    for index, item in enumerate(changes):
        if (not isinstance(item, dict)
                or set(item) != {"status", "path", "previous_path", "blob_sha"}):
            raise ArtifactValidationError(f"{schema} changed path {index} has invalid shape")
        status_value = item["status"]
        if (not isinstance(status_value, str)
                or not re.fullmatch(r"(?:[AMDTUXB]|[RC](?:[0-9]{1,3}))", status_value)):
            raise ArtifactValidationError(f"{schema} changed path {index} has invalid status")
        path = _repository_path(item["path"], label=f"{schema} changed path {index}")
        previous = item["previous_path"]
        if status_value.startswith(("R", "C")):
            previous = _repository_path(
                previous, label=f"{schema} changed path {index} previous_path",
            )
            if previous == path:
                raise ArtifactValidationError(
                    f"{schema} changed path {index} rename/copy identity is unchanged"
                )
        elif previous is not None:
            raise ArtifactValidationError(
                f"{schema} changed path {index} previous_path is only valid for rename/copy"
            )
        blob_sha = item["blob_sha"]
        if status_value == "D":
            if blob_sha is not None:
                raise ArtifactValidationError(
                    f"{schema} deleted path {index} must have null blob_sha"
                )
        elif not _hash_string(blob_sha, (40, 64)):
            raise ArtifactValidationError(
                f"{schema} changed path {index} must have a resulting blob_sha"
            )
        identity = (previous, path)
        if identity in seen:
            raise ArtifactValidationError(f"{schema} changed_paths contains a duplicate")
        seen.add(identity)


def _validate_review_story(document: dict[str, Any]) -> None:
    schema = "shipfactory.review-story/v1"
    required = REQUIRED_TOP_LEVEL[schema]
    _require_keys(document, schema, required)
    if set(document) != required:
        raise ArtifactValidationError(f"{schema} has unknown fields")
    for field in (
        "instance_id", "revision_hash", "task_spec_sha256", "plan_sha256",
        "change_set_sha256", "evidence_bundle_sha256", "headline",
    ):
        if not isinstance(document[field], str) or not document[field]:
            raise ArtifactValidationError(f"{schema} field {field} must be a nonempty string")
    for field in (
        "revision_hash", "task_spec_sha256", "plan_sha256", "change_set_sha256",
        "evidence_bundle_sha256",
    ):
        if not _hash_string(document[field], (64,)):
            raise ArtifactValidationError(f"{schema} field {field} must be a sha256")
    if not isinstance(document["changes"], list) or not document["changes"]:
        raise ArtifactValidationError(f"{schema} changes must be a nonempty list")
    change_keys = REVIEW_STORY_CHANGE_KEYS
    for index, change in enumerate(document["changes"]):
        if not isinstance(change, dict) or set(change) != change_keys:
            raise ArtifactValidationError(f"{schema} change {index} has invalid shape")
        if (not isinstance(change["importance"], int)
                or isinstance(change["importance"], bool)
                or change["importance"] < 1):
            raise ArtifactValidationError(f"{schema} change {index} importance is invalid")
        for field in ("requirement_ids", "files", "evidence_case_ids"):
            if (not isinstance(change[field], list)
                    or not all(isinstance(item, str) and item for item in change[field])):
                raise ArtifactValidationError(
                    f"{schema} change {index} field {field} must be a nonempty string list"
                )
        if not change["files"]:
            raise ArtifactValidationError(f"{schema} change {index} must name a file")
        for field in ("why", "risk"):
            if not isinstance(change[field], str) or not change[field]:
                raise ArtifactValidationError(
                    f"{schema} change {index} field {field} must be nonempty"
                )
        for path in change["files"]:
            _repository_path(path, label=f"{schema} change {index} file")
    _string_list(document, schema, ("generated_or_mechanical_files",))
    _substantive_string_list(document, schema, ("residual_risks",))
    for path in document["generated_or_mechanical_files"]:
        _repository_path(path, label=f"{schema} generated file")
    if not isinstance(document["not_changed"], list):
        raise ArtifactValidationError(f"{schema} not_changed must be a list")
    for index, item in enumerate(document["not_changed"]):
        if not isinstance(item, dict):
            raise ArtifactValidationError(
                f"{schema} not_changed {index} must be an explicit not-implemented entry"
            )
        ids = item.get("requirement_ids")
        if ids is None and isinstance(item.get("requirement_id"), str):
            ids = [item["requirement_id"]]
        reason = item.get("reason") or item.get("why")
        disposition = item.get("disposition") or item.get("status")
        explicit = item.get("not_implemented") is True or disposition in {
            "not_implemented", "not-implemented",
        }
        if (not isinstance(ids, list) or not ids
                or not all(isinstance(value, str) and value for value in ids)
                or not isinstance(reason, str) or not reason or not explicit):
            raise ArtifactValidationError(
                f"{schema} not_changed {index} is not an explicit not-implemented entry"
            )


_VALIDATORS = {
    ("exploration", 1): _validate_exploration,
    ("task-spec", 1): _validate_task_spec,
    ("plan", 1): _validate_plan,
    ("change-set", 1): _validate_change_set,
    ("review-story", 1): _validate_review_story,
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
    if find_unresolved_output_placeholder(document) is not None:
        raise ArtifactValidationError(
            f"{schema} candidate contains an unresolved Factory output-contract placeholder"
        )
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


# Test-only seam: a callable invoked exactly once, immediately after the
# FIRST chunk of a candidate read, before any subsequent os.read() call on
# the same fd. Production code never sets this; it exists so a test can
# deterministically land a real, synchronized write in the middle of a
# read — instead of an unsynchronized background thread that may or may
# not overlap the read window by luck (finding #2).
_CANDIDATE_READ_HOOK: Any = None


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
        first_chunk = True
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
            if first_chunk and _CANDIDATE_READ_HOOK is not None:
                _CANDIDATE_READ_HOOK()
            first_chunk = False
        # TOCTOU guard: this fd stays open on the same inode for the whole
        # read, so a concurrent in-place write to the candidate (the real
        # attack this seam and finding #2 are about) changes this inode's
        # mtime/ctime even when the byte length happens to match. A read
        # that spanned such a write must never be sealed as if it were one
        # coherent snapshot — reject it explicitly rather than silently
        # accepting whatever bytes landed in the buffer.
        after = os.fstat(fd)
        if (after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (
            info.st_mtime_ns, info.st_ctime_ns, info.st_size,
        ):
            raise ArtifactValidationError(
                f"candidate path was modified while being read (torn read): {candidate_path}"
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


def _tree_entry(workspace: Path, ref: str, path: str) -> tuple[str, str]:
    """Return ``(mode, object_sha)`` for one exact tree path."""
    raw = _git_bytes(workspace, "ls-tree", "-z", "--full-tree", ref, "--", path)
    entries = [entry for entry in raw.split(b"\0") if entry]
    if len(entries) != 1:
        raise ArtifactValidationError(
            f"change-set path {path!r} does not resolve to one resulting tree entry"
        )
    try:
        metadata, encoded_path = entries[0].split(b"\t", 1)
        mode, object_type, object_sha = metadata.decode("ascii").split(" ", 2)
        actual_path = encoded_path.decode("utf-8", errors="surrogateescape")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ArtifactValidationError(
            f"change-set tree entry for {path!r} is malformed"
        ) from exc
    if actual_path != path or object_type != "blob":
        raise ArtifactValidationError(
            f"change-set path {path!r} is not an exact resulting blob"
        )
    return mode, object_sha


def _path_is_allowed(path: str, allowed_paths: Iterable[str]) -> bool:
    for allowed in allowed_paths:
        if fnmatchcase(path, allowed):
            return True
        if not any(character in allowed for character in "*?["):
            prefix = allowed.rstrip("/")
            if allowed.endswith("/") and path.startswith(prefix + "/"):
                return True
    return False


def _assert_change_set_not_forbidden(
    document: dict[str, Any], forbidden_paths: Iterable[str],
) -> None:
    """Apply task-spec exclusions to the final canonical rename-aware diff."""
    forbidden = list(forbidden_paths)
    if not forbidden:
        return
    for change in document["changed_paths"]:
        paths = [change["path"]]
        if change.get("previous_path") is not None:
            paths.append(change["previous_path"])
        for path in paths:
            if any(_path_is_allowed(path, [pattern]) for pattern in forbidden):
                raise ArtifactValidationError(
                    f"change-set path {path!r} is forbidden by the task specification"
                )


def _change_set_dirty(workspace: Path) -> bool:
    """Detect source dirt while excluding Factory's out-of-band output root."""
    raw = _git_bytes(
        workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        "--", ".", ":(exclude).shipfactory-output",
    )
    return bool(raw)


def _changed_worktree_paths(workspace: Path) -> list[str]:
    """Return every tracked or untracked source path in a dirty worktree."""
    conflicted = _git_bytes(workspace, "diff", "--name-only", "-z", "--diff-filter=U")
    if conflicted:
        raise ArtifactValidationError("change-set worktree contains unresolved conflicts")
    tracked = _git_bytes(workspace, "diff", "--name-only", "-z", "HEAD", "--", ".")
    untracked = _git_bytes(
        workspace, "ls-files", "--others", "--exclude-standard", "-z", "--", ".",
    )
    paths: list[str] = []
    for encoded in [item for item in (tracked + untracked).split(b"\0") if item]:
        path = _repository_path(
            encoded.decode("utf-8", errors="surrogateescape"),
            label="change-set dirty path",
        )
        if path == ".shipfactory-output" or path.startswith(".shipfactory-output/"):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def _assert_worktree_path_not_symlink(workspace: Path, path: str) -> None:
    """Reject a symlink at any existing component of one candidate path."""
    current = workspace
    for component in PurePosixPath(path).parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ArtifactSealError(f"change-set path {path!r} cannot be inspected: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactValidationError(f"change-set path {path!r} traverses a symlink")


def _factory_commit_message(instance_id: str, step_id: str, activation: int) -> str:
    return f"{_FACTORY_COMMIT_PREFIX} {instance_id}/{step_id}/{int(activation)}"


def _factory_commit_timestamp(started_at: str) -> str:
    """Return one stable Git timestamp derived from the durable run identity."""
    try:
        parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        seconds = int(parsed.astimezone(timezone.utc).timestamp())
    except (OverflowError, TypeError, ValueError) as exc:
        raise ArtifactValidationError("change-set run has no valid durable start timestamp") from exc
    return f"@{seconds} +0000"


def _factory_commit_logical_key(instance_id: str, step_id: str, activation: int) -> str:
    identity = f"factory-commit|{instance_id}|{step_id}|{int(activation)}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _factory_commit_action_key(logical_key: str) -> str:
    return hashlib.sha256(f"{logical_key}|attempt|1".encode()).hexdigest()


def _factory_commit_intent_row(
    *, instance_id: str, step_id: str, activation: int,
) -> dict[str, Any] | None:
    """Load and validate the non-claimable journal envelope for one build."""
    logical_key = _factory_commit_logical_key(instance_id, step_id, activation)
    with store._connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM action_intents WHERE logical_key=? ORDER BY attempt",
            (logical_key,),
        ).fetchall()]
    if not rows:
        return None
    row = rows[0]
    if (len(rows) != 1 or row["key"] != _factory_commit_action_key(logical_key)
            or int(row["attempt"]) != 1
            or row["kind"] != _FACTORY_COMMIT_INTENT_KIND
            or row["state"] not in {_FACTORY_COMMIT_INTENT_PREPARED, "succeeded"}
            or row["instance_id"] != instance_id or row["step_id"] != step_id
            or row["activation"] is None or int(row["activation"]) != int(activation)
            or row["lease_owner"] is not None or row["lease_until"] is not None):
        raise ArtifactValidationError("change-set Factory commit intent envelope mismatched")
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("change-set Factory commit intent payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ArtifactValidationError("change-set Factory commit intent payload is invalid")
    row["payload"] = payload
    return row


def _persist_factory_commit_intent(payload: dict[str, Any]) -> dict[str, Any]:
    """Durably authenticate an exact commit before its ref can move."""
    logical_key = _factory_commit_logical_key(
        payload["instance_id"], payload["step_id"], int(payload["activation"]),
    )
    key = _factory_commit_action_key(logical_key)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM action_intents WHERE logical_key=? ORDER BY attempt",
            (logical_key,),
        ).fetchall()
        if not existing:
            db.execute(
                "INSERT INTO action_intents"
                "(key,logical_key,attempt,instance_id,step_id,activation,kind,payload_json,"
                "state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key, logical_key, 1, payload["instance_id"], payload["step_id"],
                    int(payload["activation"]), _FACTORY_COMMIT_INTENT_KIND, encoded,
                    _FACTORY_COMMIT_INTENT_PREPARED, store._now(),
                ),
            )
        elif len(existing) != 1 or existing[0]["payload_json"] != encoded:
            raise ArtifactValidationError("change-set Factory commit intent payload mismatched")
    row = _factory_commit_intent_row(
        instance_id=payload["instance_id"], step_id=payload["step_id"],
        activation=int(payload["activation"]),
    )
    if row is None or row["payload"] != payload:
        raise ArtifactValidationError("change-set Factory commit intent was not durable")
    return row


def _write_factory_commit_object(
    workspace: Path, *, tree_sha: str, base_sha: str, message: str, timestamp: str,
) -> str:
    """Create the deterministic commit object without changing any Git ref."""
    git_config = ["-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false"]
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": _FACTORY_GIT_NAME,
        "GIT_AUTHOR_EMAIL": _FACTORY_GIT_EMAIL,
        "GIT_COMMITTER_NAME": _FACTORY_GIT_NAME,
        "GIT_COMMITTER_EMAIL": _FACTORY_GIT_EMAIL,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    try:
        completed = subprocess.run(
            ["git", *git_config, "commit-tree", tree_sha, "-p", base_sha, "-m", message],
            cwd=workspace, env=commit_env, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=20,
        )
    except subprocess.CalledProcessError as exc:
        error = exc.stderr.decode("utf-8", errors="replace")[:1000]
        raise ArtifactSealError(f"Factory change-set commit object failed: {error}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactSealError(f"Factory change-set commit object unavailable: {exc}") from exc
    expected = completed.stdout.decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", expected):
        raise ArtifactSealError("Factory change-set commit object returned an invalid SHA")
    return expected


def _expected_factory_commit_payload(
    *, instance_id: str, step_id: str, activation: int, run_id: int,
    workspace: Path, base_sha: str, tree_sha: str, expected_commit_sha: str,
    message: str, timestamp: str,
) -> dict[str, Any]:
    return {
        "schema": "shipfactory.factory-commit-intent/v1",
        "instance_id": instance_id,
        "step_id": step_id,
        "activation": int(activation),
        "run_id": int(run_id),
        "workspace": str(workspace.resolve()),
        "base_sha": base_sha,
        "tree_sha": tree_sha,
        "expected_commit_sha": expected_commit_sha,
        "message": message,
        "author_name": _FACTORY_GIT_NAME,
        "author_email": _FACTORY_GIT_EMAIL,
        "committer_name": _FACTORY_GIT_NAME,
        "committer_email": _FACTORY_GIT_EMAIL,
        "author_timestamp": timestamp,
        "committer_timestamp": timestamp,
    }


def _verify_factory_commit_payload(
    workspace: Path, *, payload: dict[str, Any], instance_id: str, step_id: str,
    activation: int, run_id: int, base_sha: str, tree_sha: str, message: str,
    timestamp: str,
) -> str:
    """Bind a journaled SHA to the complete current finalization context."""
    expected_sha = payload.get("expected_commit_sha")
    if not isinstance(expected_sha, str):
        raise ArtifactValidationError("change-set Factory commit intent payload mismatched")
    expected = _expected_factory_commit_payload(
        instance_id=instance_id, step_id=step_id, activation=activation, run_id=run_id,
        workspace=workspace, base_sha=base_sha, tree_sha=tree_sha,
        expected_commit_sha=expected_sha, message=message, timestamp=timestamp,
    )
    if payload != expected:
        raise ArtifactValidationError("change-set Factory commit intent payload mismatched")
    actual = _write_factory_commit_object(
        workspace, tree_sha=tree_sha, base_sha=base_sha,
        message=message, timestamp=timestamp,
    )
    if actual != expected_sha:
        raise ArtifactValidationError("change-set Factory commit intent SHA mismatched")
    return expected_sha


def _update_factory_commit_ref(workspace: Path, *, expected_sha: str, base_sha: str) -> None:
    """Atomically publish exactly the authenticated commit from exactly base."""
    try:
        subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "update-ref", "HEAD",
             expected_sha, base_sha],
            cwd=workspace, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20,
        )
    except subprocess.CalledProcessError as exc:
        error = exc.stderr.decode("utf-8", errors="replace")[:1000]
        raise ArtifactValidationError(
            f"change-set Factory commit ref compare-and-swap failed: {error}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactSealError(f"Factory change-set ref update unavailable: {exc}") from exc


def _mark_factory_commit_intent_succeeded(row: dict[str, Any], expected_sha: str) -> None:
    """Audit the terminal ref state without exposing the intent to the action runner."""
    if row["state"] == "succeeded":
        return
    now = store._now()
    result = json.dumps(
        {"expected_commit_sha": expected_sha, "probe": "head_matches_authenticated_intent"},
        sort_keys=True, separators=(",", ":"),
    )
    with store._connect() as db:
        changed = db.execute(
            "UPDATE action_intents SET state='succeeded',finished_at=?,result_json=?,"
            "last_error=NULL WHERE key=? AND state=? AND kind=?",
            (
                now, result, row["key"], _FACTORY_COMMIT_INTENT_PREPARED,
                _FACTORY_COMMIT_INTENT_KIND,
            ),
        ).rowcount
        if changed != 1:
            raise ArtifactValidationError("change-set Factory commit intent terminal state lost")


def _write_factory_candidate(workspace: Path, candidate_path: str, data: bytes) -> None:
    """Atomically publish Factory-generated bytes below the output root."""
    parts = _candidate_parts(candidate_path)
    output_root = workspace / parts[0]
    try:
        info = output_root.lstat()
    except FileNotFoundError:
        output_root.mkdir(mode=0o700)
    else:
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ArtifactValidationError(".shipfactory-output must be a real directory")
    target = workspace.joinpath(*parts)
    if output_root.resolve() not in (target.parent.resolve(), *target.parent.resolve().parents):
        raise ArtifactValidationError("Factory change-set output escapes its output root")
    _copy_once(target, data)


def finalize_change_set_for_task(
    *, instance_id: str, step_id: str, activation: int, run_id: int,
    workspace: str | Path, output: dict[str, Any], inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the one Factory-owned build commit and canonical manifest.

    Workers only edit approved source paths. On their successful exit the
    trusted reaper validates the dirty tree, creates a hook-disabled commit,
    and derives the manifest from Git facts. A retry after the commit but
    before manifest publication recognizes only this exact Factory commit.
    """
    worktree = Path(workspace)
    with store._connect() as db:
        ownership = db.execute(
            "SELECT r.task_id,r.recipe_activation,r.workspace_path,r.started_at,s.kanban_task_id "
            "FROM runs r JOIN recipe_steps s ON s.instance_id=? AND s.step_id=? "
            "AND s.activation=? WHERE r.id=?",
            (instance_id, step_id, int(activation), int(run_id)),
        ).fetchone()
    if (ownership is None or not ownership["kanban_task_id"]
            or str(ownership["task_id"]) != str(ownership["kanban_task_id"])
            or ownership["recipe_activation"] is None
            or int(ownership["recipe_activation"]) != int(activation)
            or not ownership["workspace_path"]
            or Path(ownership["workspace_path"]).resolve() != worktree.resolve()):
        raise ArtifactValidationError(
            "change-set finalization is not bound to the exact assigned run/worktree"
        )
    top = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve()
    if top != worktree.resolve():
        raise ArtifactValidationError("change-set workspace is not its isolated git worktree root")
    plans = [item for item in inputs if item.get("kind") == "plan"]
    if len(plans) != 1:
        raise ArtifactValidationError("change-set build requires exactly one declared plan input")
    plan = artifact_document(plans[0])
    allowed_paths: list[str] = []
    for node in plan["nodes"]:
        for path in node["allowed_paths"]:
            if path not in allowed_paths:
                allowed_paths.append(path)
    forbidden_paths: list[str] = []
    for item in inputs:
        if item.get("kind") == "task-spec":
            forbidden_paths.extend(artifact_document(item).get("forbidden_paths", []))

    base = _git(worktree, "rev-parse", f"{plan['base_sha']}^{{commit}}")
    head = _git(worktree, "rev-parse", "HEAD^{commit}")
    message = _factory_commit_message(instance_id, step_id, activation)
    timestamp = _factory_commit_timestamp(ownership["started_at"])
    intent = _factory_commit_intent_row(
        instance_id=instance_id, step_id=step_id, activation=activation,
    )
    dirty_paths = _changed_worktree_paths(worktree)
    if head != base:
        if dirty_paths:
            raise ArtifactValidationError("change-set Factory commit has post-commit source mutations")
        if intent is None:
            raise ArtifactValidationError(
                "change-set HEAD has no exact durable Factory commit intent"
            )
        tree = _git(worktree, "rev-parse", f"{head}^{{tree}}")
        expected_sha = _verify_factory_commit_payload(
            worktree, payload=intent["payload"], instance_id=instance_id,
            step_id=step_id, activation=activation, run_id=run_id, base_sha=base,
            tree_sha=tree, message=message, timestamp=timestamp,
        )
        if head != expected_sha:
            raise ArtifactValidationError(
                "change-set HEAD does not match the exact durable Factory commit intent"
            )
        _mark_factory_commit_intent_succeeded(intent, expected_sha)
    else:
        if intent is not None and intent["state"] == "succeeded":
            raise ArtifactValidationError(
                "change-set HEAD moved away from its completed Factory commit intent"
            )
        if not dirty_paths:
            raise ArtifactValidationError("change-set build produced no source changes")
        for path in dirty_paths:
            if not _path_is_allowed(path, allowed_paths):
                raise ArtifactValidationError(
                    f"change-set dirty path {path!r} is outside the approved plan"
                )
            if any(_path_is_allowed(path, [forbidden]) for forbidden in forbidden_paths):
                raise ArtifactValidationError(
                    f"change-set dirty path {path!r} is forbidden by the task specification"
                )
            _assert_worktree_path_not_symlink(worktree, path)
        git_config = [
            "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
        ]
        try:
            subprocess.run(
                ["git", *git_config, "add", "--all", "--", ".",
                 ":(exclude).shipfactory-output"],
                cwd=worktree, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20,
            )
            staged_paths = [
                _repository_path(
                    value.decode("utf-8", errors="surrogateescape"),
                    label="change-set staged path",
                )
                for value in _git_bytes(
                    # Compare the same source/result path set validated before
                    # staging.  Rename-aware --name-only collapses a rename to
                    # its destination and would falsely reject the old path;
                    # --no-renames preserves the deletion and addition here.
                    worktree, "diff", "--cached", "--name-only", "--no-renames", "-z",
                ).split(b"\0") if value
            ]
            if staged_paths != dirty_paths and set(staged_paths) != set(dirty_paths):
                raise ArtifactValidationError("change-set staged paths differ from validated dirt")
            tree = _git(worktree, "write-tree")
        except subprocess.CalledProcessError as exc:
            error = exc.stderr.decode("utf-8", errors="replace")[:1000]
            raise ArtifactSealError(f"Factory change-set staging failed: {error}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactSealError(f"Factory change-set staging unavailable: {exc}") from exc

        if intent is None:
            expected_sha = _write_factory_commit_object(
                worktree, tree_sha=tree, base_sha=base,
                message=message, timestamp=timestamp,
            )
            payload = _expected_factory_commit_payload(
                instance_id=instance_id, step_id=step_id, activation=activation,
                run_id=run_id, workspace=worktree, base_sha=base, tree_sha=tree,
                expected_commit_sha=expected_sha, message=message, timestamp=timestamp,
            )
            intent = _persist_factory_commit_intent(payload)
        else:
            expected_sha = _verify_factory_commit_payload(
                worktree, payload=intent["payload"], instance_id=instance_id,
                step_id=step_id, activation=activation, run_id=run_id, base_sha=base,
                tree_sha=tree, message=message, timestamp=timestamp,
            )
        _update_factory_commit_ref(worktree, expected_sha=expected_sha, base_sha=base)
        head = _git(worktree, "rev-parse", "HEAD^{commit}")
        if head != expected_sha:
            raise ArtifactSealError("Factory change-set ref update did not persist")
        _mark_factory_commit_intent_succeeded(intent, expected_sha)
        if _change_set_dirty(worktree):
            raise ArtifactValidationError("change-set Factory commit has post-commit source mutations")

    document = rederive_change_set(
        worktree, base_sha=base, allowed_paths=allowed_paths,
    )
    # Public commit metadata is never authority, and even an authenticated
    # crash-retry intent must satisfy the current sealed task-spec exclusions.
    # Recheck the final rename-aware Git diff rather than relying only on the
    # pre-stage dirty-path snapshot (finding #55 / cross-lab blocker 1).
    _assert_change_set_not_forbidden(document, forbidden_paths)
    data = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    _write_factory_candidate(worktree, output["path"], data)
    return document


def rederive_change_set(
    workspace: str | Path, *, base_sha: str, allowed_paths: Iterable[str],
) -> dict[str, Any]:
    """Rederive the complete committed change identity from an assigned clone.

    No caller-supplied manifest value participates in this calculation.  The
    returned object is the exact canonical claim a worker must match before
    Factory will seal it.
    """
    worktree = Path(workspace)
    base = _git(worktree, "rev-parse", f"{base_sha}^{{commit}}")
    head = _git(worktree, "rev-parse", "HEAD^{commit}")
    tree = _git(worktree, "rev-parse", "HEAD^{tree}")
    try:
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head], cwd=worktree,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactSealError(f"change-set ancestry validation unavailable: {exc}") from exc
    if ancestry.returncode == 1:
        raise ArtifactValidationError("change-set head is not descended from base")
    if ancestry.returncode != 0:
        raise ArtifactValidationError(
            "change-set ancestry validation failed: "
            + ancestry.stderr.decode("utf-8", errors="replace")[:500]
        )
    if _change_set_dirty(worktree):
        raise ArtifactValidationError("change-set assigned clone has a dirty tree")

    normalized_allowed: list[str] = []
    for value in allowed_paths:
        normalized = _plan_path(value, label="change-set approved allowed path")
        if normalized not in normalized_allowed:
            normalized_allowed.append(normalized)
    if not normalized_allowed:
        raise ArtifactValidationError("change-set approved plan has no allowed paths")
    commits_text = _git(
        worktree, "rev-list", "--reverse", "--topo-order", f"{base}..{head}",
    )
    commits = commits_text.splitlines() if commits_text else []
    if not commits or commits[-1] != head:
        raise ArtifactValidationError(
            "change-set head must contain at least one ordered commit after base"
        )

    raw = _git_bytes(
        worktree, "diff", "--name-status", "-z", "--find-renames", base, head,
    )
    fields = raw.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    changes: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(fields):
        try:
            status_value = fields[cursor].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ArtifactValidationError("change-set diff status is not ASCII") from exc
        cursor += 1
        rename_or_copy = status_value.startswith(("R", "C"))
        path_count = 2 if rename_or_copy else 1
        if cursor + path_count > len(fields):
            raise ArtifactValidationError("change-set rename-aware diff is truncated")
        names = [
            fields[cursor + offset].decode("utf-8", errors="surrogateescape")
            for offset in range(path_count)
        ]
        cursor += path_count
        previous = (
            _repository_path(names[0], label="change-set previous path")
            if rename_or_copy else None
        )
        path = _repository_path(names[-1], label="change-set resulting path")
        if not _path_is_allowed(path, normalized_allowed):
            raise ArtifactValidationError(
                f"change-set path {path!r} is outside the approved plan"
            )
        if previous is not None and not _path_is_allowed(previous, normalized_allowed):
            raise ArtifactValidationError(
                f"change-set previous path {previous!r} is outside the approved plan"
            )
        if previous is not None:
            previous_mode, _previous_sha = _tree_entry(worktree, base, previous)
            if previous_mode == "120000":
                raise ArtifactValidationError(
                    f"change-set previous path {previous!r} is a symlink"
                )
        if status_value == "D":
            deleted_mode, _deleted_sha = _tree_entry(worktree, base, path)
            if deleted_mode == "120000":
                raise ArtifactValidationError(f"change-set path {path!r} is a symlink")
            blob_sha = None
        else:
            mode, blob_sha = _tree_entry(worktree, head, path)
            if mode == "120000":
                raise ArtifactValidationError(f"change-set path {path!r} is a symlink")
        changes.append({
            "status": status_value, "path": path, "previous_path": previous,
            "blob_sha": blob_sha,
        })
    if not changes:
        raise ArtifactValidationError("change-set contains no changed paths")
    document = {
        "schema": "shipfactory.change-set/v1", "base_sha": base,
        "head_sha": head, "tree_sha": tree, "commits": commits,
        "changed_paths": changes, "allowed_paths": normalized_allowed,
        "dirty_tree": False,
    }
    _validate_change_set(document)
    return document


def _validate_change_set_context(
    document: dict[str, Any], instance_id: str, step_id: str, activation: int,
    run_id: int | None, workspace: Path,
) -> dict[str, Any]:
    """Bind a change-set to its exact run, current instance base, and plan."""
    if run_id is None:
        raise ArtifactValidationError("change-set requires an exact producer run")
    with store._connect() as db:
        instance = db.execute(
            "SELECT * FROM recipe_instances WHERE id=?", (instance_id,),
        ).fetchone()
        step = db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND activation=?",
            (instance_id, step_id, int(activation)),
        ).fetchone()
        run = db.execute(
            "SELECT * FROM runs WHERE id=?", (int(run_id),),
        ).fetchone()
        if instance is None or step is None or run is None:
            raise ArtifactValidationError("change-set producer identity is incomplete")
        if (not step["kanban_task_id"] or str(run["task_id"]) != str(step["kanban_task_id"])
                or run["recipe_activation"] is None
                or int(run["recipe_activation"]) != int(activation)):
            raise ArtifactValidationError(
                "change-set producer task/run/activation does not match the assigned build"
            )
        if not run["workspace_path"] or Path(run["workspace_path"]).resolve() != workspace.resolve():
            raise ArtifactValidationError(
                "change-set was not produced in the assigned producer workspace"
            )
        from shipfactory.recipes.instantiate import recipe_for_instance
        recipe = recipe_for_instance(dict(instance), db=db).document
        definition = next(
            (item for item in recipe["steps"] if item["id"] == step_id), None,
        )
        if definition is None:
            raise ArtifactValidationError("change-set build definition is missing")
        plans = [
            artifact for artifact in input_artifacts(db, instance_id, definition)
            if artifact["kind"] == "plan"
        ]
        if len(plans) != 1:
            raise ArtifactValidationError(
                "change-set build must consume exactly one approved sealed plan"
            )
        trusted_base = str(instance["base_sha"] or "")
    plan = artifact_document(plans[0])
    if plan["base_sha"] != trusted_base:
        raise ArtifactValidationError("change-set approved plan base differs from instance base")
    allowed_paths: list[str] = []
    for node in plan["nodes"]:
        for path in node["allowed_paths"]:
            if path not in allowed_paths:
                allowed_paths.append(path)
    expected = rederive_change_set(
        workspace, base_sha=trusted_base, allowed_paths=allowed_paths,
    )
    if document != expected:
        raise ArtifactValidationError(
            "shipfactory.change-set/v1 worker claim differs from the daemon-rederived manifest"
        )
    return expected


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
        if reference["kind"] == "symbol":
            # A byte-perfect hash only proves the citation names SOME real
            # span of text — it says nothing about which symbol that span
            # actually defines or calls. Without this, a correct hash for
            # `login` could be dishonestly labeled `revoke_all_sessions`
            # (a hallucinated symbol) or `lοgin` (a Unicode homoglyph of the
            # real name, byte-distinct from it) and would seal. §2.2.5
            # requires a symbol claim to resolve to a definition or call
            # site in what it cites; require the claimed name to appear,
            # verbatim, as its own token in the cited text (finding #3).
            claimed_symbol = reference["id"]
            cited_text = cited.decode("utf-8", errors="replace")
            if not re.search(r"\b" + re.escape(claimed_symbol) + r"\b", cited_text):
                raise ArtifactValidationError(
                    f"exploration reference {index} claims symbol {claimed_symbol!r} "
                    "which does not resolve to a definition or call site in the "
                    "cited text"
                )


def _changed_paths(workspace: Path, base_sha: str, head_sha: str) -> dict[str, str]:
    """Return the complete NUL-safe changed-path map for a committed revision."""
    raw = _git_bytes(
        workspace, "diff", "--name-status", "-z", "--find-renames",
        base_sha, head_sha,
    )
    parts = raw.split(b"\0")
    if parts and not parts[-1]:
        parts.pop()
    changed: dict[str, str] = {}
    index = 0
    while index < len(parts):
        status = parts[index].decode("ascii", errors="strict")
        index += 1
        count = 2 if status.startswith(("R", "C")) else 1
        if index + count > len(parts):
            raise ArtifactValidationError("review-story diff status output is truncated")
        names = [parts[index + offset].decode("utf-8", errors="surrogateescape")
                 for offset in range(count)]
        index += count
        for name_index, name in enumerate(names):
            path = _repository_path(name, label="review-story changed path")
            effective = "D" if status.startswith("R") and name_index == 0 else status[0]
            if path in changed:
                raise ArtifactValidationError(
                    f"review-story diff contains duplicate path {path!r}"
                )
            changed[path] = effective
    return changed


def _generated_forbidden(path: str, status: str) -> bool:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    lockfiles = {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
        "bun.lock", "bun.lockb", "poetry.lock", "pipfile.lock", "uv.lock",
        "cargo.lock", "gemfile.lock", "composer.lock", "go.sum",
    }
    workflow = lowered == ".github/workflows" or lowered.startswith(".github/workflows/")
    return status == "D" or workflow or name in lockfiles or name.endswith(".lock")


def _configuration_path(path: str) -> bool:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    return (
        lowered.startswith((".github/", ".shipfactory/"))
        or name.startswith(".")
        or name in {"dockerfile", "makefile", "justfile", "procfile"}
        or PurePosixPath(lowered).suffix in {
            ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".lock",
        }
    )


def _not_changed_requirement_ids(items: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for item in items:
        if isinstance(item.get("requirement_ids"), list):
            ids.update(item["requirement_ids"])
        elif isinstance(item.get("requirement_id"), str):
            ids.add(item["requirement_id"])
    return ids


def _validate_review_story_context(
    document: dict[str, Any], instance_id: str, workspace: Path, *,
    step_id: str | None = None, activation: int | None = None,
) -> None:
    """Bind narrative claims to the complete diff, spec, plan, and evidence."""
    schema = "shipfactory.review-story/v1"
    if document["instance_id"] != instance_id:
        raise ArtifactValidationError(f"{schema} instance_id does not match producer instance")
    with store._connect() as db:
        instance = db.execute(
            "SELECT * FROM recipe_instances WHERE id=?", (instance_id,),
        ).fetchone()
        if instance is None or not instance["base_sha"]:
            raise ArtifactValidationError(f"{schema} instance has no trusted base")
        story_step = None
        exact_inputs: list[dict[str, Any]] | None = None
        if step_id is not None and activation is not None:
            story_step = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? "
                "AND step_id=? AND activation=?",
                (instance_id, step_id, int(activation)),
            ).fetchone()
            from shipfactory.recipes.instantiate import recipe_for_instance
            recipe = recipe_for_instance(dict(instance), db=db).document
            if recipe.get("schema") == "shipfactory.recipe/v2":
                if story_step is None:
                    raise ArtifactValidationError(f"{schema} producer activation is unavailable")
                definition = next(
                    (item for item in recipe["steps"] if item["id"] == step_id), None,
                )
                if definition is None:
                    raise ArtifactValidationError(f"{schema} producer definition is unavailable")
                exact_inputs = input_artifacts(db, instance_id, definition)
                if artifact_set_hash(exact_inputs) != story_step["input_artifact_set_hash"]:
                    raise ArtifactValidationError(f"{schema} producer input artifact set changed")
                required_kinds = {"task-spec", "plan", "change-set", "evidence-bundle"}
                if {item["kind"] for item in exact_inputs} != required_kinds:
                    raise ArtifactValidationError(f"{schema} exact declared inputs are incomplete")

        def exact_artifact(kind: str, digest: str) -> Any:
            if exact_inputs is not None:
                matches = [
                    item for item in exact_inputs
                    if item["kind"] == kind and item["sha256"] == digest
                ]
                if len(matches) != 1:
                    return None
                if kind == "evidence-bundle":
                    return db.execute(
                        "SELECT * FROM evidence_bundles WHERE id=? AND state='done'",
                        (matches[0]["id"],),
                    ).fetchone()
                return db.execute(
                    "SELECT * FROM artifacts WHERE id=? AND state='sealed'",
                    (matches[0]["id"],),
                ).fetchone()
            if kind == "evidence-bundle":
                return db.execute(
                    "SELECT * FROM evidence_bundles WHERE instance_id=? AND state='done' "
                    "AND bundle_sha256=? ORDER BY activation DESC LIMIT 1",
                    (instance_id, digest),
                ).fetchone()
            return db.execute(
                "SELECT * FROM artifacts WHERE instance_id=? AND kind=? "
                "AND state='sealed' AND sha256=? ORDER BY activation DESC LIMIT 1",
                (instance_id, kind, digest),
            ).fetchone()

        spec_row = exact_artifact("task-spec", document["task_spec_sha256"])
        plan_row = exact_artifact("plan", document["plan_sha256"])
        change_row = exact_artifact("change-set", document["change_set_sha256"])
        bundle = exact_artifact("evidence-bundle", document["evidence_bundle_sha256"])
        if spec_row is None:
            raise ArtifactValidationError(f"{schema} task_spec_sha256 is not an exact input")
        if plan_row is None:
            raise ArtifactValidationError(f"{schema} plan_sha256 is not an exact input")
        if change_row is None:
            raise ArtifactValidationError(f"{schema} change_set_sha256 is not an exact input")
        if bundle is None:
            raise ArtifactValidationError(
                f"{schema} evidence_bundle_sha256 is not an exact sealed input"
            )
        from shipfactory.verification import verify_evidence_bundle
        verified_bundle = verify_evidence_bundle(bundle["id"], db=db)
        case_rows = db.execute(
            "SELECT case_id,attempt,status FROM verification_cases WHERE bundle_id=?",
            (bundle["id"],),
        ).fetchall()
        waiting = db.execute(
            "SELECT input_revision_hash FROM recipe_steps WHERE instance_id=? "
            "AND primitive='approval_gate' AND state='waiting' "
            "ORDER BY activation DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
    if verified_bundle["bundle_sha256"] != document["evidence_bundle_sha256"]:
        raise ArtifactValidationError(f"{schema} evidence bundle hash changed")
    if waiting and waiting["input_revision_hash"] != document["revision_hash"]:
        raise ArtifactValidationError(f"{schema} revision_hash is not the waiting gate revision")
    if (story_step is not None and story_step["input_artifact_set_hash"]
            and story_step["input_artifact_set_hash"] != document["revision_hash"]):
        raise ArtifactValidationError(
            f"{schema} revision_hash is not the producer input artifact revision"
        )

    spec = artifact_document(dict(spec_row))
    plan = artifact_document(dict(plan_row))
    if plan.get("task_spec_sha256") != document["task_spec_sha256"]:
        raise ArtifactValidationError(f"{schema} plan is not bound to its task spec")
    requirements = {item["id"] for item in spec["requirements"]}
    covered: set[str] = set()
    narrated_paths: list[str] = []
    known_cases = {str(row["case_id"]) for row in case_rows}
    for index, change in enumerate(document["changes"]):
        change_requirements = set(change["requirement_ids"])
        unknown = change_requirements - requirements
        if unknown:
            raise ArtifactValidationError(
                f"{schema} change {index} names unknown requirements: {sorted(unknown)}"
            )
        covered.update(change_requirements)
        narrated_paths.extend(change["files"])
        evidence_ids = set(change["evidence_case_ids"])
        missing_cases = evidence_ids - known_cases
        if missing_cases:
            raise ArtifactValidationError(
                f"{schema} change {index} cites absent evidence cases: {sorted(missing_cases)}"
            )
        # Every change carries a risk assertion; require evidence for all of
        # them so euphemistic wording cannot bypass the safety-claim check.
        if not evidence_ids:
            raise ArtifactValidationError(
                f"{schema} safety claim in change {index} has no evidence case"
            )
    explicit_not_implemented = _not_changed_requirement_ids(document["not_changed"])
    unknown_not_changed = explicit_not_implemented - requirements
    if unknown_not_changed:
        raise ArtifactValidationError(
            f"{schema} not_changed names unknown requirements: {sorted(unknown_not_changed)}"
        )
    if covered | explicit_not_implemented != requirements:
        missing = sorted(requirements - covered - explicit_not_implemented)
        raise ArtifactValidationError(
            f"{schema} does not cover every requirement: {missing}"
        )

    head_sha = str(bundle["head_sha"])
    diff_workspace = workspace
    if change_row is not None:
        change = dict(change_row)
        change_document = artifact_document(change)
        if (change_document["base_sha"] != str(instance["base_sha"])
                or change_document["head_sha"] != head_sha
                or change_document["tree_sha"] != str(bundle["tree_sha"])):
            raise ArtifactValidationError(
                f"{schema} change-set identity differs from the evidence bundle"
            )
        if exact_inputs is not None:
            run = store.run_row(int(change["run_id"])) if change.get("run_id") is not None else None
            if run is None or not run.get("workspace_path"):
                raise ArtifactValidationError(
                    f"{schema} exact change-set producer workspace is unavailable"
                )
            diff_workspace = Path(run["workspace_path"])
            _validate_change_set_context(
                change_document, instance_id, str(change["step_id"]),
                int(change["activation"]), int(change["run_id"]), diff_workspace,
            )
    changed = _changed_paths(diff_workspace, str(instance["base_sha"]), head_sha)
    generated = list(document["generated_or_mechanical_files"])
    all_declared = narrated_paths + generated
    duplicates = sorted({path for path in all_declared if all_declared.count(path) != 1})
    if duplicates:
        raise ArtifactValidationError(
            f"{schema} changed paths must appear exactly once: {duplicates}"
        )
    declared = set(all_declared)
    if declared != set(changed):
        missing = sorted(set(changed) - declared)
        extra = sorted(declared - set(changed))
        raise ArtifactValidationError(
            f"{schema} changed-path completeness mismatch; missing={missing}, extra={extra}"
        )
    for path in generated:
        if _generated_forbidden(path, changed[path]):
            raise ArtifactValidationError(
                f"{schema} path {path!r} cannot be classified as generated/mechanical"
            )
        if _configuration_path(path):
            raise ArtifactValidationError(
                f"{schema} configuration path {path!r} must be called out as a change"
            )
    narrated = set(narrated_paths)
    for path, status in changed.items():
        if (status == "D" or _configuration_path(path)) and path not in narrated:
            raise ArtifactValidationError(
                f"{schema} deletion/configuration path {path!r} must be called out"
            )

    verification_has_caveats = any(
        int(row["attempt"]) > 1
        or str(row["status"]).lower() in {"skipped", "warning", "warnings", "warn"}
        for row in case_rows
    ) or "warning" in str(bundle["invalid_reason"] or "").lower()
    substantive_residual_risks = [
        risk for risk in document.get("residual_risks", [])
        if isinstance(risk, str) and risk.strip()
    ]
    if verification_has_caveats and not substantive_residual_risks:
        raise ArtifactValidationError(
            f"{schema} residual_risks must contain substantive text after "
            "retries/skips/warnings"
        )


def dashboard_safe_review_story(document: dict[str, Any]) -> dict[str, Any]:
    """Escape all narrative strings before returning a story to dashboard HTML."""
    def escape(value: Any) -> Any:
        if isinstance(value, str):
            # Canonicalize through unescape so sealing and subsequent dashboard
            # projection are idempotent rather than double-escaping.
            return html.escape(html.unescape(value), quote=True)
        if isinstance(value, list):
            return [escape(item) for item in value]
        if isinstance(value, dict):
            return {key: escape(item) for key, item in value.items()}
        return value
    return escape(document)


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
        recipe = recipe_for_instance(instance, db=db).document
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
        elif kind == "change-set":
            if run_id is None or producer != f"run:{int(run_id)}":
                raise ArtifactValidationError(
                    "change-set producer must identify its exact durable run"
                )
            document = _validate_change_set_context(
                document, instance_id, step_id, int(activation), run_id, worktree,
            )
            data = json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
            base_sha = document["base_sha"]
            head_sha = document["head_sha"]
            tree_sha = document["tree_sha"]
        elif kind == "review-story":
            _validate_review_story_context(
                document, instance_id, worktree, step_id=step_id,
                activation=int(activation),
            )
            with store._connect() as db:
                story_bundle = db.execute(
                    "SELECT base_sha,head_sha,tree_sha FROM evidence_bundles "
                    "WHERE instance_id=? AND bundle_sha256=? AND state='done'",
                    (instance_id, document["evidence_bundle_sha256"]),
                ).fetchone()
            if story_bundle is None:
                raise ArtifactValidationError(
                    "shipfactory.review-story/v1 evidence identity is unavailable"
                )
            base_sha = str(story_bundle["base_sha"])
            head_sha = str(story_bundle["head_sha"])
            tree_sha = str(story_bundle["tree_sha"])
            # Canonical sealed bytes are the validated artifact, byte-for-byte.
            # Dashboard escaping belongs exclusively to the API/UI projection.
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
        if item["kind"] == "evidence-bundle":
            evidence = db.execute(
                "SELECT * FROM evidence_bundles WHERE instance_id=? AND step_id=? "
                "AND state='done' ORDER BY activation DESC,sealed_at DESC LIMIT 1",
                (instance_id, item["from"]),
            ).fetchone()
            if evidence is None:
                if item["required"]:
                    raise ArtifactMissing(
                        f"artifact_missing:{item['from']}:{item['kind']}"
                    )
                continue
            evidence = dict(evidence)
            if evidence["base_sha"] != instance.get("base_sha"):
                if item["required"]:
                    raise ArtifactStale(
                        f"artifact_stale:{item['from']}:{item['kind']}"
                    )
                continue
            from shipfactory.verification import verify_evidence_bundle
            verify_evidence_bundle(evidence["id"], db=db)
            resolved.append({
                "id": evidence["id"], "kind": "evidence-bundle",
                "sha256": evidence["bundle_sha256"], "base_sha": evidence["base_sha"],
                "head_sha": evidence["head_sha"], "repo_tree_sha": evidence["tree_sha"],
                "state": "sealed", "sealed_at": evidence["sealed_at"],
            })
            continue
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
            "SELECT s.*,i.recipe_id,i.recipe_version,i.recipe_hash "
            "FROM recipe_steps s JOIN recipe_instances i ON i.id=s.instance_id "
            "JOIN recipe_versions v ON v.id=i.recipe_id AND v.version=i.recipe_version "
            "WHERE s.kanban_task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return []
        from shipfactory.recipes.instantiate import recipe_for_instance
        recipe = recipe_for_instance(dict(row), db=db).document
        if recipe.get("schema") != "shipfactory.recipe/v2":
            return []
        definition = next(item for item in recipe["steps"] if item["id"] == row["step_id"])
        inputs = input_artifacts(db, row["instance_id"], definition)
        run = db.execute("SELECT * FROM runs WHERE id=?", (int(run_id),)).fetchone()
        if run is None or str(run["task_id"]) != str(task_id):
            raise ArtifactValidationError("declared output run does not own the reaped task")
        if (run["recipe_activation"] is None
                or int(run["recipe_activation"]) != int(row["activation"])):
            raise ArtifactValidationError("declared output run activation is stale")
        if (not run["workspace_path"]
                or Path(run["workspace_path"]).resolve() != Path(workspace).resolve()):
            raise ArtifactValidationError("declared output workspace differs from its durable run")
    sealed: list[dict[str, Any]] = []
    failures: list[str] = []
    for output in definition["outputs"]:
        try:
            if output["kind"] == "change-set":
                finalize_change_set_for_task(
                    instance_id=row["instance_id"], step_id=row["step_id"],
                    activation=int(row["activation"]), run_id=int(run_id),
                    workspace=workspace, output=output, inputs=inputs,
                )
            child = seal_artifact(
                instance_id=row["instance_id"], step_id=row["step_id"],
                activation=int(row["activation"]), run_id=int(run_id), output=output,
                workspace=workspace, producer=f"run:{int(run_id)}",
                max_bytes=int(max_bytes),
            )
            sealed.append(child)
            for parent in inputs:
                record_artifact_edge(parent["id"], child["id"], "derived-from")
        except ArtifactSealError:
            raise
        except ArtifactValidationError as exc:
            failures.append(f"{output['kind']}: {exc}")
    if failures:
        raise ArtifactValidationError("; ".join(failures))
    return sealed


__all__ = [
    "ArtifactMissing", "ArtifactSealError", "ArtifactStale",
    "ArtifactValidationError", "DEFAULT_ARTIFACT_MAX_BYTES",
    "artifact_document", "artifact_id", "artifact_is_stale", "artifact_set_hash", "input_artifacts",
    "finalize_change_set_for_task", "output_artifacts", "read_artifact",
    "record_artifact_edge", "rederive_change_set", "seal_artifact",
    "seal_declared_outputs_for_task", "task_spec_has_clarifications",
]
