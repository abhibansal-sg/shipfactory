"""Closed-loop dev-pipeline@6 cutover acceptance tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import base64
from pathlib import Path

import pytest

from shipfactory import artifacts, decisions, store
from shipfactory.recipes import advancer
from shipfactory.recipes.instantiate import instantiate, revision_vector
from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "ShipFactory Test",
    "GIT_AUTHOR_EMAIL": "shipfactory@example.invalid",
    "GIT_COMMITTER_NAME": "ShipFactory Test",
    "GIT_COMMITTER_EMAIL": "shipfactory@example.invalid",
}
PUBLISHED_SHA256 = {
    1: "fff1275c003037ed84c35e97a38f8c07210b7143f871eb81dcc1b2c11455ab45",
    2: "80743ca9c35d5455fc8c273a02cb7cdfc35c273a682ce4ed61a8327575f2152f",
    3: "79f7812a5372d9e97781ccfb501198ed3cc3c13728d50c291f2e07f8d0fe6d45",
    4: "4fc4ba60ae33754b8a7bc4180bf3fe33ca851a1c167c7105f7b9d0216dc4f68c",
    5: "b1abcb9a0c5dc80c2a98495583ca7797725a959588b4a4cfb29481c5656ac239",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, env=GIT_ENV, check=True)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".shipfactory-output/\n", encoding="utf-8")
    (repo / ".shipfactory").mkdir()
    (repo / ".shipfactory" / "verification.yaml").write_text(
        """schema: shipfactory.verification/v1
cases:
  - id: unit-suite
    requirement_ids: [REQ-1]
    driver: command
    argv: [python3, -c, "print('ok')"]
    oracle: {type: exit_code, equals: 0}
capture: {video: false, trace: false, screenshots: never}
""",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "old.py").write_text("old = True\n", encoding="utf-8")
    return repo, _commit(repo, "base")


def _minimal_recipe(tmp_path: Path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "change.yaml").write_text(
        """schema: shipfactory.recipe/v2
id: change-fixture
version: 1
status: active
description: strict change-set fixture
intent_tags: [test]
supersedes: null
parameters: {}
budgets:
  max_activations: 3
  max_tokens: 300
  step_activation_caps: {plan: 1, build: 1}
  token_pools: {planning: 100, build: 200}
steps:
  - id: plan
    primitive: agent_task
    title: Plan
    needs: []
    optional: false
    inputs: []
    outputs:
      - {kind: plan, schema: shipfactory.plan/v1, path: .shipfactory-output/plan.json}
    params: {seat: planner, instructions: plan, execution_profile: planning, workspace: worktree, access_mode: workspace_write, environment: source}
  - id: build
    primitive: agent_task
    title: Build
    needs: [plan]
    optional: false
    inputs:
      - {from: plan, kind: plan, required: true}
    outputs:
      - {kind: change-set, schema: shipfactory.change-set/v1, path: .shipfactory-output/change-set.json}
    params: {seat: builder, instructions: build, execution_profile: build, workspace: worktree, access_mode: workspace_write, environment: source}
  - id: approval
    primitive: approval_gate
    title: Approve
    needs: [build]
    optional: false
    inputs:
      - {from: plan, kind: plan, required: true}
      - {from: build, kind: change-set, required: true}
    outputs: []
    params: {approvers: [operator], instructions: approve exact bytes}
""",
        encoding="utf-8",
    )
    return load_library(library).get("change-fixture@1")


def _seed_change_set(
    tmp_path: Path, kanban_conn, instance_id: str,
) -> tuple[Path, str, str, int, dict]:
    from hermes_cli import kanban_db

    repo, base = _repo(tmp_path)
    recipe = _minimal_recipe(tmp_path)
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={},
        instance_id=instance_id, base_sha=base,
    )
    plan_document = {
        "schema": "shipfactory.plan/v1", "task_spec_sha256": "a" * 64,
        "base_sha": base,
        "nodes": [{
            "id": "build", "title": "Build", "needs": [], "kind": "logic",
            "requirements": ["REQ-1"],
            "allowed_paths": ["src/*.py", "old.py", "new.py"],
            "expected_outputs": ["change-set"], "test_cases": ["TEST-REQ-1"],
            "risk_tags": [],
        }],
        "integration_order": ["build"], "shared_file_overlaps": [],
        "residual_risks": [],
    }
    plan_bytes = json.dumps(plan_document, sort_keys=True, separators=(",", ":")).encode()
    plan_path = tmp_path / f"{instance_id}-plan.json"
    plan_path.write_bytes(plan_bytes)
    now = store._now()
    plan_id = artifacts.artifact_id(instance_id, "plan", 1, "plan")
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='done',output_revision=1 WHERE instance_id=? AND step_id='plan'",
            (instance_id,),
        )
        db.execute(
            "INSERT INTO artifacts(id,instance_id,step_id,activation,run_id,kind,"
            "schema_version,state,candidate_path,sealed_path,sha256,size_bytes,producer,"
            "base_sha,head_sha,repo_tree_sha,created_at,sealed_at) "
            "VALUES(?,?, 'plan',1,1,'plan',1,'sealed',?,?,?,?,'test',?,?,?,?,?)",
            (
                plan_id, instance_id, ".shipfactory-output/plan.json", str(plan_path),
                hashlib.sha256(plan_bytes).hexdigest(), len(plan_bytes), base, base,
                _git(repo, "rev-parse", f"{base}^{{tree}}"), now, now,
            ),
        )
    task_id = kanban_db.create_task(
        kanban_conn, title="Build", body="build", assignee="builder",
        workspace_kind="worktree", workspace_path=str(repo), board="test",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='running',kanban_task_id=? "
            "WHERE instance_id=? AND step_id='build'",
            (task_id, instance_id),
        )
    run_id = store.record_run_start(
        task_id, "builder", "codex", "model", workspace_path=repo,
        recipe_activation=1,
    )
    (repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "old.py").rename(repo / "new.py")
    head = _commit(repo, "candidate")
    document = artifacts.rederive_change_set(
        repo, base_sha=base, allowed_paths=["src/*.py", "old.py", "new.py"],
    )
    return repo, base, head, run_id, document


def _seal(
    repo: Path, instance_id: str, run_id: int, document: dict,
    *, workspace: Path | None = None,
) -> dict:
    output_path = (workspace or repo) / ".shipfactory-output" / "change-set.json"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(document), encoding="utf-8")
    return artifacts.seal_artifact(
        instance_id=instance_id, step_id="build", activation=1, run_id=run_id,
        output={"kind": "change-set", "schema": "shipfactory.change-set/v1",
                "path": ".shipfactory-output/change-set.json"},
        workspace=workspace or repo, producer=f"run:{run_id}",
    )


def _dirty_finalizer_case(
    tmp_path: Path, kanban_conn, instance_id: str,
) -> tuple[Path, str, int, dict, list[dict]]:
    """Prepare one assigned build run with validated source dirt at base."""
    repo, base, _old_head, run_id, _document = _seed_change_set(
        tmp_path, kanban_conn, instance_id,
    )
    subprocess.run(
        ["git", "reset", "--hard", base], cwd=repo, check=True, capture_output=True,
    )
    (repo / "src" / "app.py").write_text(
        f"value = {len(instance_id) + 10}\n", encoding="utf-8",
    )
    recipe = load_library(tmp_path / "library", persist=False).get("change-fixture@1")
    definition = next(step for step in recipe.document["steps"] if step["id"] == "build")
    with store._connect() as db:
        inputs = artifacts.input_artifacts(db, instance_id, definition)
    return repo, base, run_id, definition["outputs"][0], inputs


def _sealed_task_spec_input(tmp_path: Path, *, forbidden_paths: list[str]) -> dict:
    document = {
        "schema": "shipfactory.task-spec/v1",
        "intent_artifact_id": "a" * 64,
        "problem": "Exercise the Factory change-set exclusion boundary.",
        "non_goals": [],
        "requirements": [{
            "id": "REQ-1", "behavior": "Only approved source paths may change.",
            "oracle": "Factory rejects forbidden changed paths.", "risk": "security",
        }],
        "target_files": ["src/app.py", "old.py", "new.py"],
        "forbidden_paths": forbidden_paths,
        "risk_tags": ["security"],
        "acceptance_cases": ["TEST-REQ-1"],
        "rollback_notes": "Reset the isolated fixture worktree.",
        "assumptions": [], "clarifications": [],
    }
    data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / ("task-spec-" + hashlib.sha256(data).hexdigest()[:12] + ".json")
    path.write_bytes(data)
    return {
        "kind": "task-spec", "state": "sealed", "schema_version": 1,
        "sealed_path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _finalize_dirty_case(
    repo: Path, instance_id: str, run_id: int, output: dict, inputs: list[dict],
) -> dict:
    return artifacts.finalize_change_set_for_task(
        instance_id=instance_id, step_id="build", activation=1, run_id=run_id,
        workspace=repo, output=output, inputs=inputs,
    )


def _factory_commit_intent(instance_id: str) -> dict | None:
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM action_intents WHERE instance_id=? "
            "AND kind='factory_commit_ref_update'",
            (instance_id,),
        ).fetchone()
    return dict(row) if row else None


def test_dev_pipeline_6_is_the_exact_closed_loop_without_delivery_side_effects():
    for version, expected in PUBLISHED_SHA256.items():
        assert hashlib.sha256(
            (ROOT / "recipes" / f"dev-pipeline@{version}.yaml").read_bytes()
        ).hexdigest() == expected
    recipe = load_library(ROOT / "recipes", persist=False).get("dev-pipeline@6").document
    assert recipe["supersedes"] == "dev-pipeline@5"
    assert [step["id"] for step in recipe["steps"]] == [
        "explore", "spec-draft", "spec-attack", "plan-draft", "plan-attack",
        "build", "verify-runtime", "correctness-review", "adversarial-review",
        "review-story", "approval", "notify",
    ]
    definitions = {step["id"]: step for step in recipe["steps"]}
    assert definitions["build"]["outputs"] == [{
        "kind": "change-set", "schema": "shipfactory.change-set/v1",
        "path": ".shipfactory-output/change-set.json",
    }]
    assert definitions["verify-runtime"]["primitive"] == "verification"
    assert definitions["verify-runtime"]["params"] == {
        "manifest": ".shipfactory/verification.yaml",
        "profile": "browser-standard", "environment": "app",
    }
    for review_id in ("correctness-review", "adversarial-review"):
        review = definitions[review_id]
        assert review["params"]["access_mode"] == "readonly"
        assert {item["kind"] for item in review["inputs"]} == {
            "task-spec", "plan", "change-set", "evidence-bundle",
        }
        assert "upstream build" in review["params"]["instructions"]
    assert definitions["review-story"]["outputs"][0]["schema"] == "shipfactory.review-story/v1"
    assert definitions["approval"]["params"]["approvers"] == ["operator"]
    assert definitions["notify"]["optional"] is True
    assert not ({"merge", "release", "deploy"} & {
        step["primitive"] for step in recipe["steps"]
    })


def test_dev_pipeline_6_loads_against_exact_live_seats_and_profiles(tmp_path):
    library = tmp_path / "only-v6"
    library.mkdir()
    (library / "dev-pipeline@6.yaml").write_bytes(
        (ROOT / "recipes" / "dev-pipeline@6.yaml").read_bytes()
    )
    seats = {"explorer", "dev-backend", "verifier", "architect", "operator"}
    profiles = {"planning", "build", "review"}
    verification_profiles = {"browser-standard"}
    loaded = load_library(
        library, seats=seats, profiles=profiles,
        verification_profiles=verification_profiles, persist=False,
    ).get("dev-pipeline@6")
    assert loaded.seats == frozenset(seats)
    assert loaded.profiles == frozenset(profiles)
    assert loaded.verification_profiles == frozenset(verification_profiles)
    with pytest.raises(ValueError, match="unknown seat 'architect'"):
        load_library(
            library, seats=seats - {"architect"}, profiles=profiles,
            verification_profiles=verification_profiles, persist=False,
        )
    with pytest.raises(ValueError, match="unknown profile 'review'"):
        load_library(
            library, seats=seats, profiles=profiles - {"review"},
            verification_profiles=verification_profiles, persist=False,
        )
    with pytest.raises(ValueError, match="unknown verification profile 'browser-standard'"):
        load_library(
            library, seats=seats, profiles=profiles,
            verification_profiles=set(), persist=False,
        )


def test_change_set_is_rederived_and_sealed_canonically(tmp_path, kanban_conn):
    repo, base, head, run_id, document = _seed_change_set(tmp_path, kanban_conn, "canonical")
    sealed = _seal(repo, "canonical", run_id, document)
    actual = artifacts.artifact_document(sealed)
    assert actual == document
    assert actual["base_sha"] == base and actual["head_sha"] == head
    assert actual["tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert actual["dirty_tree"] is False
    assert actual["commits"] == [head]
    assert {item["status"] for item in actual["changed_paths"]} == {"M", "R100"}
    assert next(item for item in actual["changed_paths"] if item["status"] == "R100")[
        "previous_path"
    ] == "old.py"


def test_public_reap_commits_dirty_build_and_seals_factory_manifest(
    tmp_path, kanban_conn, monkeypatch,
):
    from shipfactory import spawn

    repo, base, _old_head, run_id, _document = _seed_change_set(
        tmp_path, kanban_conn, "factory-reap",
    )
    subprocess.run(["git", "reset", "--hard", base], cwd=repo, check=True, capture_output=True)
    (repo / "src" / "app.py").write_text("value = 7\n", encoding="utf-8")
    output = repo / ".shipfactory-output" / "change-set.json"
    output.unlink(missing_ok=True)
    run = store.run_row(run_id)
    assert run is not None

    class Exited:
        def poll(self):
            return 0

    log_path = tmp_path / "factory-reap.jsonl"
    log_path.write_text(
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"SHIPFACTORY_RESULT: done edited approved source"}}\n',
        encoding="utf-8",
    )
    spawn._RUNNING.clear()
    spawn._RUNNING[99123] = {
        "proc": Exited(), "run_id": run_id, "task_id": run["task_id"],
        "executor": "codex", "board": None, "log_path": log_path,
        "workspace_path": repo, "started": None, "started_at": store._now(),
        "process_start_token": None, "task_attempt_id": None,
        "lease_key": f"worker_slot:run:{run_id}", "adopted": False,
    }
    monkeypatch.setattr(spawn, "_plan_worker_transition", lambda *args, **kwargs: "planned")
    result = spawn.reap_finished()
    assert result[0]["result"] == "done"
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert _git(repo, "show", "-s", "--format=%an <%ae>") == (
        "Abhinav Bansal <abhibansal-sg@users.noreply.github.com>"
    )
    with store._connect() as db:
        sealed = dict(db.execute(
            "SELECT * FROM artifacts WHERE instance_id='factory-reap' "
            "AND step_id='build' AND kind='change-set' AND state='sealed'",
        ).fetchone())
    document = artifacts.artifact_document(sealed)
    assert document == artifacts.rederive_change_set(
        repo, base_sha=base, allowed_paths=["src/*.py", "old.py", "new.py"],
    )
    assert json.loads(output.read_text()) == document


def test_factory_commit_stages_validated_paths_without_touching_ignored_output_root(
    tmp_path, kanban_conn,
):
    instance_id = "factory-ignored-output"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    excludes = tmp_path / "factory-global-excludes"
    excludes.write_text(".shipfactory-output/\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.excludesFile", str(excludes)],
        cwd=repo, check=True, capture_output=True,
    )
    ignored = repo / ".shipfactory-output" / "worker-state" / "ignored.txt"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("not source\n", encoding="utf-8")

    document = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)

    assert document["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert _git(repo, "show", "--format=", "--name-only", "HEAD").splitlines() == [
        "src/app.py",
    ]
    assert ignored.read_text(encoding="utf-8") == "not source\n"


def test_factory_change_set_finalizes_rework_from_instance_pivot_base(
    tmp_path, kanban_conn,
):
    """A stale-but-lineage-valid plan commits rework on the advanced base."""
    instance_id = "factory-rework-pivot"
    repo, plan_base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    subprocess.run(
        ["git", "reset", "--hard", plan_base], cwd=repo, check=True, capture_output=True,
    )
    (repo / "src" / "app.py").write_text("value = 20\n", encoding="utf-8")
    pivot = _commit(repo, "prior sealed build")
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO artifacts(id,instance_id,step_id,activation,run_id,kind,"
            "schema_version,state,producer,base_sha,head_sha,repo_tree_sha,created_at,sealed_at) "
            "VALUES(?,?, 'prior-build',1,999,'change-set',1,'sealed','test',?,?,?,?,?)",
            (
                artifacts.artifact_id(instance_id, "prior-build", 1, "change-set"),
                instance_id, plan_base, pivot,
                _git(repo, "rev-parse", f"{pivot}^{{tree}}"), now, now,
            ),
        )
        db.execute(
            "UPDATE recipe_instances SET base_sha=?,updated_base_at=? WHERE id=?",
            (pivot, now, instance_id),
        )
    (repo / "src" / "app.py").write_text("value = 21\n", encoding="utf-8")

    document = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)

    assert document["base_sha"] == pivot
    assert document["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "HEAD^") == pivot


def test_factory_change_set_finalize_is_idempotent_after_commit_before_manifest(
    tmp_path, kanban_conn,
):
    repo, base, _old_head, run_id, _document = _seed_change_set(
        tmp_path, kanban_conn, "factory-retry",
    )
    subprocess.run(["git", "reset", "--hard", base], cwd=repo, check=True, capture_output=True)
    (repo / "src" / "app.py").write_text("value = 8\n", encoding="utf-8")
    recipe = load_library(tmp_path / "library", persist=False).get("change-fixture@1")
    definition = next(step for step in recipe.document["steps"] if step["id"] == "build")
    with store._connect() as db:
        inputs = artifacts.input_artifacts(db, "factory-retry", definition)
    output = definition["outputs"][0]
    first = artifacts.finalize_change_set_for_task(
        instance_id="factory-retry", step_id="build", activation=1, run_id=run_id,
        workspace=repo, output=output, inputs=inputs,
    )
    committed_head = _git(repo, "rev-parse", "HEAD")
    (repo / output["path"]).unlink()
    second = artifacts.finalize_change_set_for_task(
        instance_id="factory-retry", step_id="build", activation=1, run_id=run_id,
        workspace=repo, output=output, inputs=inputs,
    )
    assert second == first
    assert _git(repo, "rev-parse", "HEAD") == committed_head
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"


def test_factory_change_set_rejects_forged_public_factory_commit_metadata(
    tmp_path, kanban_conn,
):
    instance_id = "factory-forgery"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    forged_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Abhinav Bansal",
        "GIT_AUTHOR_EMAIL": "abhibansal-sg@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Abhinav Bansal",
        "GIT_COMMITTER_EMAIL": "abhibansal-sg@users.noreply.github.com",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=forged_env)
    subprocess.run(
        ["git", "commit", "-qm", f"ShipFactory: canonical build {instance_id}/build/1"],
        cwd=repo, check=True, env=forged_env,
    )

    with pytest.raises(
        artifacts.ArtifactValidationError, match="no exact durable Factory commit intent",
    ):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert _factory_commit_intent(instance_id) is None


def test_factory_change_set_rejects_task_spec_forbidden_changed_path(
    tmp_path, kanban_conn,
):
    instance_id = "factory-forbidden-path"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    task_spec = _sealed_task_spec_input(tmp_path, forbidden_paths=["src/app.py"])

    with pytest.raises(
        artifacts.ArtifactValidationError,
        match=r"src/app\.py.*forbidden by the task specification",
    ):
        _finalize_dirty_case(repo, instance_id, run_id, output, [*inputs, task_spec])

    assert _git(repo, "rev-parse", "HEAD") == base
    assert _factory_commit_intent(instance_id) is None


def test_factory_change_set_retry_rejects_forbidden_rename_source(
    tmp_path, kanban_conn,
):
    instance_id = "factory-forbidden-rename-source"
    repo, _base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    (repo / "old.py").rename(repo / "new.py")
    committed = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert any(
        change.get("previous_path") == "old.py" and change["path"] == "new.py"
        for change in committed["changed_paths"]
    )
    task_spec = _sealed_task_spec_input(tmp_path, forbidden_paths=["old.py"])

    with pytest.raises(
        artifacts.ArtifactValidationError,
        match=r"old\.py.*forbidden by the task specification",
    ):
        _finalize_dirty_case(repo, instance_id, run_id, output, [*inputs, task_spec])

    intent = _factory_commit_intent(instance_id)
    assert intent is not None and intent["state"] == "succeeded"


def test_factory_commit_crash_before_object_keeps_head_at_base_and_retries(
    tmp_path, kanban_conn, monkeypatch,
):
    instance_id = "factory-before-object"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    original = artifacts._write_factory_commit_object

    def crash_before_object(*args, **kwargs):
        raise artifacts.ArtifactSealError("injected crash before commit object")

    monkeypatch.setattr(artifacts, "_write_factory_commit_object", crash_before_object)
    with pytest.raises(artifacts.ArtifactSealError, match="before commit object"):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _factory_commit_intent(instance_id) is None

    monkeypatch.setattr(artifacts, "_write_factory_commit_object", original)
    document = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert document["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert _factory_commit_intent(instance_id)["state"] == "succeeded"


def test_factory_commit_crash_after_object_before_intent_leaves_object_unreachable(
    tmp_path, kanban_conn, monkeypatch,
):
    instance_id = "factory-after-object"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    original = artifacts._persist_factory_commit_intent
    captured: dict = {}

    def crash_before_intent(payload):
        captured.update(payload)
        subprocess.run(
            ["git", "cat-file", "-e", f"{payload['expected_commit_sha']}^{{commit}}"],
            cwd=repo, check=True,
        )
        raise artifacts.ArtifactSealError("injected crash after commit object")

    monkeypatch.setattr(artifacts, "_persist_factory_commit_intent", crash_before_intent)
    with pytest.raises(artifacts.ArtifactSealError, match="after commit object"):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert captured["base_sha"] == base
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _factory_commit_intent(instance_id) is None

    monkeypatch.setattr(artifacts, "_persist_factory_commit_intent", original)
    document = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert document["head_sha"] == captured["expected_commit_sha"]
    assert _factory_commit_intent(instance_id)["state"] == "succeeded"


def test_factory_commit_crash_after_intent_retries_ref_and_generic_runner_cannot_claim(
    tmp_path, kanban_conn, monkeypatch,
):
    instance_id = "factory-after-intent"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    original = artifacts._update_factory_commit_ref

    def crash_before_ref(*args, **kwargs):
        raise artifacts.ArtifactSealError("injected crash after durable intent")

    monkeypatch.setattr(artifacts, "_update_factory_commit_ref", crash_before_ref)
    with pytest.raises(artifacts.ArtifactSealError, match="after durable intent"):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    intent = _factory_commit_intent(instance_id)
    assert intent["state"] == "prepared"
    assert _git(repo, "rev-parse", "HEAD") == base
    assert advancer.run_action_intents(
        kanban_conn, kinds={"factory_commit_ref_update"}, limit=1,
    ) == 0
    assert _factory_commit_intent(instance_id)["state"] == "prepared"

    # The kind itself is also fenced: even a corrupted generic state must not
    # let the generic executor claim this finalizer-owned journal record.
    with store._connect() as db:
        db.execute(
            "UPDATE action_intents SET state='planned' WHERE key=?", (intent["key"],),
        )
    assert advancer.run_action_intents(
        kanban_conn, kinds={"factory_commit_ref_update"}, limit=1,
    ) == 0
    assert _factory_commit_intent(instance_id)["state"] == "planned"
    with store._connect() as db:
        db.execute(
            "UPDATE action_intents SET state='prepared' WHERE key=?", (intent["key"],),
        )

    monkeypatch.setattr(artifacts, "_update_factory_commit_ref", original)
    document = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert document["head_sha"] == json.loads(intent["payload_json"])["expected_commit_sha"]
    assert _factory_commit_intent(instance_id)["state"] == "succeeded"


def test_factory_commit_crash_after_ref_before_terminal_or_manifest_retries(
    tmp_path, kanban_conn, monkeypatch,
):
    instance_id = "factory-after-ref"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    original = artifacts._mark_factory_commit_intent_succeeded

    def crash_after_ref(*args, **kwargs):
        raise artifacts.ArtifactSealError("injected crash after ref update")

    monkeypatch.setattr(artifacts, "_mark_factory_commit_intent_succeeded", crash_after_ref)
    with pytest.raises(artifacts.ArtifactSealError, match="after ref update"):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    intent = _factory_commit_intent(instance_id)
    expected_sha = json.loads(intent["payload_json"])["expected_commit_sha"]
    assert intent["state"] == "prepared"
    assert _git(repo, "rev-parse", "HEAD") == expected_sha
    assert expected_sha != base
    assert not (repo / output["path"]).exists()

    monkeypatch.setattr(artifacts, "_mark_factory_commit_intent_succeeded", original)
    document = _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert document["head_sha"] == expected_sha
    assert json.loads((repo / output["path"]).read_text()) == document
    assert _factory_commit_intent(instance_id)["state"] == "succeeded"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("run_id", lambda value: int(value) + 1),
        ("workspace", lambda value: value + "-foreign"),
        ("base_sha", lambda value: "f" * len(value)),
        ("tree_sha", lambda value: "e" * len(value)),
        ("activation", lambda value: int(value) + 1),
    ],
)
def test_factory_commit_intent_context_mismatch_fails_closed(
    tmp_path, kanban_conn, monkeypatch, field, replacement,
):
    instance_id = f"factory-intent-mismatch-{field}"
    repo, base, run_id, output, inputs = _dirty_finalizer_case(
        tmp_path, kanban_conn, instance_id,
    )
    original = artifacts._update_factory_commit_ref
    monkeypatch.setattr(
        artifacts, "_update_factory_commit_ref",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            artifacts.ArtifactSealError("injected before ref")
        ),
    )
    with pytest.raises(artifacts.ArtifactSealError, match="before ref"):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    intent = _factory_commit_intent(instance_id)
    payload = json.loads(intent["payload_json"])
    payload[field] = replacement(payload[field])
    with store._connect() as db:
        db.execute(
            "UPDATE action_intents SET payload_json=? WHERE key=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), intent["key"]),
        )
    monkeypatch.setattr(artifacts, "_update_factory_commit_ref", original)

    with pytest.raises(artifacts.ArtifactValidationError, match="intent payload mismatched"):
        _finalize_dirty_case(repo, instance_id, run_id, output, inputs)
    assert _git(repo, "rev-parse", "HEAD") == base


def test_factory_change_set_rejects_out_of_plan_dirt_before_commit(tmp_path, kanban_conn):
    repo, base, _old_head, run_id, _document = _seed_change_set(
        tmp_path, kanban_conn, "factory-outside",
    )
    subprocess.run(["git", "reset", "--hard", base], cwd=repo, check=True, capture_output=True)
    (repo / "outside.txt").write_text("not approved\n", encoding="utf-8")
    definition = next(step for step in load_library(
        tmp_path / "library", persist=False,
    ).get("change-fixture@1").document["steps"] if step["id"] == "build")
    with store._connect() as db:
        inputs = artifacts.input_artifacts(db, "factory-outside", definition)
    with pytest.raises(artifacts.ArtifactValidationError, match="outside the approved plan"):
        artifacts.finalize_change_set_for_task(
            instance_id="factory-outside", step_id="build", activation=1, run_id=run_id,
            workspace=repo, output=definition["outputs"][0], inputs=inputs,
        )
    assert _git(repo, "rev-parse", "HEAD") == base


@pytest.mark.parametrize(
    "attack", ["dropped", "extra", "renamed", "wrong-blob", "widened-scope"],
)
def test_change_set_rejects_worker_manifest_lies(tmp_path, kanban_conn, attack):
    instance_id = f"lie-{attack}"
    repo, _base, _head, run_id, document = _seed_change_set(
        tmp_path, kanban_conn, instance_id,
    )
    if attack == "dropped":
        document["changed_paths"].pop()
    elif attack == "extra":
        document["changed_paths"].append({
            "status": "A", "path": "src/extra.py", "previous_path": None,
            "blob_sha": "f" * 40,
        })
    elif attack == "renamed":
        document["changed_paths"][0]["path"] = "src/other.py"
    elif attack == "wrong-blob":
        document["changed_paths"][0]["blob_sha"] = "f" * 40
    else:
        document["allowed_paths"].append("**")
    with pytest.raises(artifacts.ArtifactValidationError, match="rederived"):
        _seal(repo, instance_id, run_id, document)


def test_change_set_rejects_dirty_post_commit_candidate(tmp_path, kanban_conn):
    repo, _base, _head, run_id, document = _seed_change_set(
        tmp_path, kanban_conn, "dirty",
    )
    (repo / "src" / "app.py").write_text("post-test mutation\n", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactValidationError, match="dirty"):
        _seal(repo, "dirty", run_id, document)


def test_change_set_rejects_same_sha_from_wrong_worktree(tmp_path, kanban_conn):
    repo, _base, _head, run_id, document = _seed_change_set(
        tmp_path, kanban_conn, "wrong-worktree",
    )
    foreign = tmp_path / "foreign"
    subprocess.run(["git", "clone", "-q", str(repo), str(foreign)], check=True)
    with pytest.raises(artifacts.ArtifactValidationError, match="assigned producer workspace"):
        _seal(repo, "wrong-worktree", run_id, document, workspace=foreign)


def test_change_set_rejects_changed_symlink(tmp_path, kanban_conn):
    repo, base, _head, run_id, _document = _seed_change_set(
        tmp_path, kanban_conn, "symlink",
    )
    # Reset the candidate commit and replace one allowed path with a symlink.
    subprocess.run(["git", "reset", "--hard", base], cwd=repo, check=True, capture_output=True)
    (repo / "src" / "app.py").unlink()
    (repo / "src" / "app.py").symlink_to("../../outside")
    _commit(repo, "symlink candidate")
    with pytest.raises(artifacts.ArtifactValidationError, match="symlink"):
        artifacts.rederive_change_set(
            repo, base_sha=base, allowed_paths=["src/*.py", "old.py", "new.py"],
        )


def test_v2_human_decision_reopens_exact_artifacts_and_candidate_identity(
    tmp_path, kanban_conn,
):
    repo, _base, head, run_id, document = _seed_change_set(
        tmp_path, kanban_conn, "bound-decision",
    )
    change_set = _seal(repo, "bound-decision", run_id, document)
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='done',output_revision=2,producer_run_id=? "
            "WHERE instance_id='bound-decision' AND step_id='build'",
            (run_id,),
        )
        approval = dict(db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id='bound-decision' "
            "AND step_id='approval'",
        ).fetchone())
        recipe = load_library(tmp_path / "library", persist=False).get(
            "change-fixture@1"
        ).document
        vector = revision_vector(db, "bound-decision", approval, recipe)
        db.execute(
            "UPDATE recipe_steps SET state='waiting',input_revision_hash=? "
            "WHERE instance_id='bound-decision' AND step_id='approval'",
            (vector,),
        )
        binding = decisions.current_binding(db, "bound-decision", "approval")
    assert binding["revision_hash"] == vector
    assert binding["change_set_sha256"] == change_set["sha256"]
    assert binding["candidate_commit_sha"] == head
    assert binding["candidate_tree_sha"] == document["tree_sha"]

    token = decisions.issue_phone_token(
        instance_id="bound-decision", step_id="approval", activation=1,
        revision_hash=vector, evidence_bundle_hash=None, decision="approve",
        nonce="exact-binding",
    )
    encoded = token.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert payload["policy_hash"]
    assert payload["change_set_sha256"] == change_set["sha256"]
    assert payload["candidate_commit_sha"] == head
    assert payload["candidate_tree_sha"] == document["tree_sha"]

    Path(change_set["sealed_path"]).write_text("{}", encoding="utf-8")
    with store._connect() as db:
        with pytest.raises(decisions.DecisionConflict, match="inputs cannot be verified"):
            decisions.current_binding(db, "bound-decision", "approval")


@pytest.mark.parametrize(
    "scenario", ["independent", "rework-after-notification"],
)
def test_full_dev_pipeline_6_fixture_parks_at_human_approval_with_no_delivery(
    tmp_path, kanban_conn, monkeypatch, scenario,
):
    from hermes_cli import kanban_db, profiles as hermes_profiles
    from shipfactory import verification
    from shipfactory.recipes import advancer

    reconcile = advancer.reconcile

    monkeypatch.setattr(hermes_profiles, "profile_exists", lambda _name: True)
    factory_home = Path(os.environ["HERMES_HOME"]) / "shipfactory"
    factory_home.mkdir(parents=True, exist_ok=True)
    architect_executor = "hermes"
    (factory_home / "seats.yaml").write_text(
        f"""company: shipfactory-test
seats:
  explorer: {{profile: explore, executor: codex, model: build, role: researcher}}
  dev-backend: {{profile: build, executor: codex, model: build, role: engineer}}
  verifier: {{profile: verify, executor: claude, model: review, role: qa}}
  architect: {{profile: attack, executor: {architect_executor}, model: adversarial, role: security}}
  operator: {{profile: operator, executor: hermes, model: human, role: general}}
hierarchy_gates: {{}}
""",
        encoding="utf-8",
    )
    profiles = {
        "planning": {"max_runtime_seconds": 10, "max_retries": 1, "token_allowance": 1000},
        "build": {"max_runtime_seconds": 10, "max_retries": 1, "token_allowance": 1000},
        "review": {"max_runtime_seconds": 10, "max_retries": 1, "token_allowance": 1000},
    }
    verify_profile = {
        "max_runtime_seconds": 10, "infrastructure_retries": 1,
        "max_evidence_bytes": 100_000, "max_log_bytes": 50_000,
        "capture_video": False, "capture_trace": False, "capture_har": False,
        "browser_slots": 1, "surface": "stricter",
    }
    recipe = load_library(ROOT / "recipes").get("dev-pipeline@6")
    definitions = {step["id"]: step for step in recipe.document["steps"]}
    repo, base = _repo(tmp_path)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    instantiate(
        kanban_conn, board="test", recipe=recipe,
        parameters={"request": "Update the deterministic value."},
        instance_id="pipeline-v6-e2e", base_sha=base,
    )

    def latest(step_id: str) -> dict:
        with store._connect() as db:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id='pipeline-v6-e2e' "
                "AND step_id=? ORDER BY activation DESC LIMIT 1", (step_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def write_output(name: str, document: dict) -> None:
        target = repo / ".shipfactory-output" / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")

    def approve_review(step_id: str) -> None:
        step = latest(step_id)
        assert step["state"] == "running"
        # The review task was manually driven in this fixture, so seed the
        # same durable successful run that a real worker/reaper would leave
        # before reconcile evaluates its verdict.  Provider independence must
        # be tested from this run identity, not from seats.yaml.
        reviewer_executor = "hermes" if step_id == "adversarial-review" else "claude"
        reviewer_run_id = store.record_run_start(
            step["kanban_task_id"], definitions[step_id]["params"]["seat"],
            reviewer_executor, "review-model", workspace_path=repo,
            provider=reviewer_executor, recipe_activation=int(step["activation"]),
        )
        store.record_run_end(reviewer_run_id, 0, 1, 1, 0.1, "done")
        verdict = {"outcome": "approve", "body": "APPROVE clean pass; no findings"}
        assert kanban_db.complete_task(
            kanban_conn, step["kanban_task_id"],
            result="SHIPFACTORY_VERDICT: " + json.dumps(verdict, separators=(",", ":")),
        )
        reconcile(
            kanban_conn, "pipeline-v6-e2e", profiles=profiles,
            verification_profiles={"browser-standard": verify_profile},
        )

    reconcile(kanban_conn, "pipeline-v6-e2e", profiles=profiles)
    exploration_document = {
        "schema": "shipfactory.exploration/v1",
        "intent_sha256": hashlib.sha256(b"Update the deterministic value.").hexdigest(),
        "base_sha": base, "repo_tree_sha": tree, "references": [],
        "direct_callers": [], "constraints": [], "untrusted_directives": [],
        "unknowns": [],
    }
    write_output("exploration.json", exploration_document)
    exploration = artifacts.seal_artifact(
        instance_id="pipeline-v6-e2e", step_id="explore", activation=1, run_id=101,
        output=definitions["explore"]["outputs"][0], workspace=repo, producer="run:101",
    )
    assert kanban_db.complete_task(
        kanban_conn, latest("explore")["kanban_task_id"], result="explored",
    )
    reconcile(kanban_conn, "pipeline-v6-e2e", profiles=profiles)

    spec_document = {
        "schema": "shipfactory.task-spec/v1", "intent_artifact_id": exploration["id"],
        "problem": "Update the deterministic value.", "non_goals": [],
        "requirements": [{
            "id": "REQ-1", "behavior": "The value is updated.",
            "oracle": "The protected unit-suite passes.", "risk": "logic",
        }],
        "target_files": ["src/app.py"], "forbidden_paths": [], "risk_tags": [],
        "acceptance_cases": ["unit-suite"], "rollback_notes": "Revert the commit.",
        "assumptions": [], "clarifications": [],
    }
    write_output("spec.json", spec_document)
    spec = artifacts.seal_artifact(
        instance_id="pipeline-v6-e2e", step_id="spec-draft", activation=1, run_id=102,
        output=definitions["spec-draft"]["outputs"][0], workspace=repo,
        producer="run:102",
    )
    assert kanban_db.complete_task(
        kanban_conn, latest("spec-draft")["kanban_task_id"], result="specified",
    )
    reconcile(kanban_conn, "pipeline-v6-e2e", profiles=profiles)
    approve_review("spec-attack")

    plan_document = {
        "schema": "shipfactory.plan/v1", "task_spec_sha256": spec["sha256"],
        "base_sha": base,
        "nodes": [{
            "id": "build-value", "title": "Build the value", "needs": [],
            "kind": "logic", "requirements": ["REQ-1"],
            "allowed_paths": ["src/app.py"], "expected_outputs": ["change-set"],
            "test_cases": ["TEST-REQ-1-unit-suite"], "risk_tags": [],
        }],
        "integration_order": ["build-value"], "shared_file_overlaps": [],
        "residual_risks": [],
    }
    write_output("plan.json", plan_document)
    plan = artifacts.seal_artifact(
        instance_id="pipeline-v6-e2e", step_id="plan-draft", activation=1, run_id=103,
        output=definitions["plan-draft"]["outputs"][0], workspace=repo,
        producer="run:103",
    )
    assert kanban_db.complete_task(
        kanban_conn, latest("plan-draft")["kanban_task_id"], result="planned",
    )
    reconcile(kanban_conn, "pipeline-v6-e2e", profiles=profiles)
    approve_review("plan-attack")

    build = latest("build")
    kanban_conn.execute(
        "UPDATE tasks SET workspace_path=? WHERE id=?", (str(repo), build["kanban_task_id"]),
    )
    kanban_conn.commit()
    run_id = store.record_run_start(
        build["kanban_task_id"], "dev-backend", "codex", "build",
        workspace_path=repo, recipe_activation=1,
    )
    (repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    head = _commit(repo, "update value")
    change_document = artifacts.rederive_change_set(
        repo, base_sha=base, allowed_paths=["src/app.py"],
    )
    write_output("change-set.json", change_document)
    change_set = artifacts.seal_artifact(
        instance_id="pipeline-v6-e2e", step_id="build", activation=1, run_id=run_id,
        output=definitions["build"]["outputs"][0], workspace=repo,
        producer=f"run:{run_id}",
    )
    store.record_run_end(run_id, 0, 1, 1, 0.1, "done")
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET producer_run_id=? WHERE instance_id='pipeline-v6-e2e' "
            "AND step_id='build' AND activation=1", (run_id,),
        )
    assert kanban_db.complete_task(
        kanban_conn, build["kanban_task_id"], result="built exact change-set",
    )
    reconcile(
        kanban_conn, "pipeline-v6-e2e", profiles=profiles,
        verification_profiles={"browser-standard": verify_profile},
    )
    verify_step = latest("verify-runtime")
    assert verify_step["state"] == "running_verification"

    manifest = verification.load_verification_manifest(repo, head)
    protected = verification.load_verification_manifest(
        repo, base, verify_worktree_copy=False,
    )

    def trusted_driver(_case, _workspace, _env, _timeout):
        now = store._now()
        return {
            "classification": "passed", "stdout": b"trusted pass", "stderr": b"",
            "exit_code": 0, "started_at": now, "ended_at": now,
        }

    bundle = verification.run_verification(
        instance_id="pipeline-v6-e2e", step_id="verify-runtime", activation=1,
        input_revision_hash=verify_step["input_artifact_set_hash"], base_sha=base,
        head_sha=head, tree_sha=change_document["tree_sha"], workspace=repo,
        manifest=manifest, protected_manifest=protected, profile=verify_profile,
        drivers={"command": trusted_driver},
        workspace_owner_task_id=build["kanban_task_id"],
        workspace_owner_activation=1, workspace_owner_run_id=run_id,
        required_surface="stricter",
    )
    assert bundle["state"] == "done"
    reconcile(
        kanban_conn, "pipeline-v6-e2e", profiles=profiles,
        verification_profiles={"browser-standard": verify_profile},
    )
    approve_review("correctness-review")
    approve_review("adversarial-review")
    story_step = latest("review-story")
    assert story_step["state"] == "running"
    story_document = {
        "schema": "shipfactory.review-story/v1", "instance_id": "pipeline-v6-e2e",
        "revision_hash": story_step["input_artifact_set_hash"],
        "task_spec_sha256": spec["sha256"], "plan_sha256": plan["sha256"],
        "change_set_sha256": change_set["sha256"],
        "evidence_bundle_sha256": bundle["bundle_sha256"],
        "headline": "Update the deterministic value",
        "changes": [{
            "importance": 1, "requirement_ids": ["REQ-1"], "files": ["src/app.py"],
            "why": "Implements the requested deterministic value.", "risk": "logic",
            "evidence_case_ids": ["unit-suite"],
        }],
        "generated_or_mechanical_files": [], "not_changed": [], "residual_risks": [],
    }
    if scenario == "independent":
        # A newer-looking sealed row that is not the story activation's declared
        # build input must never be substituted by an instance-wide "latest"
        # query. The valid story below remains bound to change_set_sha256.
        foreign_document = dict(change_document)
        foreign_document.update({
            "head_sha": "f" * 40, "tree_sha": "e" * 40, "commits": ["f" * 40],
        })
        foreign_bytes = json.dumps(
            foreign_document, sort_keys=True, separators=(",", ":"),
        ).encode()
        foreign_path = tmp_path / "foreign-latest-change-set.json"
        foreign_path.write_bytes(foreign_bytes)
        with store._connect() as db:
            db.execute(
                "INSERT INTO artifacts(id,instance_id,step_id,activation,kind,schema_version,"
                "state,candidate_path,sealed_path,sha256,size_bytes,producer,base_sha,head_sha,"
                "repo_tree_sha,created_at,sealed_at) "
                "VALUES(?,?,'foreign-build',99,'change-set',1,'sealed',?,?,?,?,'test',?,?,?,?,?)",
                (
                    artifacts.artifact_id("pipeline-v6-e2e", "foreign-build", 99, "change-set"),
                    "pipeline-v6-e2e", ".shipfactory-output/change-set.json",
                    str(foreign_path), hashlib.sha256(foreign_bytes).hexdigest(),
                    len(foreign_bytes), base, "f" * 40, "e" * 40,
                    store._now(), store._now(),
                ),
            )
    write_output("review-story.json", story_document)
    story = artifacts.seal_artifact(
        instance_id="pipeline-v6-e2e", step_id="review-story", activation=1,
        run_id=204, output=definitions["review-story"]["outputs"][0],
        workspace=repo, producer="run:204",
    )
    assert kanban_db.complete_task(
        kanban_conn, story_step["kanban_task_id"], result="story complete",
    )
    reconcile(
        kanban_conn, "pipeline-v6-e2e", profiles=profiles,
        verification_profiles={"browser-standard": verify_profile},
    )

    assert latest("correctness-review")["state"] == "done"
    assert latest("adversarial-review")["state"] == "done"
    assert latest("approval")["state"] == "waiting"
    assert latest("notify")["state"] == "pending"
    with store._connect() as db:
        cases = {
            row["case_id"] for row in db.execute(
                "SELECT case_id FROM verification_cases WHERE bundle_id=?", (bundle["id"],),
            ).fetchall()
        }
        binding = decisions.current_binding(db, "pipeline-v6-e2e", "approval")
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert {"unit-suite", "protected:unit-suite"} <= cases
    assert artifacts.artifact_document(story)["headline"] == story_document["headline"]
    assert binding["task_spec_sha256"] == spec["sha256"]
    assert binding["plan_sha256"] == plan["sha256"]
    assert binding["change_set_sha256"] == change_set["sha256"]
    assert binding["review_story_sha256"] == story["sha256"]
    assert binding["evidence_bundle_hash"] == bundle["bundle_sha256"]
    assert binding["candidate_commit_sha"] == head
    if scenario == "rework-after-notification":
        token = decisions.issue_phone_token(
            instance_id="pipeline-v6-e2e", step_id="approval", activation=1,
            revision_hash=binding["revision_hash"],
            evidence_bundle_hash=binding["evidence_bundle_hash"],
            decision="approve", nonce="notified-old-revision",
        )
        with store._connect() as db:
            instance = dict(db.execute(
                "SELECT * FROM recipe_instances WHERE id='pipeline-v6-e2e'",
            ).fetchone())
            advancer._invalidate_cone(
                db, instance, recipe.document, "build", "correctness-review",
                "test:post-notification-rework",
            )
        with pytest.raises(decisions.DecisionConflict, match="activation"):
            decisions.consume_phone_token(
                token, actor_kind="operator", actor_id="operator",
                channel="notification",
            )
        for step_id in (
            "build", "verify-runtime", "correctness-review", "adversarial-review",
            "review-story", "approval", "notify",
        ):
            refreshed = latest(step_id)
            assert refreshed["activation"] == 2 and refreshed["state"] == "pending"
