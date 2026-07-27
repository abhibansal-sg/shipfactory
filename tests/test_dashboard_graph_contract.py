"""Static contracts for the pre-renderer graph/conformance lane.

These tests parse the deterministic fixture payload and inspect the owned CSS
and harness source.  They do not claim that the not-yet-owned dashboard bundle
renders graph UI.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "dashboard" / "dist" / "style.css"
HARNESS_PATH = ROOT / "dashboard" / "conformance-harness.js"
CSS = CSS_PATH.read_text(encoding="utf-8")
HARNESS = HARNESS_PATH.read_text(encoding="utf-8")


def _fixtures() -> dict:
    prefix = "const CONFORMANCE_FIXTURES = Object.freeze(JSON.parse(String.raw`"
    suffix = "`));"
    start = HARNESS.find(prefix)
    assert start >= 0, "harness must keep a parseable deterministic fixture block"
    payload_start = start + len(prefix)
    end = HARNESS.find(suffix, payload_start)
    assert end >= 0, "fixture JSON must have a closed String.raw block"
    return json.loads(HARNESS[payload_start:end])


def _fixture_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _fixture_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _fixture_strings(item)]
    return []


def _css_rules() -> list[tuple[list[str], dict[str, str]]]:
    without_comments = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", without_comments, re.DOTALL):
        selectors = [item.strip() for item in match.group(1).split(",") if item.strip()]
        declarations = {}
        for declaration in match.group(2).split(";"):
            if ":" not in declaration:
                continue
            name, value = declaration.split(":", 1)
            declarations[name.strip()] = value.strip()
        rules.append((selectors, declarations))
    return rules


def _declarations_for(selector: str) -> dict[str, str]:
    merged = {}
    for selectors, declarations in _css_rules():
        if selector in selectors:
            merged.update(declarations)
    return merged


def test_harness_has_non_authoritative_recipe_card_summaries() -> None:
    fixtures = _fixtures()
    assert fixtures["as_of"] == "2026-07-27T00:00:00+00:00"

    projects = fixtures["projects"]
    bound = projects["bound"]
    assert bound["binding"] == "bound"
    assert bound["recipes"] == {
        "allowed": ["creative-video@1", "dev-pipeline@14"],
        "default": "dev-pipeline@14",
    }
    assert projects["unclassified"] == {
        "id": "unclassified",
        "label": "Unclassified",
        "binding": "unclassified",
        "rollup": {"active": 0, "waiting": 0, "recent": []},
    }
    assert all("board" not in project for project in projects.values())

    policy = fixtures["policy"]
    assert policy["attached"]["allowed_recipe_keys"] == bound["recipes"]["allowed"]
    assert policy["attached"]["default_recipe_key"] == bound["recipes"]["default"]
    assert policy["default"]["allowed_recipe_keys"] == bound["recipes"]["allowed"]
    assert policy["default"]["default_recipe_key"] == bound["recipes"]["default"]

    response_project = fixtures["projects_response"]["projects"][0]
    assert response_project == bound
    assert fixtures["projects_response"]["unclassified"] == projects["unclassified"]

    recipes = fixtures["recipes"]
    assert set(recipes) == {"dev-pipeline@14", "creative-video@1"}
    library = load_library(ROOT / "recipes", persist=False)
    for key, recipe_fixture in recipes.items():
        recipe = library.get(key)
        assert recipe_fixture["fixture_kind"] == "non-authoritative-api-projection"
        assert recipe_fixture["authoritative_source"] == f"recipes/{key}.yaml"
        assert recipe_fixture["id"] == recipe.document["id"]
        assert recipe_fixture["version"] == recipe.document["version"]
        assert recipe_fixture["key"] == recipe.key
        assert recipe_fixture["recipe_hash"] == recipe.hash
        representative_ids = recipe_fixture["representative_step_ids"]
        assert 0 < len(representative_ids) <= 4
        authoritative_step_ids = {step["id"] for step in recipe.document["steps"]}
        assert set(representative_ids) <= authoritative_step_ids
        assert set(recipe_fixture) == {
            "fixture_kind", "authoritative_source", "id", "version", "key",
            "recipe_hash", "description", "default", "representative_step_ids",
        }
        assert recipe_fixture["default"] is (key == "dev-pipeline@14")

    fixture_text = _fixture_strings(fixtures)
    assert all("telegram:" not in text for text in fixture_text)
    assert all("Abhinav" not in text for text in fixture_text)
    assert all("private:" not in text for text in fixture_text)
    assert all(not re.search(r"</?[A-Za-z][^>]*>", text) for text in fixture_text)


def test_scenarios_use_consistent_routes_and_matching_history_graph() -> None:
    fixtures = _fixtures()
    recipes = fixtures["recipes"]
    attached = fixtures["policy"]["attached"]
    assert attached["allowed_recipe_keys"] == ["creative-video@1", "dev-pipeline@14"]
    assert set(attached["allowed_recipe_keys"]) == set(recipes)
    assert "policyFixtures.attached.allowed_recipe_keys.map" in HARNESS
    assert "recipes: [publishedRecipeFixtures[\"dev-pipeline@14\"]]" not in HARNESS

    synthetic = fixtures["graphs"]["synthetic_parallel_join"]
    assert [node["id"] for node in synthetic["nodes"]] == [
        "start", "lint", "test", "join", "approve"
    ]
    assert synthetic["nodes"][3]["primitive"] == "join"
    assert synthetic["nodes"][3]["projection_only"] is True
    assert synthetic["nodes"][4]["operator_only"] is True
    assert synthetic["nodes"][4]["shape"] == "diamond"

    history = fixtures["history"]["folded_rework"]
    history_graph = fixtures["graphs"]["folded_rework"]
    history_node_ids = {node["id"] for node in history_graph["nodes"]}
    assert {row["step_id"] for row in history["history"]} == history_node_ids
    assert history_graph is not synthetic
    for row in history["history"]:
        if row["rejected_by_step_id"] is not None:
            assert row["rejected_by_step_id"] in history_node_ids
        verdict = row["verdict"]
        if verdict and verdict["target"] is not None:
            assert verdict["target"] in history_node_ids
    for edge in history["rework_edges"]:
        assert edge["from"] in history_node_ids
        assert edge["to"] in history_node_ids
    assert history["rework_edges"][0]["from"] == "review"
    assert history["rework_edges"][0]["to"] == "build"
    assert {node["id"] for node in synthetic["nodes"]}.isdisjoint(history_node_ids)


def test_css_declares_native_svg_graph_visual_foundation() -> None:
    required = [
        ".factory-graph",
        ".factory-graph-canvas",
        ".factory-graph-node",
        ".factory-graph-shape--rect",
        ".factory-graph-shape--polygon",
        ".factory-graph-shape--path",
        ".factory-graph-node--operator-only",
        ".factory-graph-operator-badge",
        ".factory-graph-node--selected",
        ".factory-graph-node--skipped",
        ".factory-graph-node--unsupported",
        ".factory-graph-edge",
        ".factory-graph-edge--rework",
        ".factory-graph-join",
        ".factory-graph-node--state-running",
        ".factory-graph-node--state-waiting",
        ".factory-graph-node--state-blocked",
        ".factory-graph-node--state-done",
        ".factory-graph-node--state-skipped",
        ".factory-graph-node--state-unsupported",
    ]
    for selector in required:
        assert selector in CSS

    for token in (
        "--factory-graph-node-width",
        "--factory-graph-node-height",
        "--factory-graph-diamond-size",
        "--factory-graph-edge-color",
        "--factory-graph-rework-color",
        "--factory-graph-focus-ring",
    ):
        assert token in CSS

    assert "overflow-x: auto" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    for selector in (
        ".factory-graph-node--rectangle > rect.factory-graph-shape--rect",
        ".factory-graph-node--diamond > polygon.factory-graph-shape--polygon",
        ".factory-graph-node--rectangle > text.factory-graph-node-label",
        ".factory-graph-node--operator-only > text.factory-graph-operator-badge",
    ):
        assert selector in CSS
        assert _declarations_for(selector)

    forbidden_geometry = {"width", "min-width", "height", "min-height", "rx", "ry"}
    for selector in (
        ".factory-graph-node",
        ".factory-graph-node--rectangle",
        ".factory-graph-node--diamond",
        ".factory-graph-node--operator-only",
    ):
        assert not forbidden_geometry.intersection(_declarations_for(selector))
        assert "transform" not in _declarations_for(selector)
    assert ".factory-graph-node--diamond > *" not in CSS
    assert not re.search(r"\.factory-graph[^,{]*::(?:before|after)", CSS)


def test_harness_declares_future_dom_attributes_safe_text_and_valid_syntax() -> None:
    for attribute in (
        "data-project-id",
        "data-project-launch",
        "data-recipe-key",
        "data-recipe-attach",
        "data-recipe-detach",
        "data-recipe-default",
        "data-flight-instance-id",
        "data-unclassified",
        "data-graph-node",
        "data-step-id",
        "data-activation",
        "data-graph-edge",
        "data-edge-from",
        "data-edge-to",
        "data-edge-kind",
    ):
        assert attribute in HARNESS

    assert "dangerouslySetInnerHTML" not in HARNESS
    assert ".innerHTML" not in HARNESS
    assert "insertAdjacentHTML" not in HARNESS
    assert "document.write" not in HARNESS

    result = subprocess.run(
        ["node", "--check", str(HARNESS_PATH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_harness_exposes_named_static_fixture_groups() -> None:
    for declaration in (
        "const boundProjectFixture",
        "const unclassifiedProjectFixture",
        "const policyFixtures",
        "const publishedRecipeFixtures",
        "const syntheticParallelJoinFixture",
        "const foldedReworkFixture",
        "const foldedReworkGraphFixture",
    ):
        assert declaration in HARNESS
