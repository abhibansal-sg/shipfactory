"""Worker-visible contracts for Factory-validated artifact schemas."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

REQUIRED_TOP_LEVEL: dict[str, frozenset[str]] = {
    "shipfactory.exploration/v1": frozenset({
        "schema", "intent_sha256", "base_sha", "repo_tree_sha", "references",
        "direct_callers", "constraints", "untrusted_directives", "unknowns",
    }),
    "shipfactory.task-spec/v1": frozenset({
        "schema", "intent_artifact_id", "problem", "non_goals", "requirements",
        "target_files", "forbidden_paths", "risk_tags", "acceptance_cases",
        "rollback_notes", "assumptions", "clarifications",
    }),
    "shipfactory.plan/v1": frozenset({
        "schema", "task_spec_sha256", "base_sha", "nodes", "integration_order",
        "shared_file_overlaps", "residual_risks",
    }),
    "shipfactory.review-story/v1": frozenset({
        "schema", "instance_id", "revision_hash", "task_spec_sha256",
        "plan_sha256", "change_set_sha256", "evidence_bundle_sha256", "headline",
        "changes", "generated_or_mechanical_files", "not_changed", "residual_risks",
    }),
}

PLAN_NODE_KEYS = frozenset({
    "id", "title", "needs", "kind", "requirements", "allowed_paths",
    "expected_outputs", "test_cases", "risk_tags",
})
REVIEW_STORY_CHANGE_KEYS = frozenset({
    "importance", "requirement_ids", "files", "why", "risk", "evidence_case_ids",
})
EXPLORATION_REFERENCE_BASE_KEYS = frozenset({"id", "kind", "status"})
EXPLORATION_EXISTING_REFERENCE_KEYS = EXPLORATION_REFERENCE_BASE_KEYS | frozenset({
    "path", "git_blob_sha", "start_line", "end_line", "text_sha256",
})
EXPLORATION_PROPOSED_REFERENCE_KEYS = EXPLORATION_REFERENCE_BASE_KEYS | frozenset({
    "path", "reason", "intended_parent_directory",
})
TASK_SPEC_REQUIREMENT_KEYS = frozenset({"id", "behavior", "oracle", "risk"})

_TEMPLATES: dict[str, dict[str, Any]] = {
    "shipfactory.exploration/v1": {
        "schema": "shipfactory.exploration/v1",
        "intent_sha256": "<64 hex: SHA-256 of the exact request/intent text>",
        "base_sha": "<40 or 64 hex: trusted base commit>",
        "repo_tree_sha": "<40 or 64 hex: tree at base_sha>",
        "references": [
            {
                "id": "<stable citation id>",
                "kind": "file",
                "status": "existing",
                "path": "<repository-relative path>",
                "git_blob_sha": "<40 or 64 hex: blob at base_sha>",
                "start_line": 1,
                "end_line": 1,
                "text_sha256": "<64 hex: SHA-256 of the exact cited text>",
            },
            {
                "id": "<stable proposed-reference id>",
                "kind": "file",
                "status": "proposed",
                "path": "<repository-relative proposed path>",
                "reason": "<why this path is proposed>",
                "intended_parent_directory": "<repository-relative parent directory>",
            },
        ],
        "direct_callers": [],
        "constraints": [],
        "untrusted_directives": [],
        "unknowns": [],
    },
    "shipfactory.task-spec/v1": {
        "schema": "shipfactory.task-spec/v1",
        "intent_artifact_id": "<64 hex: exact sealed exploration artifact id from input>",
        "problem": "<nonempty problem statement>",
        "non_goals": [],
        "requirements": [{
            "id": "REQ-1",
            "behavior": "<observable required behavior>",
            "oracle": "<deterministic acceptance oracle>",
            "risk": "<risk controlled by this requirement>",
        }],
        "target_files": ["<repository-relative target path>"],
        "forbidden_paths": [],
        "risk_tags": [],
        "acceptance_cases": ["<named acceptance case>"],
        "rollback_notes": "<nonempty rollback procedure>",
        "assumptions": [],
        "clarifications": [],
    },
    "shipfactory.plan/v1": {
        "schema": "shipfactory.plan/v1",
        "task_spec_sha256": "<64 hex: exact sealed task-spec SHA-256 from input>",
        "base_sha": "<40 or 64 hex: trusted base commit>",
        "nodes": [{
            "id": "implement-change",
            "title": "<nonempty node title>",
            "needs": [],
            "kind": "implementation",
            "requirements": ["REQ-1"],
            "allowed_paths": ["<repository-relative path or glob>"],
            "expected_outputs": ["<nonempty expected output>"],
            "test_cases": ["<nonempty test case id or command>"],
            "risk_tags": [],
        }],
        "integration_order": ["implement-change"],
        "shared_file_overlaps": [],
        "residual_risks": [],
    },
    "shipfactory.review-story/v1": {
        "schema": "shipfactory.review-story/v1",
        "instance_id": "<exact recipe instance id from input context>",
        "revision_hash": "<64 hex: copy input_artifact_set_hash from your review inputs verbatim>",
        "task_spec_sha256": "<64 hex: exact sealed task-spec SHA-256 from input>",
        "plan_sha256": "<64 hex: exact sealed plan SHA-256 from input>",
        "change_set_sha256": "<64 hex: exact sealed change-set SHA-256 from input>",
        "evidence_bundle_sha256": "<64 hex: exact sealed evidence-bundle SHA-256 from input>",
        "headline": "<nonempty operator-facing headline>",
        "changes": [{
            "importance": 1,
            "requirement_ids": ["REQ-1"],
            "files": ["<repository-relative changed path>"],
            "why": "<nonempty explanation>",
            "risk": "<nonempty residual or mitigated risk>",
            "evidence_case_ids": ["<verification case id>"],
        }],
        "generated_or_mechanical_files": [],
        "not_changed": [],
        "residual_risks": [],
    },
}


def _prompt_placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith("<") and value.endswith(">") else set()
    if isinstance(value, list):
        return set().union(*(_prompt_placeholders(item) for item in value), set())
    if isinstance(value, dict):
        return set().union(*(_prompt_placeholders(item) for item in value.values()), set())
    return set()


PROMPT_PLACEHOLDERS = frozenset(
    set().union(*(_prompt_placeholders(template) for template in _TEMPLATES.values()), set())
)


def find_unresolved_output_placeholder(value: Any) -> str | None:
    """Return the first exact template placeholder copied into candidate JSON."""
    if isinstance(value, str):
        return value if value in PROMPT_PLACEHOLDERS else None
    if isinstance(value, list):
        for item in value:
            found = find_unresolved_output_placeholder(item)
            if found is not None:
                return found
    if isinstance(value, dict):
        for item in value.values():
            found = find_unresolved_output_placeholder(item)
            if found is not None:
                return found
    return None


_NOTES = {
    "shipfactory.exploration/v1": (
        "Each reference also requires string id/kind/status. Existing references require "
        "path, git_blob_sha, start_line, end_line, and text_sha256. Proposed references "
        "require path, reason, and intended_parent_directory. Status is one of existing, "
        "proposed, generated, external. Keep only truthful reference objects and remove any "
        "non-applicable example. All repository paths are relative and may not escape the "
        "repository or name .git. base_sha must be the trusted base and repo_tree_sha its exact "
        "tree. For an existing reference, path must exist at base_sha, git_blob_sha must match, "
        "the line range must be valid, and text_sha256 is the SHA-256 of those exact lines "
        "including their stored line endings. A symbol reference id must appear verbatim as a "
        "token in that cited text. A generated tracked path requires its matching git_blob_sha; "
        "only a path absent at base_sha may omit it. The four list fields contain strings only."
    ),
    "shipfactory.task-spec/v1": (
        "Requirement objects have exactly id, behavior, oracle, risk; ids are unique REQ-N. "
        "intent_artifact_id accepts a 64-hex artifact id; copy the exact sealed exploration "
        "artifact id from the Factory input (this schema validator checks its format, not a "
        "separate context binding). problem and rollback_notes are nonempty strings. All other "
        "plural fields are string lists."
    ),
    "shipfactory.plan/v1": (
        "Node objects have exactly the shown keys. "
        "Node ids are unique lowercase slugs; needs name existing nodes; integration_order "
        "contains unique known node ids; needs must form an acyclic graph. task_spec_sha256 must "
        "match the exact sealed task-spec input and base_sha must match its trusted base. Node "
        "requirements may name only task-spec requirement ids and all task-spec requirements "
        "must be covered across nodes. Every test_cases entry must contain at least one literal "
        "REQ-N id and may mention only requirements assigned to that node. allowed_paths and "
        "shared_file_overlaps are normalized repository-relative paths or globs: no absolute "
        "paths, backslashes, '..', or .git. Every overlap between two nodes' allowed_paths must "
        "have one matching shared_file_overlaps declaration, and every declaration must match "
        "an actual overlap. A node with deployment/release kind, .shipfactory or workflow paths, "
        "policy/verification/deploy paths, or trusted runtime-manifest scripts needs a "
        "control-plane or high-risk risk tag."
    ),
    "shipfactory.review-story/v1": (
        "changes is nonempty and each item has exactly the shown keys. not_changed is either "
        "empty or contains explicit not-implemented objects with requirement_ids, reason, "
        "and not_implemented=true (or disposition='not_implemented'). revision_hash must be "
        "copied VERBATIM from the input_artifact_set_hash value in your Factory-opened "
        "review-input context — do NOT compute, derive, or hash anything for it; it is not the "
        "change-set hash. instance_id and all four artifact hashes (task_spec_sha256, "
        "plan_sha256, change_set_sha256, evidence_bundle_sha256) must likewise be copied "
        "exactly from those same Factory-opened producer inputs. "
        "changes.requirement_ids and not_changed requirement ids may name only ids that exist in "
        "the task-spec. "
        "Every task-spec requirement must appear in changes or an explicit not_changed item. "
        "Every change must cite at least one existing evidence case id from the input bundle. "
        "Every real changed path must appear exactly once across changes.files and "
        "generated_or_mechanical_files, with no extra path. Deletions, configuration files, "
        "workflows, and lockfiles must be narrated as changes and cannot be classified as "
        "generated/mechanical. residual_risks must contain substantive text whenever verification "
        "has retries, skips, or warnings."
    ),
}

for _schema, _template in _TEMPLATES.items():
    if set(_template) != set(REQUIRED_TOP_LEVEL[_schema]):
        raise RuntimeError(f"artifact prompt template drift for {_schema}")

_nested_template_keys = (
    (set(_TEMPLATES["shipfactory.exploration/v1"]["references"][0]),
     set(EXPLORATION_EXISTING_REFERENCE_KEYS), "exploration existing reference"),
    (set(_TEMPLATES["shipfactory.exploration/v1"]["references"][1]),
     set(EXPLORATION_PROPOSED_REFERENCE_KEYS), "exploration proposed reference"),
    (set(_TEMPLATES["shipfactory.task-spec/v1"]["requirements"][0]),
     set(TASK_SPEC_REQUIREMENT_KEYS), "task-spec requirement"),
    (set(_TEMPLATES["shipfactory.plan/v1"]["nodes"][0]),
     set(PLAN_NODE_KEYS), "plan node"),
    (set(_TEMPLATES["shipfactory.review-story/v1"]["changes"][0]),
     set(REVIEW_STORY_CHANGE_KEYS), "review-story change"),
)
for _actual, _expected, _label in _nested_template_keys:
    if _actual != _expected:
        raise RuntimeError(f"artifact prompt nested template drift for {_label}")


def artifact_output_template(schema: str) -> dict[str, Any]:
    """Return an isolated copy of the worker-visible template for tests/tooling."""
    template = _TEMPLATES.get(schema)
    if template is None:
        raise ValueError(f"no worker output contract for artifact schema {schema!r}")
    return deepcopy(template)


def artifact_output_contract(schema: str) -> str:
    """Return the complete worker-visible JSON shape for one supported schema."""
    template = artifact_output_template(schema)
    rendered = json.dumps(template, indent=2, ensure_ascii=False)
    return (
        "Exact JSON template (replace every `<...>` placeholder with truthful exact input or "
        "repository values; placeholders themselves are invalid):\n"
        f"```json\n{rendered}\n```\n"
        "Do not add other top-level fields. " + _NOTES[schema]
    )
