"""SF-6 serial planning, typed artifacts, and v2 budget regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from shipfactory import store
from shipfactory.artifacts import seal_artifact
from shipfactory.recipes.advancer import apply_events, reconcile, release_review_stall
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PROFILES = {
    "planning": {"max_runtime_seconds": 1800, "max_retries": 2, "token_allowance": 30_000},
    "review": {"max_runtime_seconds": 1800, "max_retries": 2, "token_allowance": 25_000},
    "build": {"max_runtime_seconds": 1800, "max_retries": 2, "token_allowance": 50_000},
}
PUBLISHED_SHA256 = {
    1: "fff1275c003037ed84c35e97a38f8c07210b7143f871eb81dcc1b2c11455ab45",
    2: "80743ca9c35d5455fc8c273a02cb7cdfc35c273a682ce4ed61a8327575f2152f",
    3: "79f7812a5372d9e97781ccfb501198ed3cc3c13728d50c291f2e07f8d0fe6d45",
    4: "4fc4ba60ae33754b8a7bc4180bf3fe33ca851a1c167c7105f7b9d0216dc4f68c",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("planning fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Planning Test",
        "GIT_AUTHOR_EMAIL": "planning@example.invalid",
        "GIT_COMMITTER_NAME": "Planning Test",
        "GIT_COMMITTER_EMAIL": "planning@example.invalid",
    }
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, env=env, check=True)
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _candidate(repo: Path, relative: str, document: dict) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _exploration(base_sha: str, tree_sha: str, references: list[dict] | None = None) -> dict:
    return {
        "schema": "shipfactory.exploration/v1",
        "intent_sha256": hashlib.sha256(b"request").hexdigest(),
        "base_sha": base_sha,
        "repo_tree_sha": tree_sha,
        "references": references or [],
        "direct_callers": [],
        "constraints": [],
        "untrusted_directives": [],
        "unknowns": [],
    }


def _task_spec(exploration_id: str, *, clarifications: list[str] | None = None) -> dict:
    return {
        "schema": "shipfactory.task-spec/v1",
        "intent_artifact_id": exploration_id,
        "problem": "Make the requested behavior deterministic.",
        "non_goals": [],
        "requirements": [{
            "id": "REQ-1", "behavior": "The behavior is deterministic.",
            "oracle": "A regression test passes.", "risk": "control-plane",
        }],
        "target_files": ["README.md"],
        "forbidden_paths": [],
        "risk_tags": ["control-plane"],
        "acceptance_cases": ["TEST-REQ-1-A"],
        "rollback_notes": "Revert the change.",
        "assumptions": [],
        "clarifications": clarifications or [],
    }


def _plan(base_sha: str, task_spec_sha: str) -> dict:
    return {
        "schema": "shipfactory.plan/v1",
        "task_spec_sha256": task_spec_sha,
        "base_sha": base_sha,
        "nodes": [{
            "id": "build-readme", "title": "Build the change", "needs": [],
            "kind": "logic", "requirements": ["REQ-1"],
            "allowed_paths": ["README.md"], "expected_outputs": ["change-set"],
            "test_cases": ["TEST-REQ-1-A"], "risk_tags": ["control-plane"],
        }],
        "integration_order": ["build-readme"],
        "shared_file_overlaps": [],
        "residual_risks": [],
    }


def _step(instance_id: str, step_id: str, activation: int | None = None) -> dict:
    with store._connect() as db:
        if activation is None:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
                "ORDER BY activation DESC LIMIT 1", (instance_id, step_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND activation=?",
                (instance_id, step_id, activation),
            ).fetchone()
    assert row is not None
    return dict(row)


def _complete_review(conn, task_id: str, outcome: str, target: str | None = None) -> None:
    from hermes_cli import kanban_db
    verdict = {"outcome": outcome, "body": "APPROVE clean pass"}
    if target:
        verdict = {
            "outcome": "request_changes", "target_step": target,
            "body": "README.md:1 requires changes",
        }
    result = "SHIPFACTORY_VERDICT: " + json.dumps(verdict, separators=(",", ":"))
    assert kanban_db.complete_task(conn, task_id, result=result, summary="reviewed")


def _complete_review_without_target(conn, task_id: str) -> None:
    from hermes_cli import kanban_db  # type: ignore[import-not-found]

    result = (
        'SHIPFACTORY_VERDICT: {"outcome":"request_changes",'
        '"body":"README.md:1 requires changes"}'
    )
    assert kanban_db.complete_task(conn, task_id, result=result, summary="reviewed")


def _advance_to_spec_attack(
    tmp_path: Path, conn, instance_id: str, *,
    clarifications: list[str] | None = None, request: str = "change README",
) -> tuple[Path, str]:
    from hermes_cli import kanban_db
    recipe = load_library(ROOT / "recipes").get("dev-pipeline@5")
    repo, base_sha, tree_sha = _repo(tmp_path)
    instantiate(
        conn, board="test", recipe=recipe, parameters={"request": request},
        instance_id=instance_id, base_sha=base_sha,
    )
    reconcile(conn, instance_id, profiles=PIPELINE_PROFILES)
    explore = _step(instance_id, "explore")
    exploration_doc = _exploration(base_sha, tree_sha)
    _candidate(repo, ".shipfactory-output/exploration.json", exploration_doc)
    exploration = seal_artifact(
        instance_id=instance_id, step_id="explore", activation=1, run_id=1,
        output=recipe.document["steps"][0]["outputs"][0], workspace=repo,
        producer="run:1",
    )
    assert kanban_db.complete_task(conn, explore["kanban_task_id"], result="explored")
    reconcile(conn, instance_id, profiles=PIPELINE_PROFILES)
    spec = _step(instance_id, "spec-draft")
    _candidate(
        repo, ".shipfactory-output/spec.json",
        _task_spec(exploration["id"], clarifications=clarifications),
    )
    seal_artifact(
        instance_id=instance_id, step_id="spec-draft", activation=1, run_id=2,
        output=recipe.document["steps"][1]["outputs"][0], workspace=repo,
        producer="run:2",
    )
    assert kanban_db.complete_task(conn, spec["kanban_task_id"], result="specified")
    reconcile(conn, instance_id, profiles=PIPELINE_PROFILES)
    assert _step(instance_id, "spec-attack")["state"] == "running"
    return repo, base_sha


def _advance_to_plan_draft(
    tmp_path: Path, conn, instance_id: str, *, request: str = "change README",
) -> tuple[Path, str, dict, dict]:
    repo, base_sha = _advance_to_spec_attack(tmp_path, conn, instance_id, request=request)
    spec_attack = _step(instance_id, "spec-attack")
    _complete_review(conn, spec_attack["kanban_task_id"], "approve")
    reconcile(conn, instance_id, profiles=PIPELINE_PROFILES)
    with store._connect() as db:
        task_spec = dict(db.execute(
            "SELECT * FROM artifacts WHERE instance_id=? AND kind='task-spec'",
            (instance_id,),
        ).fetchone())
    recipe = load_library(ROOT / "recipes", persist=False).get("dev-pipeline@5")
    return repo, base_sha, task_spec, recipe.document["steps"][3]["outputs"][0]


def _seal_plan_candidate(
    repo: Path, instance_id: str, base_sha: str, task_spec: dict,
    output: dict, mutate,
):
    document = _plan(base_sha, task_spec["sha256"])
    mutate(document)
    _candidate(repo, ".shipfactory-output/plan.json", document)
    return seal_artifact(
        instance_id=instance_id, step_id="plan-draft", activation=1, run_id=3,
        output=output, workspace=repo, producer="run:3",
    )


def test_dev_pipeline_5_loads_and_published_predecessors_are_byte_pinned():
    for version, expected in PUBLISHED_SHA256.items():
        assert hashlib.sha256(
            (ROOT / "recipes" / f"dev-pipeline@{version}.yaml").read_bytes()
        ).hexdigest() == expected
    recipe = load_library(ROOT / "recipes", persist=False).get("dev-pipeline@5").document
    assert recipe["schema"] == "shipfactory.recipe/v2"
    assert [step["id"] for step in recipe["steps"]] == [
        "explore", "spec-draft", "spec-attack", "plan-draft", "plan-attack", "build",
    ]
    assert recipe["steps"][0]["params"]["access_mode"] == "readonly"
    assert recipe["steps"][0]["params"]["execution_profile"] == "planning"
    assert recipe["budgets"]["token_pools"] == {
        "planning": 120_000, "build": 130_000, "review": 50_000,
    }


def test_exploration_existing_path_must_exist_at_base_sha(tmp_path):
    repo, base_sha, tree_sha = _repo(tmp_path)
    reference = {
        "id": "ref-1", "kind": "path", "status": "existing",
        "path": "missing.py", "git_blob_sha": "a" * 40,
        "start_line": 1, "end_line": 1, "text_sha256": "b" * 64,
    }
    _candidate(
        repo, ".shipfactory-output/exploration.json",
        _exploration(base_sha, tree_sha, [reference]),
    )
    with pytest.raises(ValueError, match="absent at base_sha"):
        seal_artifact(
            instance_id="missing-path", step_id="explore", activation=1, run_id=1,
            output={
                "kind": "exploration", "schema": "shipfactory.exploration/v1",
                "path": ".shipfactory-output/exploration.json",
            },
            workspace=repo, producer="run:1",
        )


def test_exploration_direct_callers_rejects_non_string_members(tmp_path):
    repo, base_sha, tree_sha = _repo(tmp_path)
    document = _exploration(base_sha, tree_sha)
    document["direct_callers"] = [{"symbol": "caller"}, 12345, None]
    _candidate(repo, ".shipfactory-output/exploration.json", document)
    with pytest.raises(ValueError, match="direct_callers must be a string list"):
        seal_artifact(
            instance_id="bad-direct-callers", step_id="explore", activation=1,
            run_id=1, output={
                "kind": "exploration", "schema": "shipfactory.exploration/v1",
                "path": ".shipfactory-output/exploration.json",
            }, workspace=repo, producer="run:1",
        )


def test_spec_approval_is_blocked_while_clarifications_are_nonempty(tmp_path, kanban_conn):
    _advance_to_spec_attack(
        tmp_path, kanban_conn, "clarifications", clarifications=["Which API is authoritative?"],
    )
    gate = _step("clarifications", "spec-attack")
    _complete_review(kanban_conn, gate["kanban_task_id"], "approve")
    reconcile(kanban_conn, "clarifications", profiles=PIPELINE_PROFILES)
    assert _step("clarifications", "spec-attack")["blocked_reason"] == "clarifications_nonempty"
    with store._connect() as db:
        instance = db.execute(
            "SELECT status,blocked_reason FROM recipe_instances WHERE id='clarifications'"
        ).fetchone()
    assert tuple(instance) == ("blocked", "clarifications_nonempty")


def test_operator_release_recovers_clarifications_with_fresh_spec_activation(
    tmp_path, kanban_conn,
):
    _advance_to_spec_attack(
        tmp_path, kanban_conn, "clarification-release",
        clarifications=["Which API is authoritative?"],
    )
    gate = _step("clarification-release", "spec-attack")
    _complete_review(kanban_conn, gate["kanban_task_id"], "approve")
    reconcile(kanban_conn, "clarification-release", profiles=PIPELINE_PROFILES)
    assert gate["activation"] == 1
    assert _step("clarification-release", "spec-attack")["blocked_reason"] == "clarifications_nonempty"

    key = release_review_stall(
        "clarification-release", "spec-attack",
        "operator requests a clarified specification",
    )
    with store._connect() as db:
        queued = db.execute(
            "SELECT state FROM advance_events WHERE key=?", (key,),
        ).fetchone()
        fresh_before_apply = db.execute(
            "SELECT 1 FROM recipe_steps WHERE instance_id='clarification-release' "
            "AND step_id='spec-draft' AND activation=2"
        ).fetchone()
    assert queued["state"] == "pending"
    assert fresh_before_apply is None

    apply_events(kanban_conn, profiles=PIPELINE_PROFILES)
    assert _step("clarification-release", "spec-draft", 2)["state"] == "running"
    assert _step("clarification-release", "spec-attack", 2)["state"] == "pending"
    with store._connect() as db:
        applied = db.execute(
            "SELECT state,outcome FROM advance_events WHERE key=?", (key,),
        ).fetchone()
        instance = db.execute(
            "SELECT status,blocked_reason FROM recipe_instances "
            "WHERE id='clarification-release'"
        ).fetchone()
    assert tuple(applied) == ("applied", "clarifications_nonempty_released")
    assert tuple(instance) == ("running", None)


def test_reconcile_derives_missing_unambiguous_review_target(tmp_path, kanban_conn):
    _advance_to_spec_attack(tmp_path, kanban_conn, "derived-target")
    gate = _step("derived-target", "spec-attack")
    _complete_review_without_target(kanban_conn, gate["kanban_task_id"])

    reconcile(kanban_conn, "derived-target", profiles=PIPELINE_PROFILES)

    assert _step("derived-target", "spec-attack", 1)["blocked_reason"] == "changes_requested"
    assert _step("derived-target", "spec-draft")["activation"] == 2
    assert _step("derived-target", "spec-draft")["state"] == "running"


def test_operator_release_recovers_historical_missing_target_block(
    tmp_path, kanban_conn,
):
    from shipfactory import cli as shipfactory_cli

    _advance_to_spec_attack(tmp_path, kanban_conn, "derived-target-release")
    gate = _step("derived-target-release", "spec-attack")
    _complete_review_without_target(kanban_conn, gate["kanban_task_id"])
    with store._connect() as db:
        now = store._now()
        db.execute(
            "UPDATE recipe_steps SET state='blocked',blocked_reason=?,updated_at=? "
            "WHERE instance_id=? AND step_id='spec-attack' AND activation=1",
            ("invalid request_changes verdict", now, "derived-target-release"),
        )
        db.execute(
            "UPDATE recipe_instances SET status='blocked',blocked_reason=?,updated_at=? WHERE id=?",
            ("invalid request_changes verdict", now, "derived-target-release"),
        )

    queued = shipfactory_cli._recipe_release(
        None, "derived-target-release", "spec-attack",
        "give the reviewer a fresh activation",
    )
    key = queued["key"]
    apply_events(kanban_conn, profiles=PIPELINE_PROFILES)

    # A malformed verdict means the REVIEWER failed: release produces one
    # fresh review activation against the same sealed spec, never a
    # producer rework (the old re-parse/derive path failed the event
    # whenever the parked verdict was genuinely unparseable).
    assert _step("derived-target-release", "spec-draft")["activation"] == 1
    fresh = _step("derived-target-release", "spec-attack")
    assert fresh["activation"] == 2 and fresh["state"] == "running"
    assert fresh["kanban_task_id"] != gate["kanban_task_id"]
    with store._connect() as db:
        event = db.execute(
            "SELECT state,outcome FROM advance_events WHERE key=?", (key,),
        ).fetchone()
        instance = db.execute(
            "SELECT status,blocked_reason FROM recipe_instances "
            "WHERE id='derived-target-release'",
        ).fetchone()
    assert tuple(event) == ("applied", "malformed_verdict_released")
    assert tuple(instance) == ("running", None)


def test_plan_rejects_undeclared_shared_write_overlap(tmp_path, kanban_conn):
    repo, base_sha, task_spec, output = _advance_to_plan_draft(
        tmp_path, kanban_conn, "plan-overlap",
    )

    def overlap(document):
        document["nodes"].append({
            "id": "second-writer", "title": "Also write the shared file", "needs": [],
            "kind": "logic", "requirements": ["REQ-1"],
            "allowed_paths": ["shared/file.py"], "expected_outputs": ["change-set"],
            "test_cases": ["TEST-REQ-1-B"], "risk_tags": ["control-plane"],
        })
        document["nodes"][0]["allowed_paths"] = ["shared/file.py"]
        document["integration_order"].append("second-writer")

    with pytest.raises(ValueError, match="undeclared write overlap 'shared/file.py'"):
        _seal_plan_candidate(repo, "plan-overlap", base_sha, task_spec, output, overlap)


def test_plan_rejects_deployment_control_without_high_risk_tag(tmp_path, kanban_conn):
    repo, base_sha, task_spec, output = _advance_to_plan_draft(
        tmp_path, kanban_conn, "plan-risk",
    )

    def unsafe_deployment(document):
        document["nodes"][0].update({
            "kind": "deployment",
            "allowed_paths": [".github/workflows/deploy.yml"],
            "risk_tags": [],
        })

    with pytest.raises(ValueError, match="without a control-plane or high-risk tag"):
        _seal_plan_candidate(
            repo, "plan-risk", base_sha, task_spec, output, unsafe_deployment,
        )


def test_plan_rejects_node_budget_key_after_token_removal(tmp_path, kanban_conn):
    """Plan-node token budgets were removed with the token system (finding
    #77); a node declaring the old budget key is now an invalid node shape."""
    repo, base_sha, task_spec, output = _advance_to_plan_draft(
        tmp_path, kanban_conn, "plan-budget",
    )

    def with_budget(document):
        document["nodes"][0]["budget"] = {"token_pool": "build", "tokens": 70_000}

    with pytest.raises(ValueError, match="invalid node shape"):
        _seal_plan_candidate(repo, "plan-budget", base_sha, task_spec, output, with_budget)


def test_spec_rejection_reactivates_only_the_spec_cone(tmp_path, kanban_conn):
    _advance_to_spec_attack(tmp_path, kanban_conn, "spec-cone")
    attack = _step("spec-cone", "spec-attack")
    _complete_review(kanban_conn, attack["kanban_task_id"], "request_changes", "spec-draft")
    reconcile(kanban_conn, "spec-cone", profiles=PIPELINE_PROFILES)
    assert _step("spec-cone", "spec-draft", 2)["state"] == "running"
    assert _step("spec-cone", "spec-attack", 2)["state"] == "pending"
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM recipe_steps WHERE instance_id='spec-cone' AND step_id='explore'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM recipe_steps WHERE instance_id='spec-cone' AND step_id='plan-draft'"
        ).fetchone()[0] == 1


def test_plan_rejection_reactivates_only_the_plan_cone(tmp_path, kanban_conn):
    from hermes_cli import kanban_db
    repo, base_sha = _advance_to_spec_attack(tmp_path, kanban_conn, "plan-cone")
    spec_attack = _step("plan-cone", "spec-attack")
    _complete_review(kanban_conn, spec_attack["kanban_task_id"], "approve")
    reconcile(kanban_conn, "plan-cone", profiles=PIPELINE_PROFILES)
    plan_step = _step("plan-cone", "plan-draft")
    with store._connect() as db:
        task_spec = dict(db.execute(
            "SELECT * FROM artifacts WHERE instance_id='plan-cone' AND kind='task-spec'"
        ).fetchone())
    _candidate(repo, ".shipfactory-output/plan.json", _plan(base_sha, task_spec["sha256"]))
    recipe = load_library(ROOT / "recipes", persist=False).get("dev-pipeline@5")
    seal_artifact(
        instance_id="plan-cone", step_id="plan-draft", activation=1, run_id=3,
        output=recipe.document["steps"][3]["outputs"][0], workspace=repo,
        producer="run:3",
    )
    assert kanban_db.complete_task(kanban_conn, plan_step["kanban_task_id"], result="planned")
    reconcile(kanban_conn, "plan-cone", profiles=PIPELINE_PROFILES)
    attack = _step("plan-cone", "plan-attack")
    _complete_review(kanban_conn, attack["kanban_task_id"], "request_changes", "plan-draft")
    reconcile(kanban_conn, "plan-cone", profiles=PIPELINE_PROFILES)
    assert _step("plan-cone", "plan-draft", 2)["state"] == "running"
    assert _step("plan-cone", "plan-attack", 2)["state"] == "pending"
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM recipe_steps WHERE instance_id='plan-cone' AND step_id='explore'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM recipe_steps WHERE instance_id='plan-cone' AND step_id='spec-draft'"
        ).fetchone()[0] == 1


def _budget_recipe(tmp_path: Path, text: str, key: str):
    library = tmp_path / key
    library.mkdir()
    (library / "recipe.yaml").write_text(text, encoding="utf-8")
    return load_library(library).get(key)


def test_step_activation_cap_exhaustion_blocks_instance(tmp_path, kanban_conn):
    from hermes_cli import kanban_db
    recipe = _budget_recipe(tmp_path, """schema: shipfactory.recipe/v2
id: step-budget
version: 1
status: active
description: step budget
intent_tags: [test]
supersedes: null
parameters: {}
budgets:
  max_activations: 4
  max_tokens: 400
  step_activation_caps: {build: 1, attack: 2}
  token_pools: {build: 200, review: 200}
steps:
  - {id: build, primitive: agent_task, title: Build, needs: [], optional: false, inputs: [], outputs: [], params: {seat: dev-backend, instructions: build, execution_profile: build, workspace: worktree}}
  - {id: attack, primitive: review_gate, title: Attack, needs: [build], optional: false, inputs: [], outputs: [], params: {seat: verifier, instructions: attack, execution_profile: review, workspace: worktree}}
""", "step-budget@1")
    profiles = {
        name: {"max_runtime_seconds": 1, "max_retries": 1, "token_allowance": 50}
        for name in ("build", "review")
    }
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="step-cap")
    reconcile(kanban_conn, "step-cap", profiles=profiles)
    assert kanban_db.complete_task(
        kanban_conn, _step("step-cap", "build")["kanban_task_id"], result="built",
    )
    reconcile(kanban_conn, "step-cap", profiles=profiles)
    attack = _step("step-cap", "attack")
    _complete_review(kanban_conn, attack["kanban_task_id"], "request_changes", "build")
    reconcile(kanban_conn, "step-cap", profiles=profiles)
    reason = "budget_exhausted:step_activation_cap:build"
    assert _step("step-cap", "build", 2)["blocked_reason"] == reason
    with store._connect() as db:
        instance = db.execute(
            "SELECT status,blocked_reason FROM recipe_instances WHERE id='step-cap'"
        ).fetchone()
    assert tuple(instance) == ("blocked", reason)
