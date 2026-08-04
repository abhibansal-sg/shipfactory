from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from shipfactory.graph_recipe import (
    GraphRecipeError,
    ensure_runtime_supported,
    load_v1_recipe,
    validate,
    validate_v1_recipe,
)


ROOT = Path(__file__).resolve().parents[1]


def v1_document():
    return yaml.safe_load(
        (ROOT / "recipes" / "v1" / "plan-build-review.yaml").read_text()
    )


def v2_document():
    contract = (ROOT / "docs" / "graph-recipe-v2.md").read_text()
    match = re.search(r"```ya?ml\s*\n(.*?)\n```", contract, re.DOTALL)
    assert match is not None
    return yaml.safe_load(match.group(1))


def test_v1_document_reports_version_1_and_empty_capability_sets():
    recipe = validate(v1_document())

    assert recipe.version == 1
    assert dict(recipe.capability_sets) == {}
    with pytest.raises(TypeError):
        recipe.capability_sets["new"] = {}

    assert ensure_runtime_supported(recipe) is recipe


def test_v2_document_from_frozen_contract_validates():
    document = v2_document()
    recipe = validate(document)

    assert recipe.version == 2
    assert isinstance(recipe.capability_sets, MappingProxyType)
    assert set(recipe.capability_sets) == {"coding"}
    coding = recipe.capability_sets["coding"]
    assert dict(coding) == {
        "skills": ("test-driven-development", "systematic-debugging"),
        "toolsets": ("terminal", "file", "github"),
        "plugins": ("gbrain",),
    }
    assert recipe.box("builder")["workspace"] == {"lane": "build", "access": "write"}
    assert recipe.box("builder")["capabilities"] == "coding"
    assert recipe.is_end("delivery") is True
    assert len(recipe.hash) == 64

    with pytest.raises(GraphRecipeError, match="not executable"):
        ensure_runtime_supported(recipe)


def test_v2_capability_sets_are_immutable():
    document = v2_document()
    recipe = validate(document)

    with pytest.raises(TypeError):
        recipe.capability_sets["coding"]["skills"] = ()


@pytest.mark.parametrize("invalid_version", ["2", 2.0, 1, 3, True, None])
def test_version_must_be_exactly_integer_2(invalid_version):
    document = v2_document()
    document["version"] = invalid_version

    with pytest.raises(GraphRecipeError, match="version"):
        validate(document)


def test_v2_top_level_keys_are_exact():
    document = v2_document()
    document["extra"] = True

    with pytest.raises(GraphRecipeError, match="keys"):
        validate(document)


def test_v2_top_level_missing_capability_sets_fails():
    document = v2_document()
    document.pop("capability_sets")

    with pytest.raises(GraphRecipeError, match="keys"):
        validate(document)


def test_v2_approval_artifact_is_still_supported():
    document = v2_document()
    document["approval_artifact"] = "delivery"

    recipe = validate(document)

    assert recipe.approval_artifact == "delivery"


@pytest.mark.parametrize("key", ["Coding", "1coding", "coding_set", ""])
def test_capability_set_keys_must_match_identifier_regex(key):
    document = v2_document()
    document["capability_sets"][key] = document["capability_sets"].pop("coding")

    with pytest.raises(GraphRecipeError, match="capability_sets"):
        validate(document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec.pop("skills"),
        lambda spec: spec.update(extra=[]),
    ],
)
def test_capability_set_has_exactly_three_list_fields(mutate):
    document = v2_document()
    mutate(document["capability_sets"]["coding"])

    with pytest.raises(GraphRecipeError, match="capability_sets"):
        validate(document)


def test_capability_set_lists_allow_empty_but_reject_duplicates():
    document = v2_document()
    document["capability_sets"]["coding"]["skills"] = []
    recipe = validate(document)
    assert recipe.capability_sets["coding"]["skills"] == ()

    document = v2_document()
    document["capability_sets"]["coding"]["skills"] = ["a", "a"]
    with pytest.raises(GraphRecipeError, match="unique"):
        validate(document)


def test_v2_box_keys_are_exact():
    document = v2_document()
    document["boxes"][0]["extra"] = True

    with pytest.raises(GraphRecipeError, match="keys"):
        validate(document)

    document = v2_document()
    del document["boxes"][0]["workspace"]

    with pytest.raises(GraphRecipeError, match="keys"):
        validate(document)


def test_v2_workspace_keys_are_exact():
    document = v2_document()
    document["boxes"][0]["workspace"]["extra"] = True

    with pytest.raises(GraphRecipeError, match="workspace"):
        validate(document)


def test_v2_workspace_lane_is_non_empty_string():
    document = v2_document()
    document["boxes"][0]["workspace"]["lane"] = ""

    with pytest.raises(GraphRecipeError, match="lane"):
        validate(document)


@pytest.mark.parametrize("invalid", ["", "readonly", "READ", None, 1, [], {}])
def test_v2_workspace_access_is_exactly_read_or_write(invalid):
    document = v2_document()
    document["boxes"][0]["workspace"]["access"] = invalid

    with pytest.raises(GraphRecipeError, match="access"):
        validate(document)


def test_v2_capabilities_must_reference_existing_capability_set():
    document = v2_document()
    document["boxes"][0]["capabilities"] = "unknown-set"

    with pytest.raises(GraphRecipeError, match="capabilities"):
        validate(document)


def test_v2_reuses_v1_arrow_and_reachability_semantics():
    document = v2_document()
    document["arrows"][0]["to"] = ["builder"]

    with pytest.raises(GraphRecipeError, match="reachable"):
        validate(document)


def test_v2_document_without_version_key_is_treated_as_v1():
    document = v1_document()
    assert "version" not in document

    recipe = validate(document)

    assert recipe.version == 1


def test_v1_document_cannot_carry_v2_only_keys():
    document = v1_document()
    document["capability_sets"] = {}

    with pytest.raises(GraphRecipeError, match="keys"):
        validate(document)


def test_v1_compatibility_entry_points_reject_v2_documents(tmp_path):
    document = v2_document()

    with pytest.raises(GraphRecipeError, match="v1"):
        validate_v1_recipe(document)

    path = tmp_path / "v2.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(GraphRecipeError, match="v1"):
        load_v1_recipe(path)
