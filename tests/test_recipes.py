"""Recipe v2 regression coverage against the real Hermes kanban database."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import pytest

from shipfactory import store
from shipfactory.cli import _recipe_gate, _reroute
from shipfactory.config import FactoryConfig
from shipfactory.recipes.advancer import (
    advance_key,
    apply_events,
    cancel,
    event,
    reconcile,
    reconcile_root_collectors,
    startup_guard,
)
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import RecipeError, load_library


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}


def _recipe(tmp_path: Path, text: str, key: str):
    library_path = tmp_path / ("library-" + key.replace("@", "-"))
    library_path.mkdir()
    (library_path / f"{key}.yaml").write_text(text, encoding="utf-8")
    return load_library(library_path).get(key)


def _step(instance_id: str, step_id: str, activation: int | None = None) -> dict:
    with store._connect() as db:
        if activation is None:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? "
                "ORDER BY activation DESC LIMIT 1",
                (instance_id, step_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM recipe_steps WHERE instance_id=? AND step_id=? AND activation=?",
                (instance_id, step_id, activation),
            ).fetchone()
    assert row is not None
    return dict(row)


def _instance(instance_id: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM recipe_instances WHERE id=?", (instance_id,)).fetchone()
    assert row is not None
    return dict(row)


def _complete_review(kanban_db, conn, task_id: str, *, outcome: str, target: str | None = None) -> None:
    verdict = {"outcome": outcome}
    if target is not None:
        verdict["target_step"] = target
        verdict["body"] = "factory/recipes/advancer.py:84 requires changes"
    else:
        verdict["body"] = "APPROVE clean pass"
    result = "SHIPFACTORY_VERDICT: " + json.dumps(verdict, separators=(",", ":"))
    assert kanban_db.complete_task(conn, task_id, result=result, summary="reviewed")


def _one_step_recipe(tmp_path: Path, key: str = "single@1"):
    recipe_id, version = key.split("@")
    return _recipe(
        tmp_path,
        f"""schema: shipfactory.recipe/v1
id: {recipe_id}
version: {version}
status: active
description: one real worker task
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 1, max_tokens: 50000}}
steps:
  - id: work
    primitive: agent_task
    title: Do work
    needs: []
    optional: false
    params: {{seat: dev-backend, instructions: do it, execution_profile: standard, workspace: worktree}}
""",
        key,
    )


def _selection_with_siblings(conn, tmp_path: Path):
    from hermes_cli import kanban_db

    store.init_db()
    recipe = _one_step_recipe(tmp_path)
    root = kanban_db.create_blocked_task(
        conn,
        title="Triage root collector",
        body="wait for all siblings",
        block_kind="needs_input",
        reason="recipe_root_collector",
    )
    left = instantiate(conn, board="test", recipe=recipe, parameters={}, instance_id="left")
    right = instantiate(conn, board="test", recipe=recipe, parameters={}, instance_id="right")
    kanban_db.link_tasks(conn, left["collector_task_id"], root)
    kanban_db.link_tasks(conn, right["collector_task_id"], root)
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO triage_selections(id,source_task_id,board,ranked_json,outcome,"
            "root_collector_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("selection", root, "test", "[]", "selected", root, now, now),
        )
    reconcile(conn, "left", profiles=PROFILES)
    reconcile(conn, "right", profiles=PROFILES)
    return root, left, right


def test_loader_persists_immutable_normalized_recipe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = tmp_path / "r.yaml"
    source.write_text("""schema: shipfactory.recipe/v1
id: test
version: 1
status: active
description: test
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 1, max_step_activations: 1, max_tokens: 1}
steps:
 - id: work
   primitive: notify
   title: n
   needs: []
   optional: false
   params: {target: x, message: y}
""")
    library = load_library(tmp_path)
    assert library.get("test@1").hash
    source.write_text(source.read_text().replace("description: test", "description: changed"))
    with pytest.raises(RecipeError, match="immutable"):
        load_library(tmp_path)


def _legacy_recipe_source() -> dict:
    from shipfactory.recipes.loader import validate

    source = {
        "schema": "shipfactory.recipe/v1",
        "id": "legacy-compatible",
        "version": 1,
        "status": "active",
        "description": "unchanged policy",
        "intent_tags": ["test"],
        "supersedes": None,
        "parameters": {},
        "budgets": {"max_activations": 1, "max_step_activations": 1, "max_tokens": 1},
        "steps": [{
            "id": "build",
            "primitive": "agent_task",
            "title": "build",
            "needs": [],
            "optional": False,
            "params": {
                "seat": "builder",
                "instructions": "Build the change.",
                "execution_profile": "standard",
                "workspace": "worktree",
            },
        }, {
            "id": "review",
            "primitive": "review_gate",
            "title": "review",
            "needs": ["build"],
            "optional": False,
            "params": {
                "seat": "verifier",
                "instructions": "Finish with SHIPFACTORY_VERDICT JSON.",
                "execution_profile": "standard",
                "workspace": "worktree",
            },
        }],
    }
    resolved = validate(json.loads(json.dumps(source)))
    persisted = json.loads(json.dumps(resolved))
    persisted["schema"] = "factory.recipe/v1"
    persisted["steps"][1]["params"]["instructions"] = (
        "Finish with FACTORY_VERDICT JSON."
    )
    return {"source": source, "persisted": persisted}


def _seed_legacy_publication(tmp_path, monkeypatch, source, persisted):
    from shipfactory.recipes.loader import _canonical

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    library_path = tmp_path / "library"
    library_path.mkdir()
    normalized = _canonical(persisted)
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_versions(id,version,hash,status,normalized_yaml,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("legacy-compatible", 1, digest, "active", normalized, store._now()),
        )
    (library_path / "legacy-compatible@1.yaml").write_text(
        json.dumps(source), encoding="utf-8",
    )
    return library_path, digest, normalized


def test_loader_accepts_only_known_namespace_migrations_without_republishing(
    tmp_path, monkeypatch,
):
    fixture = _legacy_recipe_source()
    source, persisted = fixture["source"], fixture["persisted"]
    library_path, digest, normalized = _seed_legacy_publication(
        tmp_path, monkeypatch, source, persisted,
    )

    recipe = load_library(library_path).get("legacy-compatible@1")

    assert recipe.hash == digest
    assert recipe.document == persisted
    with store._connect() as db:
        row = db.execute(
            "SELECT hash,normalized_yaml FROM recipe_versions "
            "WHERE id='legacy-compatible' AND version=1",
        ).fetchone()
    assert row["hash"] == digest
    assert row["normalized_yaml"] == normalized


def test_loader_rejects_legacy_row_when_stored_hash_does_not_match_bytes(
    tmp_path, monkeypatch,
):
    fixture = _legacy_recipe_source()
    library_path, _digest, _normalized = _seed_legacy_publication(
        tmp_path, monkeypatch, fixture["source"], fixture["persisted"],
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_versions SET hash=? WHERE id='legacy-compatible' AND version=1",
            ("0" * 64,),
        )
    with pytest.raises(RecipeError, match="stored hash"):
        load_library(library_path)


@pytest.mark.parametrize("persisted", [
    ["not", "a", "recipe"],
    {"schema": "factory.recipe/v1", "steps": None},
    {"schema": "factory.recipe/v1", "steps": [None]},
])
def test_loader_rejects_hash_consistent_non_document_legacy_row_cleanly(
    tmp_path, monkeypatch, persisted,
):
    source = _legacy_recipe_source()["source"]
    library_path, _digest, _normalized = _seed_legacy_publication(
        tmp_path, monkeypatch, source, persisted,
    )
    with pytest.raises(RecipeError, match="policy bytes are invalid"):
        load_library(library_path)


def test_loader_rejects_partial_namespace_migration(
    tmp_path, monkeypatch,
):
    fixture = _legacy_recipe_source()
    source, persisted = fixture["source"], fixture["persisted"]
    source["steps"][1]["params"]["instructions"] = (
        "Finish with FACTORY_VERDICT JSON."
    )
    library_path, _digest, _normalized = _seed_legacy_publication(
        tmp_path, monkeypatch, source, persisted,
    )
    with pytest.raises(RecipeError, match="immutable"):
        load_library(library_path)


def test_loader_does_not_alias_factory_verdict_inside_larger_token(
    tmp_path, monkeypatch,
):
    fixture = _legacy_recipe_source()
    source, persisted = fixture["source"], fixture["persisted"]
    source["steps"][1]["params"]["instructions"] = (
        "Finish with mySHIPFACTORY_VERDICT JSON."
    )
    persisted["steps"][1]["params"]["instructions"] = (
        "Finish with myFACTORY_VERDICT JSON."
    )
    library_path, _digest, _normalized = _seed_legacy_publication(
        tmp_path, monkeypatch, source, persisted,
    )
    with pytest.raises(RecipeError, match="immutable"):
        load_library(library_path)


def test_loader_rejects_legacy_schema_in_current_source(
    tmp_path, monkeypatch,
):
    fixture = _legacy_recipe_source()
    source, persisted = fixture["source"], fixture["persisted"]
    source["schema"] = "factory.recipe/v1"
    library_path, _digest, _normalized = _seed_legacy_publication(
        tmp_path, monkeypatch, source, persisted,
    )
    with pytest.raises(RecipeError, match="unsupported recipe schema"):
        load_library(library_path)


def _activate_output_task(kanban_conn, output):
    from hermes_cli import kanban_db
    from shipfactory.recipes.primitives import activate

    instance = {"id": "output-contract", "recipe_hash": "a" * 64, "board": "test"}
    definition = {
        "id": "produce", "primitive": "agent_task", "title": "Produce",
        "needs": [], "optional": False, "inputs": [], "outputs": [output],
        "params": {
            "seat": "builder", "instructions": "Produce the declared output.",
            "execution_profile": "standard", "workspace": "worktree",
            "access_mode": "workspace_write", "environment": "source",
        },
    }
    task_id = activate(
        kanban_conn, instance, {"schema": "shipfactory.recipe/v2"}, definition,
        {"step_id": "produce", "activation": 1}, {}, [],
    )
    return kanban_db.get_task(kanban_conn, task_id)


def test_worker_authored_v2_output_contract_names_exact_path_and_schema(kanban_conn):
    task = _activate_output_task(kanban_conn, {
        "kind": "exploration", "schema": "shipfactory.exploration/v1",
        "path": ".shipfactory-output/exploration.json",
    })
    assert "Factory output contract" in task.body
    assert ".shipfactory-output/exploration.json" in task.body
    assert "shipfactory.exploration/v1" in task.body
    assert "Write the artifact" in task.body


@pytest.mark.parametrize(("kind", "schema", "fragments"), [
    ("exploration", "shipfactory.exploration/v1", [
        '"intent_sha256"', '"repo_tree_sha"', '"references"', '"direct_callers"',
        '"untrusted_directives"', '"unknowns"', '"git_blob_sha"', '"start_line"',
        '"text_sha256"',
    ]),
    ("task-spec", "shipfactory.task-spec/v1", [
        '"intent_artifact_id"', '"problem"', '"non_goals"', '"requirements"',
        '"target_files"', '"forbidden_paths"', '"acceptance_cases"',
        '"rollback_notes"', '"clarifications"', '"behavior"', '"oracle"',
    ]),
    ("plan", "shipfactory.plan/v1", [
        '"task_spec_sha256"', '"nodes"', '"integration_order"',
        '"shared_file_overlaps"', '"residual_risks"', '"allowed_paths"',
        '"expected_outputs"', '"test_cases"',
    ]),
    ("review-story", "shipfactory.review-story/v1", [
        '"instance_id"', '"revision_hash"', '"task_spec_sha256"',
        '"plan_sha256"', '"change_set_sha256"', '"evidence_bundle_sha256"',
        '"headline"', '"changes"', '"generated_or_mechanical_files"',
        '"not_changed"', '"residual_risks"', '"evidence_case_ids"',
    ]),
])
def test_worker_output_contract_exposes_complete_schema_template(
    kanban_conn, kind, schema, fragments,
):
    task = _activate_output_task(kanban_conn, {
        "kind": kind, "schema": schema,
        "path": f".shipfactory-output/{kind}.json",
    })
    assert "Exact JSON template" in task.body
    assert "Do not add other top-level fields" in task.body
    for fragment in fragments:
        assert fragment in task.body


def test_unknown_worker_output_schema_fails_activation(kanban_conn):
    with pytest.raises(ValueError, match="no worker output contract"):
        _activate_output_task(kanban_conn, {
            "kind": "mystery", "schema": "shipfactory.mystery/v1",
            "path": ".shipfactory-output/mystery.json",
        })
    assert kanban_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_worker_output_templates_share_every_nested_validator_key_set():
    from shipfactory.artifact_contracts import (
        EXPLORATION_EXISTING_REFERENCE_KEYS,
        EXPLORATION_PROPOSED_REFERENCE_KEYS,
        PLAN_NODE_KEYS,
        REVIEW_STORY_CHANGE_KEYS,
        TASK_SPEC_REQUIREMENT_KEYS,
        artifact_output_template,
    )

    exploration = artifact_output_template("shipfactory.exploration/v1")
    spec = artifact_output_template("shipfactory.task-spec/v1")
    plan = artifact_output_template("shipfactory.plan/v1")
    story = artifact_output_template("shipfactory.review-story/v1")
    assert set(exploration["references"][0]) == EXPLORATION_EXISTING_REFERENCE_KEYS
    assert set(exploration["references"][1]) == EXPLORATION_PROPOSED_REFERENCE_KEYS
    assert set(spec["requirements"][0]) == TASK_SPEC_REQUIREMENT_KEYS
    assert set(plan["nodes"][0]) == PLAN_NODE_KEYS
    assert set(story["changes"][0]) == REVIEW_STORY_CHANGE_KEYS


@pytest.mark.parametrize(("kind", "schema"), [
    ("exploration", "shipfactory.exploration/v1"),
    ("task-spec", "shipfactory.task-spec/v1"),
    ("plan", "shipfactory.plan/v1"),
    ("review-story", "shipfactory.review-story/v1"),
])
def test_unmodified_worker_output_template_is_rejected(kind, schema):
    from shipfactory.artifact_contracts import artifact_output_template
    from shipfactory.artifacts import ArtifactValidationError, _validate_document

    with pytest.raises(ArtifactValidationError, match="unresolved Factory output-contract"):
        _validate_document(artifact_output_template(schema), kind=kind, schema=schema)


def test_placeholder_guard_does_not_reject_arbitrary_angle_bracket_text():
    from shipfactory.artifacts import _validate_document

    document = {
        "schema": "shipfactory.task-spec/v1",
        "intent_artifact_id": "0" * 64,
        "problem": "Render a literal <button> element.",
        "non_goals": [],
        "requirements": [],
        "target_files": [],
        "forbidden_paths": [],
        "risk_tags": [],
        "acceptance_cases": [],
        "rollback_notes": "Revert the change.",
        "assumptions": [],
        "clarifications": [],
    }
    assert _validate_document(
        document, kind="task-spec", schema="shipfactory.task-spec/v1",
    ) == 1


@pytest.mark.parametrize(("kind", "schema", "rules"), [
    ("exploration", "shipfactory.exploration/v1", [
        "repo_tree_sha its exact tree", "line range must be valid",
        "symbol reference id must appear verbatim", "generated tracked path",
    ]),
    ("task-spec", "shipfactory.task-spec/v1", [
        "unique REQ-N", "exact sealed exploration artifact id",
        "checks its format, not a separate context binding", "rollback_notes are nonempty",
    ]),
    ("plan", "shipfactory.plan/v1", [
        "all task-spec requirements must be covered", "Every test_cases entry",
        "Every overlap", "control-plane or high-risk", "remaining instance and pool budgets",
    ]),
    ("review-story", "shipfactory.review-story/v1", [
        "exactly match the Factory-opened producer inputs",
        "may name only ids that exist in the task-spec",
        "Every task-spec requirement", "at least one existing evidence case id",
        "Every real changed path must appear exactly once", "retries, skips, or warnings",
    ]),
])
def test_worker_output_contract_exposes_semantic_rejection_rules(
    kanban_conn, kind, schema, rules,
):
    task = _activate_output_task(kanban_conn, {
        "kind": kind, "schema": schema,
        "path": f".shipfactory-output/{kind}.json",
    })
    for rule in rules:
        assert rule in task.body


def test_factory_generated_change_set_contract_says_worker_must_not_write(kanban_conn):
    task = _activate_output_task(kanban_conn, {
        "kind": "change-set", "schema": "shipfactory.change-set/v1",
        "path": ".shipfactory-output/change-set.json",
    })
    assert "Factory generates this artifact after your successful exit" in task.body
    assert "Do not write" in task.body
    assert ".shipfactory-output/change-set.json" in task.body


def _activate_review_task(kanban_conn, *, change_set_input: bool = False):
    from shipfactory.recipes.primitives import activate

    draft = {"id": "draft", "primitive": "agent_task", "needs": []}
    build = {"id": "build", "primitive": "agent_task", "needs": ["draft"]}
    inputs = ([{"from": "build", "kind": "change-set"}]
              if change_set_input else [{"from": "draft", "kind": "task-spec"}])
    review = {
        "id": "review",
        "primitive": "review_gate",
        "needs": ["build" if change_set_input else "draft"],
        "title": "Review",
        "inputs": inputs,
        "params": {
            "seat": "reviewer", "workspace": "worktree",
            "instructions": "Review and emit a verdict.",
        },
    }
    recipe = {"schema": "shipfactory.recipe/v2", "steps": [draft, build, review]}
    task_id = activate(
        kanban_conn,
        {"id": "verdict-contract", "recipe_hash": "1" * 64},
        recipe,
        review,
        {"step_id": "review", "activation": 1},
        {},
        [],
    )
    return kanban_conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()[0]


def test_review_gate_exposes_complete_verdict_contract(kanban_conn):
    body = _activate_review_task(kanban_conn)

    assert 'SHIPFACTORY_VERDICT: {"outcome":"approve","body":"APPROVE - clean pass; no findings."}' in body
    assert 'SHIPFACTORY_VERDICT: {"outcome":"request_changes","target_step":"draft"' in body
    assert "Allowed request_changes target_step values: draft" in body
    assert "path/to/file.py:1" in body
    assert "single line before the mandatory SHIPFACTORY_RESULT" in body
    assert "Do not emit prose instead of this JSON" in body


def test_review_gate_change_set_input_restricts_target_to_exact_builder(kanban_conn):
    body = _activate_review_task(kanban_conn, change_set_input=True)

    assert "Allowed request_changes target_step values: build" in body
    assert '"target_step":"build"' in body
    assert "Allowed request_changes target_step values: draft" not in body


def test_review_verdict_contract_prefers_declared_agent_input_over_other_ancestors():
    from shipfactory.recipes.primitives import _review_verdict_contract

    recipe = {
        "steps": [
            {"id": "draft", "primitive": "agent_task", "needs": []},
            {"id": "build", "primitive": "agent_task", "needs": ["draft"]},
            {"id": "review", "primitive": "review_gate", "needs": ["build"]},
        ],
    }
    body = _review_verdict_contract(recipe, {
        "id": "review", "inputs": [{"from": "draft", "kind": "task-spec"}],
    })

    assert "Allowed request_changes target_step values: draft" in body
    assert "Allowed request_changes target_step values: build" not in body


def test_review_verdict_contract_fails_closed_without_a_valid_target():
    from shipfactory.recipes.primitives import _review_verdict_contract

    recipe = {"steps": [{"id": "review", "primitive": "review_gate", "needs": []}]}
    with pytest.raises(ValueError, match="no valid request_changes target"):
        _review_verdict_contract(recipe, {"id": "review", "inputs": []})


def test_review_verdict_examples_match_the_authoritative_parser():
    from shipfactory.recipes.primitives import parse_verdict

    approve = parse_verdict(
        'SHIPFACTORY_VERDICT: {"outcome":"approve","body":"APPROVE - clean pass; no findings."}'
    )
    request = parse_verdict(
        'SHIPFACTORY_VERDICT: {"outcome":"request_changes","target_step":"draft",'
        '"body":"test_message.py:5 - assertion conflicts with requested bytes"}'
    )

    assert approve == {"outcome": "approve", "body": "APPROVE - clean pass; no findings."}
    assert request["outcome"] == "request_changes"
    assert request["target_step"] == "draft"


def _derived_target_recipe(inputs=None):
    step = {
        "id": "review", "title": "Review", "primitive": "review_gate",
        "needs": ["draft"], "inputs": inputs or [
            {"from": "draft", "kind": "task-spec", "required": True},
        ],
        "params": {"seat": "reviewer", "workspace": "worktree"},
    }
    return {
        "schema": "shipfactory.recipe/v2",
        "steps": [
            {"id": "draft", "primitive": "agent_task", "needs": [],
             "params": {"seat": "writer", "workspace": "worktree"}},
            step,
        ],
    }, step


def test_review_verdict_derives_only_one_missing_factory_owned_target():
    from shipfactory.recipes.primitives import parse_verdict, parse_verdict_for_review

    recipe, step = _derived_target_recipe()
    result = (
        'SHIPFACTORY_VERDICT: {"outcome":"request_changes",'
        '"body":"README.md:1 requires changes"}'
    )
    with pytest.raises(ValueError, match="invalid request_changes verdict"):
        parse_verdict(result)
    assert parse_verdict_for_review(result, recipe, step) == {
        "outcome": "request_changes",
        "body": "README.md:1 requires changes",
        "target_step": "draft",
    }


def test_review_verdict_never_derives_ambiguous_or_extra_field_targets():
    from shipfactory.recipes.primitives import parse_verdict_for_review

    recipe, step = _derived_target_recipe(inputs=[
        {"from": "draft", "kind": "task-spec", "required": True},
        {"from": "build", "kind": "plan", "required": True},
    ])
    recipe["steps"].insert(1, {
        "id": "build", "primitive": "agent_task", "needs": ["draft"],
        "params": {"seat": "builder", "workspace": "worktree"},
    })
    step["needs"] = ["build"]
    missing = (
        'SHIPFACTORY_VERDICT: {"outcome":"request_changes",'
        '"body":"README.md:1 requires changes"}'
    )
    with pytest.raises(ValueError, match="invalid request_changes verdict"):
        parse_verdict_for_review(missing, recipe, step)

    recipe, step = _derived_target_recipe()
    extra = (
        'SHIPFACTORY_VERDICT: {"outcome":"request_changes",'
        '"body":"README.md:1 requires changes","note":"untrusted"}'
    )
    with pytest.raises(ValueError, match="invalid request_changes verdict"):
        parse_verdict_for_review(extra, recipe, step)


def _v2_marker_recipe(inputs=None):
    recipe, step = _derived_target_recipe(inputs=inputs)
    recipe["verdict_contract"] = "shipfactory.verdict/v2"
    return recipe, step


def _v2_verdict_line(**overrides):
    verdict = {
        "schema": "shipfactory.verdict/v2", "outcome": "request_changes",
        "clean": False, "target_step": "draft",
        "findings": [{
            "severity": "blocker", "location": "shipfactory/policy.py:16",
            "summary": "the citation vocabulary drifted",
        }],
        "summary": "one blocker remains",
    }
    verdict.update(overrides)
    for key in [key for key, value in overrides.items() if value is None]:
        del verdict[key]
    return "SHIPFACTORY_VERDICT: " + json.dumps(verdict, separators=(",", ":"))


def test_parse_verdict_v2_accepts_both_exact_shapes_and_synthesizes_body():
    from shipfactory.recipes.primitives import parse_verdict_v2

    recipe, step = _v2_marker_recipe()
    approve = parse_verdict_v2(_v2_verdict_line(
        outcome="approve", clean=True, findings=[], target_step=None,
        summary="Clean pass; no findings.",
    ), recipe, step)
    assert approve["outcome"] == "approve" and approve["clean"] is True
    assert approve["body"] == "Clean pass; no findings."

    request = parse_verdict_v2(_v2_verdict_line(), recipe, step)
    assert request["target_step"] == "draft" and request["clean"] is False
    assert request["body"] == (
        "one blocker remains\n"
        "BLOCKER shipfactory/policy.py:16 — the citation vocabulary drifted"
    )


@pytest.mark.parametrize("line", [
    "no verdict at all",
    "SHIPFACTORY_VERDICT: {not json}",
    _v2_verdict_line(schema=None),
    _v2_verdict_line(schema="shipfactory.verdict/v1"),
    _v2_verdict_line(note="unknown field"),
    _v2_verdict_line(clean=True),
    _v2_verdict_line(clean="false"),
    _v2_verdict_line(outcome="approve", clean=True),
    _v2_verdict_line(outcome="approve", clean=True, target_step=None),
    _v2_verdict_line(findings=[]),
    _v2_verdict_line(target_step=None),
    _v2_verdict_line(target_step="review"),
    _v2_verdict_line(summary="   "),
    _v2_verdict_line(findings=[{"severity": "blocker", "location": "a.py:1"}]),
    _v2_verdict_line(findings=[{"severity": "nit", "location": "a.py:1", "summary": "x"}]),
    _v2_verdict_line(findings=[{"severity": "blocker", "location": "a.py:1", "summary": " "}]),
])
def test_parse_verdict_v2_fails_closed_with_the_stable_prefix(line):
    from shipfactory.recipes.primitives import parse_verdict_v2

    recipe, step = _v2_marker_recipe()
    with pytest.raises(ValueError, match="^verdict_contract: "):
        parse_verdict_v2(line, recipe, step)


@pytest.mark.parametrize(("location", "valid"), [
    ("shipfactory/recipes/advancer.py:1049", True),
    ("a/b.py:1-9", True),
    ("Sources/App.swift:12", True),
    ("README.md:3", True),
    # finding #72 (first-light): any short alphanumeric extension is a real
    # repository file — a reviewer citing message.txt:1 must not be rejected.
    ("message.txt:1", True),
    (".shipfactory/verification.yaml:2-16", True),
    ("config.toml:7", True),
    ("a.py", False),
    ("a.py:1x", False),
    ("a.py:1-", False),
    ("README:3", False),
    ("no citation here", False),
    ("prose then a.py:1 then prose", False),
])
def test_parse_verdict_v2_finding_location_is_a_strict_fullmatch(location, valid):
    from shipfactory.recipes.primitives import parse_verdict_v2

    recipe, step = _v2_marker_recipe()
    line = _v2_verdict_line(findings=[{
        "severity": "warning", "location": location, "summary": "cited",
    }])
    if valid:
        assert parse_verdict_v2(line, recipe, step)["findings"][0]["location"] == location
    else:
        with pytest.raises(ValueError, match="finding location"):
            parse_verdict_v2(line, recipe, step)


def test_parse_verdict_v2_accepts_kanban_task_id_targets_for_downstream_mapping():
    from shipfactory.recipes.primitives import parse_verdict_v2

    recipe, step = _v2_marker_recipe()
    verdict = parse_verdict_v2(_v2_verdict_line(target_step="t_1082ec9b"), recipe, step)
    assert verdict["target_step"] == "t_1082ec9b"


def test_marker_recipes_require_v2_and_plain_recipes_reject_it():
    from shipfactory.recipes.primitives import parse_verdict_for_review

    recipe, step = _v2_marker_recipe()
    old_shape = (
        'SHIPFACTORY_VERDICT: {"outcome":"approve","body":"APPROVE - clean pass; no findings."}'
    )
    with pytest.raises(ValueError, match="^verdict_contract: "):
        parse_verdict_for_review(old_shape, recipe, step)
    # No omitted-target derivation exists under the marker: explicit or reject.
    with pytest.raises(ValueError, match="^verdict_contract: "):
        parse_verdict_for_review(_v2_verdict_line(target_step=None), recipe, step)

    plain_recipe, plain_step = _derived_target_recipe()
    with pytest.raises(ValueError, match="invalid review verdict"):
        parse_verdict_for_review(_v2_verdict_line(), plain_recipe, plain_step)


def test_review_gate_with_marker_exposes_the_v2_verdict_contract(kanban_conn):
    from shipfactory.recipes.primitives import activate, parse_verdict_v2

    draft = {"id": "draft", "primitive": "agent_task", "needs": []}
    review = {
        "id": "review", "primitive": "review_gate", "needs": ["draft"],
        "title": "Review",
        "inputs": [{"from": "draft", "kind": "task-spec"}],
        "params": {
            "seat": "reviewer", "workspace": "worktree",
            "instructions": "Review and emit a verdict.",
        },
    }
    recipe = {
        "schema": "shipfactory.recipe/v2",
        "verdict_contract": "shipfactory.verdict/v2",
        "steps": [draft, review],
    }
    task_id = activate(
        kanban_conn,
        {"id": "verdict-contract-v2", "recipe_hash": "1" * 64},
        recipe, review, {"step_id": "review", "activation": 1}, {}, [],
    )
    body = kanban_conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    assert "## Factory review verdict contract (shipfactory.verdict/v2)" in body
    assert '"schema":"shipfactory.verdict/v2"' in body
    assert "Allowed request_changes target_step values: draft" in body
    # Every rendered example must satisfy the authoritative v2 parser.
    import re as _re
    examples = _re.findall(r"`(SHIPFACTORY_VERDICT: [^`]+)`", body)
    assert len(examples) == 2
    outcomes = {parse_verdict_v2(example, recipe, review)["outcome"] for example in examples}
    assert outcomes == {"approve", "request_changes"}


def _marker_recipe_yaml(*, schema: str = "shipfactory.recipe/v2",
                        contract: str = "shipfactory.verdict/v2") -> str:
    v2 = schema == "shipfactory.recipe/v2"
    budgets = (
        "budgets:\n  max_activations: 4\n  max_tokens: 200000\n"
        "  step_activation_caps: {work: 2, check: 2}\n  token_pools: {standard: 200000}\n"
        if v2 else
        "budgets: {max_activations: 4, max_step_activations: 2, max_tokens: 200000}\n"
    )
    io_lines = "    inputs: []\n    outputs: []\n" if v2 else ""
    return (
        f"schema: {schema}\n"
        "id: marker\nversion: 1\nstatus: active\n"
        "description: verdict-contract marker recipe\nintent_tags: [test]\n"
        "supersedes: null\n"
        f"verdict_contract: {contract}\n"
        "parameters: {}\n"
        f"{budgets}"
        "steps:\n"
        "  - id: work\n    primitive: agent_task\n    title: Work\n    needs: []\n"
        f"    optional: false\n{io_lines}"
        "    params: {seat: dev-backend, instructions: work, execution_profile: standard, workspace: worktree}\n"
        "  - id: check\n    primitive: review_gate\n    title: Check\n    needs: [work]\n"
        f"    optional: false\n{io_lines}"
        "    params: {seat: verifier, instructions: check, execution_profile: standard, workspace: worktree}\n"
    )


def test_loader_accepts_the_exact_verdict_contract_marker_and_round_trips(tmp_path):
    store.init_db()
    library_path = tmp_path / "marker-library"
    library_path.mkdir()
    (library_path / "marker@1.yaml").write_text(_marker_recipe_yaml(), encoding="utf-8")
    recipe = load_library(library_path).get("marker@1")
    assert recipe.document["verdict_contract"] == "shipfactory.verdict/v2"
    # The pinned normalized document carries the marker immutably.
    reloaded = load_library(library_path).get("marker@1")
    assert reloaded.hash == recipe.hash
    with store._connect() as db:
        row = db.execute(
            "SELECT hash,normalized_yaml FROM recipe_versions WHERE id='marker' AND version=1"
        ).fetchone()
    assert row["hash"] == recipe.hash
    assert json.loads(row["normalized_yaml"])["verdict_contract"] == "shipfactory.verdict/v2"


def test_loader_rejects_the_marker_on_v1_wrong_values_and_other_extra_keys(tmp_path):
    with pytest.raises(RecipeError, match="verdict_contract must be exactly"):
        _recipe(tmp_path, _marker_recipe_yaml(schema="shipfactory.recipe/v1"), "marker-v1@1")
    with pytest.raises(RecipeError, match="verdict_contract must be exactly"):
        _recipe(
            tmp_path, _marker_recipe_yaml(contract="shipfactory.verdict/v1"),
            "marker-wrong@1",
        )
    other_key = _marker_recipe_yaml().replace("verdict_contract:", "verdict_extra:")
    with pytest.raises(RecipeError, match="top-level keys must exactly match"):
        _recipe(tmp_path, other_key, "marker-extra@1")


def test_restart_reconciliation_activates_review_once_after_swallowed_hook(tmp_path, kanban_conn):
    """§17.7: reconciliation reproduces the missing transition after restart."""
    from hermes_cli import kanban_db

    library = load_library(ROOT / "recipes")
    created = instantiate(
        kanban_conn,
        board="test",
        recipe=library.get("dev-pipeline@1"),
        parameters={"request": "harden restart handling"},
        instance_id="restart",
    )
    reconcile(kanban_conn, "restart", profiles=PROFILES)
    build = _step("restart", "build")
    assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result="built")

    # No completion event/hook is delivered: the daemon's restart pass alone advances.
    reconcile(kanban_conn, "restart", profiles=PROFILES)
    review = _step("restart", "review")
    assert review["state"] == "running"
    assert review["kanban_task_id"]
    key = advance_key("restart", library.get("dev-pipeline@1").hash, "review", 1, "running", "activate")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE key=?", (key,)).fetchone()[0] == 1

    reconcile(kanban_conn, "restart", profiles=PROFILES)
    assert _step("restart", "review")["kanban_task_id"] == review["kanban_task_id"]
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE ?",
        (f"recipe/restart/{library.get('dev-pipeline@1').hash}/review/%",),
    ).fetchone()[0] == 1
    assert kanban_db.get_task(kanban_conn, created["collector_task_id"]).status == "blocked"


def test_duplicate_event_is_one_durable_key_and_one_task_transition(tmp_path, kanban_conn):
    """§17.7: duplicate external and task-level advance keys are no-ops."""
    recipe = _one_step_recipe(tmp_path)
    created = instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="duplicate")
    first_key = event("duplicate", "work", {"id": "webhook-1", "type": "arrived"})
    assert first_key == event("duplicate", "work", {"id": "webhook-1", "type": "arrived"})

    reconcile(kanban_conn, "duplicate", profiles=PROFILES)
    task_id = _step("duplicate", "work")["kanban_task_id"]
    reconcile(kanban_conn, "duplicate", profiles=PROFILES)
    activation_key = advance_key("duplicate", recipe.hash, "work", 1, "running", "activate")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE key=?", (first_key,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM advance_events WHERE key=?", (activation_key,)).fetchone()[0] == 1
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key=?",
        (f"recipe/duplicate/{recipe.hash}/work/1",),
    ).fetchone()[0] == 1
    assert _step("duplicate", "work")["kanban_task_id"] == task_id
    assert created["collector_task_id"]


def test_gate_rejection_invalidates_cone_and_rebinds_approvals(tmp_path, kanban_conn):
    """§17.4: old review approvals cannot satisfy a new producer revision."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: shipfactory.recipe/v1
id: revision-cone
version: 1
status: active
description: revision-vector test
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 3, max_tokens: 500000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: qa
    primitive: review_gate
    title: QA
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: qa, execution_profile: standard, workspace: worktree}
  - id: review
    primitive: review_gate
    title: Final review
    needs: [qa]
    optional: false
    params: {seat: verifier, instructions: review, execution_profile: standard, workspace: worktree}
""",
        "revision-cone@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="revision")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    build1 = _step("revision", "build", 1)
    assert kanban_db.complete_task(kanban_conn, build1["kanban_task_id"], result="revision one")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    qa1 = _step("revision", "qa", 1)
    _complete_review(kanban_db, kanban_conn, qa1["kanban_task_id"], outcome="approve")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    review1 = _step("revision", "review", 1)
    _complete_review(kanban_db, kanban_conn, review1["kanban_task_id"], outcome="request_changes", target="build")
    reconcile(kanban_conn, "revision", profiles=PROFILES)

    build2 = _step("revision", "build", 2)
    assert build2["state"] == "running"
    assert build2["kanban_task_id"] != build1["kanban_task_id"]
    assert _step("revision", "qa", 1)["state"] == "done"
    assert _step("revision", "qa", 2)["state"] == "pending"
    assert _step("revision", "review", 2)["kanban_task_id"] is None

    assert kanban_db.complete_task(kanban_conn, build2["kanban_task_id"], result="revision two")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    qa2 = _step("revision", "qa", 2)
    assert qa2["state"] == "running"
    assert qa2["input_revision_hash"] != qa1["input_revision_hash"]
    assert _step("revision", "review", 2)["kanban_task_id"] is None
    _complete_review(kanban_db, kanban_conn, qa2["kanban_task_id"], outcome="approve")
    reconcile(kanban_conn, "revision", profiles=PROFILES)
    review2 = _step("revision", "review", 2)
    assert review2["state"] == "running"
    assert review2["input_revision_hash"] != review1["input_revision_hash"]
    assert _step("revision", "build", 1)["output_revision"] != _step("revision", "build", 2)["output_revision"]


def test_request_changes_persists_verdict_and_rework_provenance(tmp_path, kanban_conn):
    """Amendment A: rework causality is durable in shipfactory.db.

    RED control: before migration 15 the ``verdict_json``/``rejected_by_*``
    columns did not exist, so the rejecting verdict lived only in Hermes
    ``task.result`` and the gate->new-activation edge was only re-derivable by
    re-reading and re-parsing the kanban task.
    """
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: shipfactory.recipe/v1
id: provenance
version: 1
status: active
description: rework provenance test
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 3, max_tokens: 500000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: qa
    primitive: review_gate
    title: QA
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: qa, execution_profile: standard, workspace: worktree}
  - id: review
    primitive: review_gate
    title: Final review
    needs: [qa]
    optional: false
    params: {seat: verifier, instructions: review, execution_profile: standard, workspace: worktree}
""",
        "provenance@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="provenance")
    reconcile(kanban_conn, "provenance", profiles=PROFILES)
    assert kanban_db.complete_task(
        kanban_conn, _step("provenance", "build", 1)["kanban_task_id"], result="built",
    )
    reconcile(kanban_conn, "provenance", profiles=PROFILES)
    _complete_review(
        kanban_db, kanban_conn, _step("provenance", "qa", 1)["kanban_task_id"],
        outcome="approve",
    )
    reconcile(kanban_conn, "provenance", profiles=PROFILES)
    rejecting = "SHIPFACTORY_VERDICT: " + json.dumps({
        "outcome": "request_changes", "target_step": "build",
        "body": "BLOCKER factory/recipes/advancer.py:84 - build breaks the gate",
    }, separators=(",", ":"))
    assert kanban_db.complete_task(
        kanban_conn, _step("provenance", "review", 1)["kanban_task_id"],
        result=rejecting, summary="reviewed",
    )
    reconcile(kanban_conn, "provenance", profiles=PROFILES)

    # Approve outcomes persist their verdict too: the audit is complete.
    qa1 = _step("provenance", "qa", 1)
    assert json.loads(qa1["verdict_json"])["outcome"] == "approve"
    assert qa1["rejected_by_step_id"] is None and qa1["rejected_by_activation"] is None
    review1 = _step("provenance", "review", 1)
    verdict = json.loads(review1["verdict_json"])
    assert verdict["outcome"] == "request_changes"
    assert verdict["target_step"] == "build"
    assert review1["finding_count"] == 1
    # First activations were never sent to rework by anyone.
    assert _step("provenance", "build", 1)["rejected_by_step_id"] is None
    # Every cone activation records the rejecting gate attempt durably.
    for step_id in ("build", "qa", "review"):
        rework = _step("provenance", step_id, 2)
        assert rework["rejected_by_step_id"] == "review"
        assert rework["rejected_by_activation"] == 1
        assert rework["verdict_json"] is None

    # _fresh_activation heals a lost board task without inventing provenance:
    # its ``source`` string already records why, so the columns stay NULL.
    build2 = _step("provenance", "build", 2)
    assert build2["state"] == "running"
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET kanban_task_id='t_missing_provenance' "
            "WHERE instance_id='provenance' AND step_id='build' AND activation=2",
        )
    reconcile(kanban_conn, "provenance", profiles=PROFILES)
    healed = _step("provenance", "build", 3)
    assert healed["rejected_by_step_id"] is None
    assert healed["rejected_by_activation"] is None
    assert healed["verdict_json"] is None


def test_v1_run_cap_blocks_a_build_rework_loop(tmp_path, kanban_conn):
    """Count-only loop guard (finding #77): max_step_activations bounds a
    build<->review rework loop with no token budget. Two build runs are
    allowed; the third rework attempt is refused by the run cap."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: shipfactory.recipe/v1
id: run-cap-loop
version: 1
status: active
description: run-cap fuse example
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 2, max_tokens: 300000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: review
    primitive: review_gate
    title: Review
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: review, execution_profile: standard, workspace: worktree}
""",
        "run-cap-loop@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="runcap")
    reconcile(kanban_conn, "runcap", profiles=PROFILES)

    for activation in (1, 2):
        build = _step("runcap", "build", activation)
        assert kanban_db.complete_task(kanban_conn, build["kanban_task_id"], result=f"build {activation}")
        reconcile(kanban_conn, "runcap", profiles=PROFILES)
        review = _step("runcap", "review", activation)
        _complete_review(
            kanban_db, kanban_conn, review["kanban_task_id"],
            outcome="request_changes", target="build",
        )
        reconcile(kanban_conn, "runcap", profiles=PROFILES)

    blocked = _step("runcap", "build", 3)
    instance = _instance("runcap")
    assert blocked["state"] == "blocked"
    assert blocked["blocked_reason"] == "activation_fuse"
    assert blocked["kanban_task_id"] is None
    assert instance["status"] == "blocked"
    # Four runs admitted (build 1, review 1, build 2, review 2); the counter
    # advances on its own now that it no longer rides a token charge.
    assert instance["activation_count"] == 4


def test_three_day_human_gate_is_never_claimed_reclaimed_or_duplicated(tmp_path, kanban_conn, monkeypatch):
    """§17.4/§17.9: human gates remain sticky blocked across TTL maintenance."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: shipfactory.recipe/v1
id: human-gate
version: 1
status: active
description: human wait
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 1, max_step_activations: 1, max_tokens: 1}
steps:
  - id: approve
    primitive: approval_gate
    title: Human approval
    needs: []
    optional: false
    params: {approvers: [architect], instructions: approve this revision}
""",
        "human-gate@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="human")
    reconcile(kanban_conn, "human", profiles=PROFILES)
    gate = _step("human", "approve")
    task = kanban_db.get_task(kanban_conn, gate["kanban_task_id"])
    assert (task.status, task.block_kind, task.assignee) == ("blocked", "needs_input", None)
    assert task.claim_lock is None and task.claim_expires is None and task.worker_pid is None

    original_time = time.time()
    monkeypatch.setattr(kanban_db.time, "time", lambda: original_time + 3 * 24 * 60 * 60)
    assert kanban_db.release_stale_claims(kanban_conn) == 0
    reconcile(kanban_conn, "human", profiles=PROFILES)
    assert kanban_db.release_stale_claims(kanban_conn) == 0
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key=?",
        (f"recipe/human/{recipe.hash}/approve/1",),
    ).fetchone()[0] == 1
    task = kanban_db.get_task(kanban_conn, gate["kanban_task_id"])
    assert task.status == "blocked" and task.claim_lock is None and task.worker_pid is None

    result = _recipe_gate(kanban_conn, "human", "approve", "approve", "")
    assert result["status"] == "waiting_gate"
    assert result["decision_id"] != result["key"]
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "blocked"
    apply_events(kanban_conn, profiles=PROFILES)
    assert _instance("human")["status"] == "done"
    assert kanban_db.get_task(kanban_conn, gate["kanban_task_id"]).status == "done"


def test_selector_race_guard_refuses_auto_decompose_and_accepts_disabled(monkeypatch):
    """§17.1: recipe routing and Hermes auto-decompose are mutually exclusive."""
    from hermes_cli import config as hermes_config

    config = FactoryConfig("test", {}, {}, {"enabled": True})
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"kanban": {"auto_decompose": True}})
    with pytest.raises(RuntimeError, match="auto_decompose=true"):
        startup_guard(config)
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"kanban": {"auto_decompose": False}})
    startup_guard(config)


def test_reroute_replaces_before_activation_and_cancels_after_activation(tmp_path, kanban_conn):
    """§17.10: pre-activation replacement is in-place; post-activation preserves history."""
    from hermes_cli import kanban_db

    library_path = tmp_path / "reroute-library"
    library_path.mkdir()
    for recipe_id, step_id in (("route-a", "old-work"), ("route-b", "new-work")):
        (library_path / f"{recipe_id}.yaml").write_text(
            f"""schema: shipfactory.recipe/v1
id: {recipe_id}
version: 1
status: active
description: reroute fixture
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets: {{max_activations: 2, max_step_activations: 1, max_tokens: 50000}}
steps:
  - id: {step_id}
    primitive: agent_task
    title: {step_id}
    needs: []
    optional: false
    params: {{seat: dev-backend, instructions: {step_id}, execution_profile: standard, workspace: worktree}}
""",
            encoding="utf-8",
        )
    library = load_library(library_path)
    pre = instantiate(kanban_conn, board="test", recipe=library.get("route-a@1"), parameters={}, instance_id="pre")
    result = _reroute(
        kanban_conn,
        argparse.Namespace(instance="pre", recipe="route-b@1", parameters="{}", library=str(library_path)),
    )
    assert result["activated"] is False
    assert result["replacement"]["instance_id"] == "pre"
    assert result["replacement"]["collector_task_id"] == pre["collector_task_id"]
    assert _instance("pre")["recipe_id"] == "route-b"
    assert _step("pre", "new-work")["activation"] == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM recipe_steps WHERE instance_id='pre'").fetchone()[0] == 1

    post = instantiate(kanban_conn, board="test", recipe=library.get("route-a@1"), parameters={}, instance_id="post")
    reconcile(kanban_conn, "post", profiles=PROFILES)
    old_task = _step("post", "old-work")["kanban_task_id"]
    kanban_db.add_comment(kanban_conn, old_task, "worker", "artifact: retained-review.txt")
    result = _reroute(
        kanban_conn,
        argparse.Namespace(instance="post", recipe="route-b@1", parameters="{}", library=str(library_path)),
    )
    replacement = result["replacement"]
    assert result["activated"] is True
    assert replacement["instance_id"] != "post"
    assert _instance("post")["status"] == "cancelled"
    assert kanban_db.get_task(kanban_conn, post["collector_task_id"]).status == "blocked"
    assert kanban_db.get_task(kanban_conn, post["collector_task_id"]).block_kind == "needs_input"
    assert kanban_db.get_task(kanban_conn, old_task).status == "archived"
    assert _step("post", "old-work")["kanban_task_id"] == old_task
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body=?",
        (old_task, "artifact: retained-review.txt"),
    ).fetchone()[0] == 1


def test_cancellation_parks_collector_without_releasing_sibling_graph(tmp_path, kanban_conn, monkeypatch):
    """§17.11/§17.14.2: atomic subtree cancel never satisfies outer dependencies."""
    from hermes_cli import kanban_db

    root, left, right = _selection_with_siblings(kanban_conn, tmp_path)
    left_task = _step("left", "work")["kanban_task_id"]
    right_task = _step("right", "work")["kanban_task_id"]
    calls = []
    real_cancel_subtree = kanban_db.cancel_subtree

    def recording_cancel_subtree(conn, task_ids, *, keep_blocked=None):
        calls.append((list(task_ids), list(keep_blocked or [])))
        return real_cancel_subtree(conn, task_ids, keep_blocked=keep_blocked)

    monkeypatch.setattr(kanban_db, "cancel_subtree", recording_cancel_subtree)
    before_right = kanban_db.get_task(kanban_conn, right_task).status
    before_root = kanban_db.get_task(kanban_conn, root).status
    result = cancel(kanban_conn, "left")
    assert result["status"] == "cancelled"
    assert calls == [([left_task, left["collector_task_id"]], [left["collector_task_id"]])]
    assert kanban_db.get_task(kanban_conn, left_task).status == "archived"
    collector = kanban_db.get_task(kanban_conn, left["collector_task_id"])
    assert (collector.status, collector.block_kind, collector.assignee) == ("blocked", "needs_input", None)
    assert kanban_db.list_events(kanban_conn, collector.id)[-1].payload["reason"] == "recipe_cancelled"
    assert kanban_db.get_task(kanban_conn, right_task).status == before_right
    assert kanban_db.get_task(kanban_conn, root).status == before_root == "blocked"

    assert kanban_db.complete_task(kanban_conn, right_task, result="right done")
    reconcile(kanban_conn, "right", profiles=PROFILES)
    assert kanban_db.get_task(kanban_conn, right["collector_task_id"]).status == "done"
    assert reconcile_root_collectors(kanban_conn) == 0
    assert kanban_db.get_task(kanban_conn, root).status == "blocked"


def test_root_collector_completes_once_after_two_sibling_successes(tmp_path, kanban_conn):
    """§17.14.1: the advancer explicitly and idempotently completes the root."""
    from hermes_cli import kanban_db

    root, left, right = _selection_with_siblings(kanban_conn, tmp_path)
    for instance_id in ("left", "right"):
        task_id = _step(instance_id, "work")["kanban_task_id"]
        assert kanban_db.complete_task(kanban_conn, task_id, result=f"{instance_id} done")
        reconcile(kanban_conn, instance_id, profiles=PROFILES)
    assert kanban_db.get_task(kanban_conn, left["collector_task_id"]).status == "done"
    assert kanban_db.get_task(kanban_conn, right["collector_task_id"]).status == "done"
    assert kanban_db.get_task(kanban_conn, root).status == "blocked"

    assert reconcile_root_collectors(kanban_conn) == 1
    assert reconcile_root_collectors(kanban_conn) == 0
    assert kanban_db.get_task(kanban_conn, root).status == "done"
    completed = [event for event in kanban_db.list_events(kanban_conn, root) if event.kind == "completed"]
    assert len(completed) == 1
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM action_intents "
            "WHERE kind='triage_root_completion' AND state='succeeded'"
        ).fetchone()[0] == 1


def test_advance_key_is_spec_formula():
    expected = hashlib.sha256(b"i|h|s|2|done|event-9").hexdigest()
    assert advance_key("i", "h", "s", 2, "done", "event-9") == expected


def test_gate_rejection_accepts_kanban_task_id_target(tmp_path, kanban_conn):
    """Finding #25b: review workers are prompted with kanban task ids, so
    verdicts routinely cite t_<id> of the build task instead of the recipe
    step id. The advancer must resolve it instead of fusing the gate."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: shipfactory.recipe/v1
id: task-id-target
version: 1
status: active
description: verdict targets a kanban task id
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 3, max_tokens: 500000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: qa
    primitive: review_gate
    title: QA
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: qa, execution_profile: standard, workspace: worktree}
""",
        "task-id-target@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="tid")
    reconcile(kanban_conn, "tid", profiles=PROFILES)
    build1 = _step("tid", "build", 1)
    assert kanban_db.complete_task(kanban_conn, build1["kanban_task_id"], result="rev one")
    reconcile(kanban_conn, "tid", profiles=PROFILES)
    qa1 = _step("tid", "qa", 1)
    # Target the build step by its KANBAN TASK id, exactly as t_10fdf585 did.
    _complete_review(kanban_db, kanban_conn, qa1["kanban_task_id"],
                     outcome="request_changes", target=build1["kanban_task_id"])
    reconcile(kanban_conn, "tid", profiles=PROFILES)
    build2 = _step("tid", "build", 2)
    assert build2["state"] == "running"
    assert _step("tid", "qa", 1)["blocked_reason"] == "changes_requested"
    assert _instance("tid")["status"] != "blocked"


def test_rework_activation_inherits_rejecting_verdict_as_parent(tmp_path, kanban_conn):
    """Finding #26: build act-2 must receive the review gate's verdict in its
    parent handoffs — otherwise the rework worker rebuilds blind."""
    from hermes_cli import kanban_db

    recipe = _recipe(
        tmp_path,
        """schema: shipfactory.recipe/v1
id: rework-context
version: 1
status: active
description: rework worker sees the verdict
intent_tags: [test]
supersedes: null
parameters: {}
budgets: {max_activations: 10, max_step_activations: 3, max_tokens: 500000}
steps:
  - id: build
    primitive: agent_task
    title: Build
    needs: []
    optional: false
    params: {seat: dev-backend, instructions: build, execution_profile: standard, workspace: worktree}
  - id: qa
    primitive: review_gate
    title: QA
    needs: [build]
    optional: false
    params: {seat: verifier, instructions: qa, execution_profile: standard, workspace: worktree}
""",
        "rework-context@1",
    )
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="rwctx")
    reconcile(kanban_conn, "rwctx", profiles=PROFILES)
    build1 = _step("rwctx", "build", 1)
    assert kanban_db.complete_task(kanban_conn, build1["kanban_task_id"], result="rev one")
    reconcile(kanban_conn, "rwctx", profiles=PROFILES)
    qa1 = _step("rwctx", "qa", 1)
    _complete_review(kanban_db, kanban_conn, qa1["kanban_task_id"],
                     outcome="request_changes", target="build")
    reconcile(kanban_conn, "rwctx", profiles=PROFILES)
    build2 = _step("rwctx", "build", 2)
    assert build2["state"] == "running"
    parent_ids = [
        row[0] for row in kanban_conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id=?",
            (build2["kanban_task_id"],),
        ).fetchall()
    ]
    assert qa1["kanban_task_id"] in parent_ids, (
        "rework build must list the rejecting QA gate as a parent so its "
        "verdict findings flow into the worker context"
    )
