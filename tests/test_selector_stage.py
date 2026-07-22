"""Daemon selector-stage coverage with a hermetic fake auxiliary client."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipfactory import store
from shipfactory.config import FactoryConfig, Seat, selector_config
from shipfactory.recipes import selector_stage
from shipfactory.recipes.advancer import reconcile
from shipfactory.recipes.loader import RecipeError, load_library
from shipfactory.recipes.selector import RECIPE_SELECTOR_PROMPT, lease_source_task, validate_selection


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    name: {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
    for name in ("standard", "planning", "build", "review")
}


class FakeAuxClient:
    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def queue(self, *responses: dict) -> None:
        self.responses.extend(responses)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        message = SimpleNamespace(content=json.dumps(response))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def fake_aux(monkeypatch) -> FakeAuxClient:
    from agent import auxiliary_client

    fake = FakeAuxClient()
    monkeypatch.setattr(
        auxiliary_client, "get_text_auxiliary_client",
        lambda purpose: (fake, "fake-selector-model"),
    )
    monkeypatch.setattr(auxiliary_client, "get_auxiliary_extra_body", lambda: {})
    return fake


@pytest.fixture
def stage_config(monkeypatch) -> FactoryConfig:
    seats = {
        name: Seat(name, profile=name, executor="codex", role=role)
        for name, role in (
            ("explorer", "researcher"),
            ("author", "engineer"),
            ("dev-backend", "engineer"), ("verifier", "qa"),
            ("architect", "engineer"), ("operator", "general"),
            # dev-pipeline@13 step-granular seats.
            ("spec-author", "engineer"), ("plan-author", "engineer"),
            ("story-author", "engineer"), ("builder", "engineer"),
            ("spec-reviewer", "qa"), ("plan-reviewer", "qa"),
            ("correctness-reviewer", "qa"), ("adversarial-reviewer", "engineer"),
        )
    }
    cfg = FactoryConfig(
        "test", seats, {},
        {
            "enabled": True,
            "library_path": str(ROOT / "recipes"),
            "bare_task_recipe": "bare-task-default@1",
            "notify_target": "test:noop",
            "dispatcher_max_in_progress": 4,
            "execution_profiles": PROFILES,
            "verification_profiles": {"browser-standard": {}},
            "selector": {
                "enabled": True,
                "max_per_tick": 3,
            },
        },
    )
    monkeypatch.setattr(selector_stage, "load_seats", lambda: cfg)
    return cfg


def _source(conn, title: str = "Route this work") -> str:
    from hermes_cli import kanban_db
    return kanban_db.create_task(conn, title=title, body="Do the requested work.", triage=True)


def _node(node_id: str, *, chosen: str | None = "dev-pipeline@1",
          needs: list[str] | None = None, clarification: list[str] | None = None) -> dict:
    parameters = (
        {"request": f"Implement {node_id}"}
        if chosen is not None else {"assignee_seat": "operator"}
    )
    return {
        "id": node_id,
        "title": f"Work on {node_id}",
        "body": f"Complete {node_id} with tests.",
        "needs": needs or [],
        "ranked_candidates": [
            {"id": chosen or "bare-task-default@1", "score": 0.9, "reason": "best fit"},
        ],
        "chosen": chosen,
        "parameters": parameters,
        "skip_steps": [],
        "assumptions": ["The task fits one seat context."],
        "needs_clarification": clarification or [],
    }


def _selection(*nodes: dict) -> dict:
    return {"nodes": list(nodes)}


def _selection_row(source: str) -> dict:
    with store._connect() as db:
        return dict(db.execute(
            "SELECT * FROM triage_selections WHERE source_task_id=?", (source,),
        ).fetchone())


def _blocked_reason(conn, source: str) -> str:
    from hermes_cli import kanban_db
    events = [event for event in kanban_db.list_events(conn, source) if event.kind == "blocked"]
    return events[-1].payload["reason"]


def test_selector_stage_fans_out_mixed_recipe_and_bare_nodes_with_needs(
    kanban_conn, stage_config, fake_aux,
):
    from hermes_cli import kanban_db

    source = _source(kanban_conn)
    fake_aux.queue(_selection(
        _node("build"),
        _node("operate", chosen=None, needs=["build"]),
    ))

    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 1, "instantiated": 2, "parked": 0, "skipped": 0,
    }
    with store._connect() as db:
        instances = [dict(row) for row in db.execute(
            "SELECT * FROM recipe_instances ORDER BY recipe_id",
        ).fetchall()]
    assert {row["recipe_id"] for row in instances} == {"bare-task-default", "dev-pipeline"}
    collectors = {row["recipe_id"]: row["collector_task_id"] for row in instances}
    assert kanban_conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
        (collectors["dev-pipeline"], collectors["bare-task-default"]),
    ).fetchone()
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM task_links WHERE child_id=?", (source,),
    ).fetchone()[0] == 2
    root = kanban_db.get_task(kanban_conn, source)
    assert (root.status, root.block_kind, root.assignee) == ("blocked", "needs_input", None)
    assert _selection_row(source)["outcome"] == "selected"


def test_selector_stage_null_choice_runs_bare_task_recipe(
    kanban_conn, stage_config, fake_aux,
):
    from hermes_cli import kanban_db

    _source(kanban_conn)
    fake_aux.queue(_selection(_node("solo", chosen=None)))
    assert selector_stage.run_stage(kanban_conn, "test")["instantiated"] == 1
    with store._connect() as db:
        instance = dict(db.execute("SELECT * FROM recipe_instances").fetchone())
    assert instance["recipe_id"] == "bare-task-default"
    assert json.loads(instance["parameters_json"]) == {
        "assignee_seat": "operator", "body": "Complete solo with tests.",
        "title": "Work on solo",
    }
    reconcile(kanban_conn, instance["id"], profiles=PROFILES)
    with store._connect() as db:
        step = db.execute("SELECT * FROM recipe_steps WHERE instance_id=?", (instance["id"],)).fetchone()
    assert kanban_db.get_task(kanban_conn, step["kanban_task_id"]).assignee == "operator"


def test_selector_stage_parks_clarification_without_instantiation(
    kanban_conn, stage_config, fake_aux,
):
    source = _source(kanban_conn)
    fake_aux.queue(_selection(_node(
        "ambiguous", clarification=["Which tenant owns the data?"],
    )))
    result = selector_stage.run_stage(kanban_conn, "test")
    assert result["parked"] == 1 and result["instantiated"] == 0
    assert "needs_clarification" in _blocked_reason(kanban_conn, source)
    assert _selection_row(source)["outcome"] == "needs_clarification"


def test_selector_stage_parks_no_recipe_match(
    kanban_conn, stage_config, fake_aux,
):
    source = _source(kanban_conn)
    fake_aux.queue(_selection(_node("unknown", chosen="missing-recipe@1")))
    result = selector_stage.run_stage(kanban_conn, "test")
    assert result["parked"] == 1 and result["instantiated"] == 0
    assert "no_recipe_match" in _blocked_reason(kanban_conn, source)
    assert _selection_row(source)["outcome"] == "no_recipe_match"


def test_selector_recipe_alias_normalizes_before_validation(stage_config):
    node = _node("alias")
    node["ranked_candidates"] = [{
        "recipe": "dev-pipeline@1", "rank": 2, "reason": "best fit", "ignored": True,
    }, {
        "id": "dev-pipeline@1", "recipe": "missing-recipe@1", "score": 0.8,
        "reason": "explicit id wins",
    }]
    library = load_library(
        stage_config.recipes["library_path"], seats=set(stage_config.seats),
        profiles=set(stage_config.recipes["execution_profiles"]),
        verification_profiles=set(stage_config.recipes["verification_profiles"]),
    )

    nodes = validate_selection(
        _selection(node), library, seats=set(stage_config.seats),
        profiles=set(stage_config.recipes["execution_profiles"]),
    )

    assert nodes[0]["ranked_candidates"] == [{
        "id": "dev-pipeline@1", "score": 0.5, "reason": "best fit",
    }, {
        "id": "dev-pipeline@1", "score": 0.8, "reason": "explicit id wins",
    }]


def test_selector_rank_derives_exact_reciprocal_score(stage_config):
    node = _node("rank")
    node["ranked_candidates"] = [{"id": "dev-pipeline@1", "rank": 3, "reason": "fit"}]
    library = load_library(
        stage_config.recipes["library_path"], seats=set(stage_config.seats),
        profiles=set(stage_config.recipes["execution_profiles"]),
        verification_profiles=set(stage_config.recipes["verification_profiles"]),
    )

    nodes = validate_selection(
        _selection(node), library, seats=set(stage_config.seats),
        profiles=set(stage_config.recipes["execution_profiles"]),
    )

    assert nodes[0]["ranked_candidates"][0]["score"] == 1 / 3


def test_selector_invalid_candidate_after_aliasing_is_rejected(stage_config):
    node = _node("invalid")
    node["ranked_candidates"] = [{"recipe": "dev-pipeline@1", "rank": 1}]
    library = load_library(
        stage_config.recipes["library_path"], seats=set(stage_config.seats),
        profiles=set(stage_config.recipes["execution_profiles"]),
        verification_profiles=set(stage_config.recipes["verification_profiles"]),
    )

    with pytest.raises(RecipeError, match="invalid ranked candidate"):
        validate_selection(
            _selection(node), library, seats=set(stage_config.seats),
            profiles=set(stage_config.recipes["execution_profiles"]),
        )


def test_selector_unknown_normalized_recipe_parks_no_recipe_match(
    kanban_conn, stage_config, fake_aux,
):
    source = _source(kanban_conn)
    node = _node("unknown", chosen="missing-recipe@1")
    node["ranked_candidates"] = [{
        "recipe": "missing-recipe@1", "rank": 1, "reason": "best fit",
    }]
    selection_id = lease_source_task(source, "test")
    assert selection_id
    with store._connect() as db:
        db.execute(
            "UPDATE triage_selections SET ranked_json=?,lease_until=? WHERE id=?",
            (json.dumps(_selection(node)), "2000-01-01T00:00:00+00:00", selection_id),
        )

    result = selector_stage.run_stage(kanban_conn, "test")

    assert result["parked"] == 1 and result["instantiated"] == 0
    assert "no_recipe_match" in _blocked_reason(kanban_conn, source)
    assert _selection_row(source)["outcome"] == "no_recipe_match"
    assert fake_aux.calls == []


def test_selector_prompt_states_canonical_ranked_candidate_schema():
    assert "exactly the keys id, score, and reason" in RECIPE_SELECTOR_PROMPT
    assert '{"id":"dev-pipeline@14","score":1.0,"reason":"best fit"}' in RECIPE_SELECTOR_PROMPT


def test_selector_gated_telemetry_records_all_declined_gate_reasons(
    kanban_conn, stage_config, fake_aux, monkeypatch,
):
    records: list[dict] = []
    monkeypatch.setattr(selector_stage, "_GATED_TELEMETRY_SEEN", set())
    monkeypatch.setattr(selector_stage.telemetry, "append_jsonl", records.append)

    stage_config.recipes["enabled"] = False
    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 0,
    }

    stage_config.recipes["enabled"] = True
    stage_config.recipes["selector"]["enabled"] = False
    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 0,
    }

    stage_config.recipes["selector"]["enabled"] = True
    assert selector_stage.run_stage(kanban_conn, "other-board") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 0,
    }

    assert records == [
        {"event": "selector_stage_gated", "reason": "recipes_disabled", "board": "test", "company": "test"},
        {"event": "selector_stage_gated", "reason": "selector_disabled", "board": "test", "company": "test"},
        {"event": "selector_stage_gated", "reason": "board_company_mismatch", "board": "other-board", "company": "test"},
    ]
    assert fake_aux.calls == []


def test_selector_gated_telemetry_is_emitted_once_per_reason_and_board_pair(
    kanban_conn, stage_config, fake_aux, monkeypatch,
):
    records: list[dict] = []
    monkeypatch.setattr(selector_stage, "_GATED_TELEMETRY_SEEN", set())
    monkeypatch.setattr(selector_stage.telemetry, "append_jsonl", records.append)

    for _ in range(2):
        assert selector_stage.run_stage(kanban_conn, "other-board")["leased"] == 0
    assert selector_stage.run_stage(kanban_conn, "another-board")["leased"] == 0

    assert records == [
        {"event": "selector_stage_gated", "reason": "board_company_mismatch", "board": "other-board", "company": "test"},
        {"event": "selector_stage_gated", "reason": "board_company_mismatch", "board": "another-board", "company": "test"},
    ]
    assert fake_aux.calls == []


def test_selector_gated_telemetry_failure_is_best_effort(
    kanban_conn, stage_config, fake_aux, monkeypatch, caplog,
):
    calls = 0

    def fail_append(_record: dict) -> None:
        nonlocal calls
        calls += 1
        raise OSError("telemetry unavailable")

    monkeypatch.setattr(selector_stage, "_GATED_TELEMETRY_SEEN", set())
    monkeypatch.setattr(selector_stage.telemetry, "append_jsonl", fail_append)
    stage_config.recipes["selector"]["enabled"] = False

    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 0,
    }
    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 0,
    }

    assert calls == 1
    assert "failed to record selector-stage gate selector_disabled" in caplog.text
    assert fake_aux.calls == []


def test_selector_stage_skips_lease_contention(
    kanban_conn, stage_config, fake_aux,
):
    source = _source(kanban_conn)
    assert lease_source_task(source, "test")
    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 1,
    }
    assert fake_aux.calls == []


def test_selector_stage_crash_between_instantiate_and_outcome_is_idempotent(
    kanban_conn, stage_config, fake_aux, monkeypatch,
):
    """A retry reuses one deterministic instance and one collector task."""
    source = _source(kanban_conn)
    fake_aux.queue(_selection(_node("crash-proof")))
    real_record = selector_stage._record_outcome

    def crash_before_selected_outcome(selection_id, outcome, **kwargs):
        if outcome == "selected":
            raise RuntimeError("simulated crash after instantiate")
        return real_record(selection_id, outcome, **kwargs)

    monkeypatch.setattr(selector_stage, "_record_outcome", crash_before_selected_outcome)
    first = selector_stage.run_stage(kanban_conn, "test")
    assert first["skipped"] == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == 1
        db.execute(
            "UPDATE triage_selections SET lease_until=? WHERE source_task_id=?",
            ("2000-01-01T00:00:00+00:00", source),
        )

    monkeypatch.setattr(selector_stage, "_record_outcome", real_record)
    second = selector_stage.run_stage(kanban_conn, "test")
    assert second["instantiated"] == 0
    assert len(fake_aux.calls) == 1
    with store._connect() as db:
        instance = dict(db.execute("SELECT * FROM recipe_instances").fetchone())
        assert db.execute("SELECT COUNT(*) FROM recipe_instances").fetchone()[0] == 1
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key=?",
        (f"recipe/{instance['id']}/{instance['recipe_hash']}/collector",),
    ).fetchone()[0] == 1
    assert _selection_row(source)["outcome"] == "selected"


def test_selector_stage_disabled_is_noop(
    kanban_conn, stage_config, fake_aux,
):
    _source(kanban_conn)
    assert selector_config({"enabled": True}) == {
        "enabled": True, "max_per_tick": 3,
    }
    stage_config.recipes["selector"]["enabled"] = False
    assert selector_stage.run_stage(kanban_conn, "test") == {
        "leased": 0, "instantiated": 0, "parked": 0, "skipped": 0,
    }
    assert fake_aux.calls == []


def test_selector_stage_enforces_max_per_tick(
    kanban_conn, stage_config, fake_aux,
):
    from hermes_cli import kanban_db

    for index in range(4):
        _source(kanban_conn, f"Task {index}")
    fake_aux.queue(*[_selection(_node(f"node-{index}")) for index in range(3)])
    result = selector_stage.run_stage(kanban_conn, "test")
    assert result == {"leased": 3, "instantiated": 3, "parked": 0, "skipped": 0}
    assert len(fake_aux.calls) == 3
    assert len(kanban_db.list_tasks(kanban_conn, status="triage")) == 1


def test_selector_stage_records_parent_collectors_in_overlay(
    kanban_conn, stage_config, fake_aux,
):
    """Amendment B: the DAG edge is durable in shipfactory.db, not only task_links."""
    _source(kanban_conn)
    fake_aux.queue(_selection(
        _node("build"),
        _node("operate", chosen=None, needs=["build"]),
    ))
    assert selector_stage.run_stage(kanban_conn, "test")["instantiated"] == 2
    with store._connect() as db:
        instances = {
            row["recipe_id"]: dict(row)
            for row in db.execute("SELECT * FROM recipe_instances").fetchall()
        }
    parent = instances["dev-pipeline"]
    child = instances["bare-task-default"]
    assert json.loads(parent["parent_tasks_json"]) == []
    assert json.loads(child["parent_tasks_json"]) == [parent["collector_task_id"]]
