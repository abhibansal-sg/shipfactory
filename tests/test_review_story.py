"""SF-11 machine-complete review-story adversarial validation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from shipfactory import artifacts, store, verification
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import load_library


GIT_ENV = {
    **os.environ, "GIT_AUTHOR_NAME": "SF11 Story", "GIT_AUTHOR_EMAIL": "story@example.invalid",
    "GIT_COMMITTER_NAME": "SF11 Story", "GIT_COMMITTER_EMAIL": "story@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _candidate(repo: Path, name: str, document: dict) -> str:
    path = repo / ".shipfactory-output" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return f".shipfactory-output/{name}"


def _story_fixture(
    tmp_path: Path, instance_id: str, changed: dict[str, str | None], *, retry: bool = False,
) -> tuple[Path, dict, dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".shipfactory").mkdir()
    (repo / ".shipfactory" / "verification.yaml").write_text(
        """schema: shipfactory.verification/v1
cases:
  - id: approval-flow
    requirement_ids: [REQ-1]
    driver: command
    argv: [python3, -c, "print('ok')"]
    oracle: {type: exit_code, equals: 0}
capture: {video: false, trace: false, screenshots: on-failure}
""", encoding="utf-8",
    )
    for path in changed:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, env=GIT_ENV, check=True)
    base = _git(repo, "rev-parse", "HEAD")

    library = tmp_path / "recipes"
    library.mkdir()
    (library / "story.yaml").write_text("""schema: shipfactory.recipe/v1
id: story-fixture
version: 1
status: active
description: story fixture
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 1, max_step_activations: 1, max_tokens: 1}
steps:
  - id: note
    primitive: notify
    title: Note
    needs: []
    optional: false
    params: {target: test, message: test}
""", encoding="utf-8")
    recipe = load_library(library).get("story-fixture@1")
    # instantiate only needs the real kanban API for collector creation; use a
    # tiny real board connection owned by this fixture.
    from hermes_cli import kanban_db
    conn = kanban_db.connect(tmp_path / "story-kanban.db")
    try:
        instantiate(
            conn, board="test", recipe=recipe, parameters={}, instance_id=instance_id,
            base_sha=base,
        )
    finally:
        conn.close()

    spec_document = {
        "schema": "shipfactory.task-spec/v1", "intent_artifact_id": "a" * 64,
        "problem": "Tell the complete review story.", "non_goals": [],
        "requirements": [{
            "id": "REQ-1", "behavior": "Every changed file is disclosed.",
            "oracle": "The story validator compares the full git diff.", "risk": "security",
        }],
        "target_files": sorted(changed), "forbidden_paths": [],
        "risk_tags": ["control-plane"], "acceptance_cases": ["approval-flow"],
        "rollback_notes": "Revert the candidate commit.", "assumptions": [],
        "clarifications": [],
    }
    spec = artifacts.seal_artifact(
        instance_id=instance_id, step_id="spec", activation=1, run_id=1,
        output={"kind": "task-spec", "schema": "shipfactory.task-spec/v1",
                "path": _candidate(repo, "spec.json", spec_document)},
        workspace=repo, producer="test",
    )
    # Factory candidates are out-of-band outputs, not candidate source changes.
    (repo / ".shipfactory-output" / "spec.json").unlink()

    for path, content in changed.items():
        target = repo / path
        if content is None:
            target.unlink()
        else:
            target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=repo, env=GIT_ENV, check=True)
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")

    plan_document = {
        "schema": "shipfactory.plan/v1", "task_spec_sha256": spec["sha256"],
        "base_sha": base,
        "nodes": [{
            "id": "build", "title": "Build", "needs": [], "kind": "implementation",
            "requirements": ["REQ-1"], "allowed_paths": sorted(changed),
            "expected_outputs": ["reviewed revision"], "test_cases": ["TEST-REQ-1"],
            "risk_tags": ["control-plane"],
        }],
        "integration_order": ["build"], "shared_file_overlaps": [],
        "residual_risks": [],
    }
    plan = artifacts.seal_artifact(
        instance_id=instance_id, step_id="plan", activation=1, run_id=2,
        output={"kind": "plan", "schema": "shipfactory.plan/v1",
                "path": _candidate(repo, "plan.json", plan_document)},
        workspace=repo, producer="test",
    )

    manifest = verification.load_verification_manifest(repo, head)
    bundle_id = verification._bundle_id(instance_id, "verify", 1)
    verification._insert_bundle(
        bundle_id=bundle_id, instance_id=instance_id, step_id="verify", activation=1,
        input_revision_hash="b" * 64, base_sha=base, head_sha=head, tree_sha=tree,
        environment_session_id=None, manifest=manifest,
    )
    now = store._now()
    if retry:
        verification._record_case(
            bundle_id=bundle_id, case_id="approval-flow", attempt=1,
            case={"requirement_ids": ["REQ-1"], "oracle": {"type": "exit_code", "equals": 0}},
            status="failed", item_ids=[], started_at=now, ended_at=now,
        )
    verification._record_case(
        bundle_id=bundle_id, case_id="approval-flow", attempt=2 if retry else 1,
        case={"requirement_ids": ["REQ-1"], "oracle": {"type": "exit_code", "equals": 0}},
        status="passed", item_ids=[], started_at=now, ended_at=now,
    )
    bundle = verification._seal_bundle(
        bundle_id, final_state="done", reason=None, phase_b_eligible=True,
        required_case_ids=["approval-flow"],
    )
    story = {
        "schema": "shipfactory.review-story/v1", "instance_id": instance_id,
        "revision_hash": "b" * 64, "task_spec_sha256": spec["sha256"],
        "plan_sha256": plan["sha256"], "evidence_bundle_sha256": bundle["bundle_sha256"],
        "headline": "Complete review story",
        "changes": [{
            "importance": 1, "requirement_ids": ["REQ-1"], "files": sorted(changed),
            "why": "Preserves the security approval invariant.", "risk": "security",
            "evidence_case_ids": ["approval-flow"],
        }],
        "generated_or_mechanical_files": [], "not_changed": [], "residual_risks": [],
    }
    output = {
        "kind": "review-story", "schema": "shipfactory.review-story/v1",
        "path": _candidate(repo, "story.json", story),
    }
    return repo, story, output


def test_story_omitting_deleted_security_check_is_rejected(tmp_path):
    repo, story, _output = _story_fixture(
        tmp_path, "deleted-check", {"app.py": "new\n", "security_check.py": None},
    )
    story["changes"][0]["files"] = ["app.py"]
    with pytest.raises(artifacts.ArtifactValidationError, match="completeness.*security_check"):
        artifacts._validate_review_story_context(story, "deleted-check", repo)


def test_lockfile_cannot_be_hidden_as_generated(tmp_path):
    repo, story, _output = _story_fixture(
        tmp_path, "lockfile", {"app.py": "new\n", "package-lock.json": "{}\n"},
    )
    story["changes"][0]["files"] = ["app.py"]
    story["generated_or_mechanical_files"] = ["package-lock.json"]
    with pytest.raises(artifacts.ArtifactValidationError, match="cannot be classified"):
        artifacts._validate_review_story_context(story, "lockfile", repo)


def test_html_story_is_stored_escaped_and_dashboard_output_is_safe(tmp_path):
    repo, story, output = _story_fixture(tmp_path, "html", {"app.py": "new\n"})
    payload = '<script>alert("x")</script>'
    story["headline"] = payload
    story["changes"][0]["why"] = payload + " preserves security."
    _candidate(repo, "story.json", story)
    sealed = artifacts.seal_artifact(
        instance_id="html", step_id="story", activation=1, run_id=3,
        output=output, workspace=repo, producer="test",
    )
    raw = artifacts.artifact_document(sealed)
    safe = artifacts.dashboard_safe_review_story(raw)
    assert raw["headline"] != payload and "&lt;script&gt;" in raw["headline"]
    assert "<script>" not in safe["headline"] and "&lt;script&gt;" in safe["headline"]


def test_large_diff_truncation_cannot_omit_changed_files(tmp_path):
    changed = {f"src/file_{index:03d}.py": f"new {index}\n" for index in range(120)}
    repo, story, _output = _story_fixture(tmp_path, "large-diff", changed)
    story["changes"][0]["files"] = story["changes"][0]["files"][:20]
    with pytest.raises(artifacts.ArtifactValidationError, match="completeness mismatch"):
        artifacts._validate_review_story_context(story, "large-diff", repo)


def test_retry_requires_nonempty_residual_risks(tmp_path):
    repo, story, _output = _story_fixture(
        tmp_path, "retry-risk", {"app.py": "new\n"}, retry=True,
    )
    with pytest.raises(artifacts.ArtifactValidationError, match="residual_risks"):
        artifacts._validate_review_story_context(story, "retry-risk", repo)
    story["residual_risks"] = ["The first verification attempt failed before retry."]
    artifacts._validate_review_story_context(story, "retry-risk", repo)


@pytest.mark.parametrize("placeholder", ["", "  "])
@pytest.mark.parametrize("retry", [False, True], ids=["without-caveat", "with-caveat"])
def test_residual_risks_reject_blank_entries(tmp_path, placeholder, retry):
    instance_id = f"blank-risk-{retry}-{len(placeholder)}"
    repo, story, _output = _story_fixture(
        tmp_path, instance_id, {"app.py": "new\n"}, retry=retry,
    )
    story["residual_risks"] = [placeholder]
    with pytest.raises(artifacts.ArtifactValidationError, match="residual_risks"):
        if retry:
            artifacts._validate_review_story_context(story, instance_id, repo)
        else:
            artifacts._validate_review_story(story)


def test_story_hash_fields_are_authoritative(tmp_path):
    repo, story, _output = _story_fixture(tmp_path, "hashes", {"app.py": "new\n"})
    story["evidence_bundle_sha256"] = hashlib.sha256(b"decoy").hexdigest()
    with pytest.raises(artifacts.ArtifactValidationError, match="not a current sealed bundle"):
        artifacts._validate_review_story_context(story, "hashes", repo)
