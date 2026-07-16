"""SF-5 artifact persistence, revision identity, and recipe-v2 regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from shipfactory import store
from shipfactory.recipes.advancer import reconcile
from shipfactory.recipes.instantiate import instantiate
from shipfactory.recipes.loader import RecipeError, load_library


PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
}


def _git(worktree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=worktree, text=True,
    ).strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("artifact fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Artifact Test",
        "GIT_AUTHOR_EMAIL": "artifact@example.invalid",
        "GIT_COMMITTER_NAME": "Artifact Test",
        "GIT_COMMITTER_EMAIL": "artifact@example.invalid",
    }
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, env=env, check=True)
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _exploration(base_sha: str, tree_sha: str) -> dict:
    return {
        "schema": "shipfactory.exploration/v1",
        "intent_sha256": hashlib.sha256(b"intent").hexdigest(),
        "base_sha": base_sha,
        "repo_tree_sha": tree_sha,
        "references": [],
        "direct_callers": [],
        "constraints": [],
        "untrusted_directives": [],
        "unknowns": [],
    }


def _output() -> dict:
    return {
        "kind": "exploration",
        "schema": "shipfactory.exploration/v1",
        "path": ".shipfactory-output/exploration.json",
    }


def _write_candidate(repo: Path, payload: dict) -> Path:
    path = repo / ".shipfactory-output" / "exploration.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _v2_text(*, steps: str) -> str:
    parsed_steps = yaml.safe_load("steps:\n" + steps)["steps"]
    caps = {
        step["id"]: 2 for step in parsed_steps
        if step["primitive"] in {"agent_task", "review_gate"}
    }
    pools = {
        step["params"]["execution_profile"]: 200000 for step in parsed_steps
        if step["primitive"] in {"agent_task", "review_gate"}
    } or {"standard": 200000}
    return f"""schema: shipfactory.recipe/v2
id: artifact-test
version: 1
status: active
description: artifact test
intent_tags: [test]
supersedes: null
parameters: {{}}
budgets:
  max_activations: 4
  max_tokens: 200000
  step_activation_caps: {json.dumps(caps)}
  token_pools: {json.dumps(pools)}
steps:
{steps}
"""


def _load_v2(tmp_path: Path, text: str):
    library = tmp_path / "library"
    library.mkdir()
    (library / "recipe.yaml").write_text(text, encoding="utf-8")
    return load_library(library).get("artifact-test@1")


def test_artifact_schema_migration_is_exact_and_numbered():
    store.init_db()
    with store._connect() as db:
        versions = [row[0] for row in db.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        artifact_columns = [row["name"] for row in db.execute(
            "PRAGMA table_info(artifacts)"
        )]
        edge_columns = [row["name"] for row in db.execute(
            "PRAGMA table_info(artifact_edges)"
        )]
        step_columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(recipe_steps)"
        )}
        instance_columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(recipe_instances)"
        )}
    assert versions[-1] == 10
    assert artifact_columns == [
        "id", "instance_id", "step_id", "activation", "run_id", "kind",
        "schema_version", "state", "candidate_path", "sealed_path", "sha256",
        "size_bytes", "producer", "trust_domain", "base_sha", "head_sha",
        "repo_tree_sha", "validation_error", "created_at", "sealed_at",
    ]
    assert edge_columns == ["parent_artifact_id", "child_artifact_id", "relation"]
    assert {"input_artifact_set_hash", "output_artifact_set_hash"} <= step_columns
    assert {"base_sha", "updated_base_at"} <= instance_columns
    with store._connect() as db:
        charge_columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(budget_charges)"
        )}
    assert "token_pool" in charge_columns


def test_seal_is_idempotent_detects_tampering_and_stale_base(tmp_path):
    from shipfactory.artifacts import (
        artifact_is_stale,
        read_artifact,
        seal_artifact,
    )

    repo, base_sha, tree_sha = _repo(tmp_path)
    _write_candidate(repo, _exploration(base_sha, tree_sha))
    first = seal_artifact(
        instance_id="instance", step_id="explore", activation=1, run_id=7,
        output=_output(), workspace=repo, producer="run:7",
    )
    second = seal_artifact(
        instance_id="instance", step_id="explore", activation=1, run_id=7,
        output=_output(), workspace=repo, producer="run:7",
    )
    assert first["id"] == second["id"]
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    assert not artifact_is_stale(first, {"base_sha": base_sha})
    assert artifact_is_stale(first, {"base_sha": "f" * 40})

    sealed = Path(first["sealed_path"])
    sealed.write_bytes(sealed.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="sha256"):
        read_artifact(first["id"])


def test_torn_sealed_path_is_atomically_replaced_on_retry(tmp_path):
    from shipfactory.artifacts import _storage_path, seal_artifact

    repo, base_sha, tree_sha = _repo(tmp_path)
    candidate = _write_candidate(repo, _exploration(base_sha, tree_sha))
    sealed_path = _storage_path("torn", "explore", 1, "exploration")
    sealed_path.parent.mkdir(parents=True, exist_ok=True)
    sealed_path.write_bytes(b'{"schema":"shipfactory.exploration/v1"')

    sealed = seal_artifact(
        instance_id="torn", step_id="explore", activation=1, run_id=9,
        output=_output(), workspace=repo, producer="run:9",
    )

    assert Path(sealed["sealed_path"]).read_bytes() == candidate.read_bytes()
    assert sealed["state"] == "sealed"


def test_validation_rejection_remains_terminal_after_candidate_is_fixed(tmp_path):
    from shipfactory.artifacts import seal_artifact

    repo, base_sha, tree_sha = _repo(tmp_path)
    candidate = repo / ".shipfactory-output" / "exploration.json"
    candidate.parent.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_exploration(base_sha, tree_sha)), encoding="utf-8")
    candidate.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        seal_artifact(
            instance_id="rejected", step_id="explore", activation=1, run_id=10,
            output=_output(), workspace=repo, producer="run:10",
        )
    candidate.unlink()
    candidate.write_text(json.dumps(_exploration(base_sha, tree_sha)), encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        seal_artifact(
            instance_id="rejected", step_id="explore", activation=1, run_id=10,
            output=_output(), workspace=repo, producer="run:10",
        )
    with store._connect() as db:
        row = db.execute("SELECT state FROM artifacts WHERE id=?", (
            hashlib.sha256(b"rejected|explore|1|exploration").hexdigest(),
        )).fetchone()
    assert row["state"] == "rejected"


@pytest.mark.parametrize("attack", ["symlink", "oversize", "wrong-worktree"])
def test_sealing_rejects_candidate_attacks(tmp_path, attack):
    from shipfactory.artifacts import seal_artifact

    repo, base_sha, tree_sha = _repo(tmp_path)
    candidate = repo / ".shipfactory-output" / "exploration.json"
    candidate.parent.mkdir()
    payload = _exploration(base_sha, tree_sha)
    ceiling = 2 * 1024 * 1024
    if attack == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(payload), encoding="utf-8")
        candidate.symlink_to(outside)
        expected = "symlink"
    elif attack == "oversize":
        candidate.write_bytes(b"{" + b" " * 1024 + b"}")
        ceiling = 64
        expected = "size"
    else:
        payload["base_sha"] = "a" * 40
        candidate.write_text(json.dumps(payload), encoding="utf-8")
        expected = "repository"

    with pytest.raises(ValueError, match=expected):
        seal_artifact(
            instance_id="instance", step_id="explore", activation=1, run_id=8,
            output=_output(), workspace=repo, producer="run:8",
            max_bytes=ceiling,
        )
    with store._connect() as db:
        artifact = dict(db.execute("SELECT * FROM artifacts").fetchone())
    assert artifact["state"] == "rejected"
    assert expected in artifact["validation_error"]


def test_artifact_edges_record_derivation_once(tmp_path):
    from shipfactory.artifacts import record_artifact_edge, seal_artifact

    repo, base_sha, tree_sha = _repo(tmp_path)
    _write_candidate(repo, _exploration(base_sha, tree_sha))
    parent = seal_artifact(
        instance_id="instance", step_id="explore", activation=1, run_id=1,
        output=_output(), workspace=repo, producer="run:1",
    )
    child_id = "child-artifact"
    record_artifact_edge(parent["id"], child_id, "derived-from")
    record_artifact_edge(parent["id"], child_id, "derived-from")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifact_edges").fetchone()[0] == 1


@pytest.mark.parametrize("access_mode", ["readonly", "workspace_write"])
def test_v2_loader_accepts_normative_access_modes(tmp_path, access_mode):
    recipe = _load_v2(tmp_path, _v2_text(steps="""  - id: explore
    primitive: agent_task
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs:
      - kind: exploration
        schema: shipfactory.exploration/v1
        path: .shipfactory-output/exploration.json
    params:
      seat: explorer
      instructions: explore
      execution_profile: standard
      workspace: worktree
      access_mode: ACCESS_MODE
      environment: source
""".replace("ACCESS_MODE", access_mode)))
    assert recipe.document["schema"] == "shipfactory.recipe/v2"


@pytest.mark.parametrize(
    "access_mode", ["readwrite", "write", "", None, 123, True, [], {}],
)
def test_v2_loader_rejects_non_normative_access_modes(tmp_path, access_mode):
    steps = """  - id: explore
    primitive: agent_task
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs: []
    params:
      seat: explorer
      instructions: explore
      execution_profile: standard
      workspace: worktree
      access_mode: ACCESS_MODE
      environment: source
""".replace("ACCESS_MODE", json.dumps(access_mode))
    with pytest.raises(RecipeError, match="readonly or workspace_write"):
        _load_v2(tmp_path, _v2_text(steps=steps))


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("""  - id: explore
    primitive: notify
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs: [{kind: exploration, schema: shipfactory.exploration/v1, path: .shipfactory-output/exploration.json}]
    surprise: true
    params: {target: x, message: y}
""", "unknown step keys"),
        ("""  - id: left
    primitive: notify
    title: Left
    needs: [right]
    optional: false
    inputs: []
    outputs: []
    params: {target: x, message: y}
  - id: right
    primitive: notify
    title: Right
    needs: [left]
    optional: false
    inputs: []
    outputs: []
    params: {target: x, message: y}
""", "dependency cycle"),
        ("""  - id: consume
    primitive: notify
    title: Consume
    needs: []
    optional: false
    inputs: [{from: missing, kind: exploration, required: true}]
    outputs: []
    params: {target: x, message: y}
""", "nonexistent producer"),
        ("""  - id: explore
    primitive: notify
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs: [{kind: exploration, schema: shipfactory.exploration/v1, path: .shipfactory-output/../escape.json}]
    params: {target: x, message: y}
""", "output path"),
    ],
)
def test_v2_loader_rejects_unsafe_graphs(tmp_path, steps, message):
    with pytest.raises(RecipeError, match=message):
        _load_v2(tmp_path, _v2_text(steps=steps))


def test_v2_advancer_hashes_sealed_outputs_and_inputs(tmp_path, kanban_conn):
    from hermes_cli import kanban_db
    from shipfactory.artifacts import seal_artifact

    recipe = _load_v2(tmp_path, _v2_text(steps="""  - id: explore
    primitive: agent_task
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs:
      - {kind: exploration, schema: shipfactory.exploration/v1, path: .shipfactory-output/exploration.json}
    params: {seat: explorer, instructions: explore, execution_profile: standard, workspace: worktree, access_mode: readonly, environment: source}
  - id: consume
    primitive: agent_task
    title: Consume
    needs: [explore]
    optional: false
    inputs:
      - {from: explore, kind: exploration, required: true}
    outputs: []
    params: {seat: developer, instructions: consume, execution_profile: standard, workspace: worktree, access_mode: readonly, environment: source}
"""))
    repo, base_sha, tree_sha = _repo(tmp_path / "work")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={},
        instance_id="v2", base_sha=base_sha,
    )
    reconcile(kanban_conn, "v2", profiles=PROFILES)
    with store._connect() as db:
        explore = dict(db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id='v2' AND step_id='explore'"
        ).fetchone())

    _write_candidate(repo, _exploration(base_sha, tree_sha))
    sealed = seal_artifact(
        instance_id="v2", step_id="explore", activation=1, run_id=11,
        output=_output(), workspace=repo, producer="run:11",
    )
    assert kanban_db.complete_task(kanban_conn, explore["kanban_task_id"], result="done")
    reconcile(kanban_conn, "v2", profiles=PROFILES)

    with store._connect() as db:
        explore = dict(db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id='v2' AND step_id='explore'"
        ).fetchone())
        consume = dict(db.execute(
            "SELECT * FROM recipe_steps WHERE instance_id='v2' AND step_id='consume'"
        ).fetchone())
    expected = hashlib.sha256(f"exploration:{sealed['sha256']}".encode()).hexdigest()
    assert explore["state"] == "done"
    assert explore["output_artifact_set_hash"] == expected
    assert consume["state"] == "running"
    assert consume["input_artifact_set_hash"] == expected


def test_v2_stale_required_input_blocks_and_fresh_activation_recovers(
    tmp_path, kanban_conn,
):
    from shipfactory.artifacts import seal_artifact

    recipe = _load_v2(tmp_path, _v2_text(steps="""  - id: explore
    primitive: notify
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs:
      - {kind: exploration, schema: shipfactory.exploration/v1, path: .shipfactory-output/exploration.json}
    params: {target: x, message: y}
  - id: consume
    primitive: notify
    title: Consume
    needs: [explore]
    optional: false
    inputs: [{from: explore, kind: exploration, required: true}]
    outputs: []
    params: {target: x, message: y}
"""))
    repo, base_x, tree_x = _repo(tmp_path / "stale-work")
    instantiate(
        kanban_conn, board="test", recipe=recipe, parameters={},
        instance_id="stale", base_sha=base_x,
    )
    with store._connect() as db:
        instance = db.execute(
            "SELECT base_sha,updated_base_at FROM recipe_instances WHERE id='stale'"
        ).fetchone()
    assert instance["base_sha"] == base_x
    assert instance["updated_base_at"]
    _write_candidate(repo, _exploration(base_x, tree_x))
    seal_artifact(
        instance_id="stale", step_id="explore", activation=1, run_id=12,
        output=_output(), workspace=repo, producer="run:12",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='done' "
            "WHERE instance_id='stale' AND step_id='explore' AND activation=1"
        )

    (repo / "README.md").write_text("new trusted base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Artifact Test",
        "GIT_AUTHOR_EMAIL": "artifact@example.invalid",
        "GIT_COMMITTER_NAME": "Artifact Test",
        "GIT_COMMITTER_EMAIL": "artifact@example.invalid",
    }
    subprocess.run(["git", "commit", "-qm", "move base"], cwd=repo, env=env, check=True)
    base_y = _git(repo, "rev-parse", "HEAD")
    tree_y = _git(repo, "rev-parse", "HEAD^{tree}")
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_instances SET base_sha=?,updated_base_at=? WHERE id='stale'",
            (base_y, store._now()),
        )

    reconcile(kanban_conn, "stale", profiles=PROFILES)
    with store._connect() as db:
        blocked = db.execute(
            "SELECT state,blocked_reason FROM recipe_steps "
            "WHERE instance_id='stale' AND step_id='consume' AND activation=1"
        ).fetchone()
    assert tuple(blocked) == ("blocked", "artifact_stale")

    _write_candidate(repo, _exploration(base_y, tree_y))
    seal_artifact(
        instance_id="stale", step_id="explore", activation=2, run_id=13,
        output=_output(), workspace=repo, producer="run:13",
    )
    now = store._now()
    with store._connect() as db:
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
            "VALUES('stale','explore',2,'notify','done',?,?)",
            (now, now),
        )
        db.execute(
            "INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) "
            "VALUES('stale','consume',2,'notify','pending',?,?)",
            (now, now),
        )
    reconcile(kanban_conn, "stale", profiles=PROFILES)
    with store._connect() as db:
        fresh = db.execute(
            "SELECT state,blocked_reason,input_artifact_set_hash FROM recipe_steps "
            "WHERE instance_id='stale' AND step_id='consume' AND activation=2"
        ).fetchone()
    assert fresh["state"] == "waiting"
    assert fresh["blocked_reason"] is None
    assert fresh["input_artifact_set_hash"]


def test_v2_missing_required_input_blocks_activation(tmp_path, kanban_conn):
    recipe = _load_v2(tmp_path, _v2_text(steps="""  - id: explore
    primitive: notify
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs: [{kind: exploration, schema: shipfactory.exploration/v1, path: .shipfactory-output/exploration.json}]
    params: {target: x, message: y}
  - id: consume
    primitive: notify
    title: Consume
    needs: [explore]
    optional: false
    inputs: [{from: explore, kind: exploration, required: true}]
    outputs: []
    params: {target: x, message: y}
"""))
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="missing")
    with store._connect() as db:
        db.execute(
            "UPDATE recipe_steps SET state='done' WHERE instance_id='missing' AND step_id='explore'"
        )
    reconcile(kanban_conn, "missing", profiles=PROFILES)
    with store._connect() as db:
        step = db.execute(
            "SELECT state,blocked_reason FROM recipe_steps "
            "WHERE instance_id='missing' AND step_id='consume'"
        ).fetchone()
    assert tuple(step) == ("blocked", "artifact_missing")


def test_v2_declared_output_cannot_complete_without_sealing(tmp_path, kanban_conn):
    from hermes_cli import kanban_db

    recipe = _load_v2(tmp_path, _v2_text(steps="""  - id: explore
    primitive: agent_task
    title: Explore
    needs: []
    optional: false
    inputs: []
    outputs: [{kind: exploration, schema: shipfactory.exploration/v1, path: .shipfactory-output/exploration.json}]
    params: {seat: explorer, instructions: explore, execution_profile: standard, workspace: worktree, access_mode: readonly, environment: source}
"""))
    instantiate(kanban_conn, board="test", recipe=recipe, parameters={}, instance_id="unsealed")
    reconcile(kanban_conn, "unsealed", profiles=PROFILES)
    with store._connect() as db:
        step = dict(db.execute("SELECT * FROM recipe_steps WHERE instance_id='unsealed'").fetchone())
    assert kanban_db.complete_task(kanban_conn, step["kanban_task_id"], result="done")
    reconcile(kanban_conn, "unsealed", profiles=PROFILES)
    with store._connect() as db:
        step = db.execute(
            "SELECT state,blocked_reason FROM recipe_steps WHERE instance_id='unsealed'"
        ).fetchone()
    assert step["state"] == "blocked"
    assert step["blocked_reason"].startswith("worker_failed:")


def test_reaper_seals_after_exit_before_journaling_completion(tmp_path, monkeypatch):
    import shipfactory.artifacts as artifacts
    import shipfactory.spawn as spawn

    log = tmp_path / "worker.log"
    log.write_text("SHIPFACTORY_RESULT: done produced artifact\n", encoding="utf-8")
    run_id = store.record_run_start(
        "v2-task", "explorer", "codex", "gpt", 9001,
        workspace_path=tmp_path, log_path=log,
    )
    calls: list[str] = []

    class Process:
        def poll(self):
            return 0

    class Executor:
        def extract_text(self, text):
            return text

        def parse_usage(self, text):
            return {"tokens_in": None, "tokens_out": None}

    monkeypatch.setattr(spawn, "get_executor", lambda name: Executor())
    monkeypatch.setattr(spawn, "_drain_worker_transitions", lambda *args: None)
    monkeypatch.setattr(
        artifacts, "seal_declared_outputs_for_task",
        lambda **kwargs: calls.append("seal") or [],
    )
    monkeypatch.setattr(
        spawn, "_plan_worker_transition",
        lambda *args: calls.append("transition"),
    )
    spawn._RUNNING.clear()
    spawn._RUNNING[9001] = {
        "proc": Process(), "run_id": run_id, "task_id": "v2-task",
        "executor": "codex", "board": "test", "log_path": log,
        "workspace_path": tmp_path, "lease_key": "unused",
        "started": None, "started_at": store._now(), "adopted": False,
    }

    assert spawn.reap_finished()[0]["result"] == "done"
    assert calls == ["seal", "transition"]
