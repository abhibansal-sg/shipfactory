import copy
from dataclasses import FrozenInstanceError
import ast
import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest
import yaml

from shipfactory.graph_recipe import (
    GraphRecipe,
    GraphRecipeError,
    load,
    load_library,
    validate,
)


ROOT = Path(__file__).resolve().parents[1]


def canonical_document():
    return yaml.safe_load(
        (ROOT / "recipes" / "v1" / "plan-build-review.yaml").read_text()
    )


def test_runtime_recipe_matches_ratified_document():
    document = (ROOT / "docs" / "recipe-structure-v1.md").read_text()
    match = re.search(r"```ya?ml\s*\n(.*?)\n```", document, re.DOTALL)
    assert match is not None

    ratified_recipe = yaml.safe_load(match.group(1))
    runtime_recipe = yaml.safe_load(
        (ROOT / "recipes" / "v1" / "plan-build-review.yaml").read_text()
    )

    assert runtime_recipe == ratified_recipe
    assert (ROOT / "recipes" / "v1" / "plan-build-review.yaml").read_text() == (
        match.group(1) + "\n"
    )


def test_canonical_fixture_loads_as_an_immutable_ordered_snapshot():
    recipe = load(ROOT / "recipes" / "v1" / "plan-build-review.yaml")

    assert isinstance(recipe, GraphRecipe)
    assert recipe.name == "plan-build-review"
    assert recipe.start == "planner"
    assert [box["id"] for box in recipe.boxes] == [
        "planner",
        "plan-review",
        "builder",
        "correctness-review",
        "risk-review",
        "simplicity-review",
        "synthesize",
        "human-approval",
        "final-delivery",
    ]
    assert recipe.boxes[-1]["end"] is True
    assert dict(recipe.arrows[0]) == {
        "from": "planner", "result": "done", "to": ("plan-review",),
    }
    assert json.loads(recipe.canonical_json) == canonical_document()
    assert isinstance(recipe.canonical_json, str)
    assert len(recipe.hash) == 64
    with pytest.raises(FrozenInstanceError):
        setattr(recipe, "name", "changed")
    with pytest.raises(TypeError):
        recipe.boxes[0]["name"] = "changed"
    destinations = recipe.arrows[0]["to"]
    assert isinstance(destinations, tuple)
    with pytest.raises(TypeError):
        recipe.arrows[0]["to"] = destinations + ("builder",)
    with pytest.raises(TypeError):
        recipe.document["start"] = "builder"
    assert recipe.hash == hashlib.sha256(recipe.canonical_json.encode()).hexdigest()
    assert [dict(box) for box in recipe.boxes] == canonical_document()["boxes"]
    actual_arrows = []
    for arrow in recipe.arrows:
        destinations = arrow["to"]
        assert isinstance(destinations, tuple)
        actual_arrows.append({**dict(arrow), "to": list(destinations)})
    assert actual_arrows == canonical_document()["arrows"]


def test_canonical_routing_destinations_preserve_declaration_order():
    recipe = load(ROOT / "recipes" / "v1" / "plan-build-review.yaml")

    assert recipe.destinations("builder", "done") == (
        "correctness-review",
        "risk-review",
        "simplicity-review",
    )
    assert recipe.destinations("planner", "unknown") == ()


def test_canonical_arrows_classify_backward_only_by_box_position():
    recipe = load(ROOT / "recipes" / "v1" / "plan-build-review.yaml")

    assert recipe.is_rework_arrow("plan-review", "revise") is True
    assert recipe.is_rework_arrow("synthesize", "rework") is True
    assert recipe.is_rework_arrow("human-approval", "rejected") is True
    assert recipe.is_rework_arrow("builder", "done") is False


def test_self_loop_is_backward_by_declared_position():
    document = canonical_document()
    document["arrows"].append({
        "from": "planner",
        "result": "retry",
        "to": ["planner"],
    })

    recipe = validate(document)

    assert recipe.is_rework_arrow("planner", "retry") is True


def test_one_backward_destination_marks_the_whole_arrow_backward():
    document = canonical_document()
    document["arrows"][3]["to"].append("planner")

    recipe = validate(document)

    assert recipe.is_rework_arrow("builder", "done") is True


def test_canonical_incoming_metadata_is_complete_and_ordered():
    document = canonical_document()
    recipe = validate(document)
    expected_incoming = {
        box["id"]: tuple(
            (arrow["from"], arrow["result"])
            for arrow in document["arrows"]
            if box["id"] in arrow["to"]
        )
        for box in document["boxes"]
    }

    assert {
        box["id"]: recipe.incoming(box["id"])
        for box in document["boxes"]
    } == expected_incoming
    assert recipe.box("planner")["name"] == "Plan the work"
    assert recipe.is_end("final-delivery") is True
    assert recipe.is_end("planner") is False


def test_rework_identity_uses_position_not_result_label():
    document = canonical_document()
    document["arrows"][0]["result"] = "rework"
    document["arrows"][2]["result"] = "done"
    recipe = validate(document)

    assert recipe.is_rework_arrow("planner", "rework") is False
    assert recipe.is_rework_arrow("plan-review", "done") is True


@pytest.mark.parametrize(
    "case,mutate",
    [
        ("unknown top-level key", lambda doc: doc.update(extra=True)),
        ("missing top-level key", lambda doc: doc.pop("name")),
        ("unknown box key", lambda doc: doc["boxes"][0].update(extra=True)),
        ("missing box key", lambda doc: doc["boxes"][0].pop("who")),
        ("unknown arrow key", lambda doc: doc["arrows"][0].update(extra=True)),
        ("missing arrow key", lambda doc: doc["arrows"][0].pop("result")),
    ],
)
def test_keys_are_exact(case, mutate):
    document = canonical_document()
    mutate(document)

    with pytest.raises(GraphRecipeError, match="keys"):
        validate(document)


@pytest.mark.parametrize(
    "replacement",
    [None, [], "recipe"],
)
def test_top_level_document_must_be_a_mapping(replacement):
    with pytest.raises(GraphRecipeError, match="mapping"):
        validate(replacement)


@pytest.mark.parametrize("field,replacement", [("boxes", {}), ("arrows", {})])
def test_boxes_and_arrows_must_be_lists(field, replacement):
    document = canonical_document()
    document[field] = replacement

    with pytest.raises(GraphRecipeError, match=field):
        validate(document)


def test_malformed_yaml_fails_closed(tmp_path):
    path = tmp_path / "malformed.yaml"
    path.write_text("name: [unterminated")

    with pytest.raises(GraphRecipeError, match="YAML"):
        load(path)


def test_load_library_returns_recipes_by_name_and_rejects_duplicates(tmp_path):
    library = load_library(ROOT / "recipes" / "v1")
    assert library == {"plan-build-review": library["plan-build-review"]}

    recipe_text = (ROOT / "recipes" / "v1" / "plan-build-review.yaml").read_text()
    (tmp_path / "one.yaml").write_text(recipe_text)
    (tmp_path / "two.yml").write_text(recipe_text)
    with pytest.raises(GraphRecipeError, match="duplicate recipe name"):
        load_library(tmp_path)


def test_duplicate_yaml_mapping_keys_fail_closed(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text(
        "name: first\nname: second\nstart: start\nboxes: []\narrows: []\n"
    )

    with pytest.raises(GraphRecipeError, match="duplicate YAML key.*name"):
        load(path)


def test_composite_yaml_mapping_key_fails_as_graph_recipe_error(tmp_path):
    path = tmp_path / "composite-key.yaml"
    path.write_text("? [one, two]\n: value\n")

    with pytest.raises(GraphRecipeError, match="invalid YAML key"):
        load(path)


def test_graph_recipe_has_no_legacy_recipe_imports():
    import shipfactory.graph_recipe as graph_recipe

    tree = ast.parse(inspect.getsource(graph_recipe))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert "shipfactory.recipes.loader" not in imports
    assert "shipfactory.recipes.primitives" not in imports


@pytest.mark.parametrize("field", ["name", "who", "instructions"])
@pytest.mark.parametrize("invalid", ["", "   ", None, 7])
def test_box_text_fields_are_non_empty_strings(field, invalid):
    document = canonical_document()
    document["boxes"][0][field] = invalid

    with pytest.raises(GraphRecipeError, match=field):
        validate(document)


@pytest.mark.parametrize("field", ["name", "start"])
@pytest.mark.parametrize("invalid", ["", "   ", None, 7])
def test_recipe_name_and_start_are_non_empty_strings(field, invalid):
    document = canonical_document()
    document[field] = invalid

    with pytest.raises(GraphRecipeError, match=field):
        validate(document)


@pytest.mark.parametrize("invalid", ["", "   ", None, 7])
def test_box_ids_are_non_empty_strings(invalid):
    document = canonical_document()
    document["boxes"][0]["id"] = invalid

    with pytest.raises(GraphRecipeError, match="id"):
        validate(document)


def test_box_ids_are_unique():
    document = canonical_document()
    document["boxes"][1]["id"] = document["boxes"][0]["id"]

    with pytest.raises(GraphRecipeError, match="unique"):
        validate(document)


@pytest.mark.parametrize("invalid", ["true", 1, None])
def test_end_is_boolean_when_present(invalid):
    document = canonical_document()
    document["boxes"][0]["end"] = invalid

    with pytest.raises(GraphRecipeError, match="end"):
        validate(document)


@pytest.mark.parametrize(
    "invalid",
    ["", "Done", "1done", "not_done", "not done", "-done", None],
)
def test_result_labels_match_v1_result_word_regex(invalid):
    document = canonical_document()
    document["arrows"][0]["result"] = invalid

    with pytest.raises(GraphRecipeError, match="result"):
        validate(document)


def test_duplicate_source_result_routes_are_rejected():
    document = canonical_document()
    duplicate = copy.deepcopy(document["arrows"][0])
    duplicate["to"] = ["builder"]
    document["arrows"].append(duplicate)

    with pytest.raises(GraphRecipeError, match=r"from.*, result"):
        validate(document)


@pytest.mark.parametrize(
    "invalid",
    [[], "plan-review", ["plan-review", "plan-review"]],
)
def test_destination_lists_are_non_empty_and_unique(invalid):
    document = canonical_document()
    document["arrows"][0]["to"] = invalid

    with pytest.raises(GraphRecipeError, match="destination"):
        validate(document)


def test_snapshot_is_sha256_of_canonical_json():
    document = canonical_document()
    recipe = validate(document)
    expected = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert recipe.canonical_json == expected
    assert recipe.hash == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_start_must_reference_an_existing_box():
    document = canonical_document()
    document["start"] = "missing"

    with pytest.raises(GraphRecipeError, match="start"):
        validate(document)


@pytest.mark.parametrize("field", ["from", "to"])
def test_arrow_references_must_exist(field):
    document = canonical_document()
    if field == "from":
        document["arrows"][0]["from"] = "missing"
    else:
        document["arrows"][0]["to"] = ["missing"]

    with pytest.raises(GraphRecipeError, match="reference"):
        validate(document)


@pytest.mark.parametrize("end_count", [0, 2])
def test_exactly_one_box_is_the_end(end_count):
    document = canonical_document()
    if end_count == 0:
        document["boxes"][-1].pop("end")
    else:
        document["boxes"][0]["end"] = True

    with pytest.raises(GraphRecipeError, match="exactly one"):
        validate(document)


def test_end_false_is_not_part_of_the_exact_grammar():
    document = canonical_document()
    document["boxes"][-1]["end"] = False

    with pytest.raises(GraphRecipeError, match="when present, must be true"):
        validate(document)


def test_end_box_has_no_outgoing_arrows():
    document = canonical_document()
    document["arrows"].append(
        {"from": "final-delivery", "result": "done", "to": ["planner"]}
    )

    with pytest.raises(GraphRecipeError, match="end.*outgoing"):
        validate(document)


def test_every_non_end_box_has_an_outgoing_route():
    document = canonical_document()
    document["arrows"] = [
        arrow for arrow in document["arrows"] if arrow["from"] != "planner"
    ]

    with pytest.raises(GraphRecipeError, match="outgoing"):
        validate(document)


def test_every_box_is_reachable_from_start():
    document = canonical_document()
    document["arrows"][0]["to"] = ["builder"]

    with pytest.raises(GraphRecipeError, match="reachable"):
        validate(document)


def test_every_box_can_reach_end():
    document = canonical_document()
    risk_route = next(
        arrow for arrow in document["arrows"] if arrow["from"] == "risk-review"
    )
    risk_route["to"] = ["risk-review"]

    with pytest.raises(GraphRecipeError, match="reach the end"):
        validate(document)
