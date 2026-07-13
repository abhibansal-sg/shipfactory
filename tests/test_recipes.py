"""Recipe v2 regression coverage for immutable loading and durable events."""
from __future__ import annotations

import hashlib
import json

import pytest

from factory import store
from factory.recipes.advancer import advance_key, event
from factory.recipes.loader import RecipeError, load_library


def test_loader_persists_immutable_normalized_recipe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = tmp_path / "r.yaml"
    source.write_text("""schema: factory.recipe/v1
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


def test_duplicate_event_is_one_durable_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path)); store.init_db()
    with store._connect() as db:
        now = store._now()
        db.execute("INSERT INTO recipe_instances(id,board,collector_task_id,recipe_id,recipe_version,recipe_hash,status,parameters_json,created_at,updated_at) VALUES('i','b','c','r',1,'h','waiting_event','{}',?,?)", (now, now))
        db.execute("INSERT INTO recipe_steps(instance_id,step_id,activation,primitive,state,created_at,updated_at) VALUES('i','wait',1,'wait_for_event','waiting',?,?)", (now, now))
    assert event("i", "wait", {"id": "webhook-1", "type": "arrived"}) == event("i", "wait", {"id": "webhook-1", "type": "arrived"})
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM advance_events").fetchone()[0] == 1


def test_advance_key_is_spec_formula():
    expected = hashlib.sha256(b"i|h|s|2|done|event-9").hexdigest()
    assert advance_key("i", "h", "s", 2, "done", "event-9") == expected
