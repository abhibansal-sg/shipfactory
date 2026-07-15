"""Daemon selector-stage coverage with a hermetic fake auxiliary client."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipfactory import store
from shipfactory.config import FactoryConfig, Seat, selector_config
from shipfactory.recipes import selector_stage
from shipfactory.recipes.advancer import reconcile
from shipfactory.recipes.selector import lease_source_task


ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "standard": {
        "max_runtime_seconds": 1800,
        "max_retries": 2,
        "token_allowance": 50_000,
    }
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
            ("dev-backend", "engineer"), ("verifier", "qa"),
            ("architect", "engineer"), ("operator", "general"),
        )
    }
    cfg = FactoryConfig(
        "test", seats, {},
        {
            "enabled": True,
            "library_path": str(ROOT / "recipes"),
            "bare_task_recipe": "bare-task-default@1",
            "notify_target": "test:noop",
            "board_day_token_ceiling": 500_000,
            "dispatcher_max_in_progress": 4,
            "execution_profiles": PROFILES,
            "selector": {
                "enabled": True,
                "max_per_tick": 3,
                "selection_allowance": 5_000,
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
        charge = db.execute(
            "SELECT step_id,tokens FROM budget_charges WHERE board='test'",
        ).fetchone()
    assert {row["recipe_id"] for row in instances} == {"bare-task-default", "dev-pipeline"}
    assert tuple(charge) == ("selector", 5_000)
    assert fake_aux.calls[0]["max_tokens"] == 5_000
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
        assert db.execute(
            "SELECT COUNT(*) FROM budget_charges WHERE instance_id=(SELECT id FROM triage_selections)"
        ).fetchone()[0] == 1
    assert kanban_conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key=?",
        (f"recipe/{instance['id']}/{instance['recipe_hash']}/collector",),
    ).fetchone()[0] == 1
    assert _selection_row(source)["outcome"] == "selected"


def test_selector_stage_budget_refusal_parks_before_model_call(
    kanban_conn, stage_config, fake_aux,
):
    from shipfactory import cli

    source = _source(kanban_conn)
    stage_config.recipes["board_day_token_ceiling"] = 5_000
    day = datetime.now(timezone.utc).date().isoformat()
    store.init_db()
    with store._connect() as db:
        db.execute(
            "INSERT INTO budget_charges"
            "(key,board,utc_day,instance_id,step_id,activation,tokens,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("prior", "test", day, "prior", "work", 1, 1, store._now()),
        )
    result = selector_stage.run_stage(kanban_conn, "test")
    assert result["parked"] == 1
    assert fake_aux.calls == []
    assert _blocked_reason(kanban_conn, source) == "budget_refused"
    assert _selection_row(source)["outcome"] == "budget_refused"
    waiting = cli.main(["recipe", "waiting"])
    assert any(
        row.get("source_task_id") == source and row.get("blocked_reason") == "budget_refused"
        for row in waiting
    )


def test_selector_stage_disabled_is_noop(
    kanban_conn, stage_config, fake_aux,
):
    _source(kanban_conn)
    assert selector_config({"enabled": True}) == {
        "enabled": True, "max_per_tick": 3, "selection_allowance": 5_000,
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
