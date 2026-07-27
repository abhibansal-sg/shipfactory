"""Static contracts for the native SVG graph/conformance lane.

These tests parse the deterministic fixture payload and inspect the owned CSS,
harness, and dashboard renderer source.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from shipfactory.config import PROJECTS_VISUAL_RECIPES_DEFAULTS
from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "dashboard" / "dist" / "style.css"
HARNESS_PATH = ROOT / "dashboard" / "conformance-harness.js"
BUNDLE_PATH = ROOT / "dashboard" / "dist" / "index.js"
CSS = CSS_PATH.read_text(encoding="utf-8")
HARNESS = HARNESS_PATH.read_text(encoding="utf-8")
BUNDLE = BUNDLE_PATH.read_text(encoding="utf-8")


def _fixtures() -> dict:
    prefix = "const CONFORMANCE_FIXTURES = deepFreeze(JSON.parse(String.raw`"
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
    assert fixtures["projects_response"]["runtime_config"] == PROJECTS_VISUAL_RECIPES_DEFAULTS
    assert set(fixtures["projects_response"]["runtime_config"]) == set(
        PROJECTS_VISUAL_RECIPES_DEFAULTS
    )

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
    expected_layout = {
        "direction": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_direction"],
        "rank_gap": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_rank_gap"],
        "lane_gap": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_lane_gap"],
        "node_width": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_node_width"],
        "node_height": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_node_height"],
        "diamond_size": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_diamond_size"],
    }
    assert synthetic["source"]["pinned"] is False
    assert synthetic["layout"] == expected_layout
    assert [node["id"] for node in synthetic["nodes"]] == [
        "start", "lint", "test", "join", "approve"
    ]
    assert synthetic["nodes"][3]["primitive"] == "join"
    assert synthetic["nodes"][3]["projection_only"] is True
    assert synthetic["nodes"][4]["operator_only"] is True
    assert synthetic["nodes"][4]["shape"] == "diamond"

    history = fixtures["history"]["folded_rework"]
    history_graph = fixtures["graphs"]["folded_rework"]
    assert history_graph["source"]["pinned"] is False
    assert history_graph["layout"] == expected_layout
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

    assert "function deepFreeze(value)" in HARNESS
    assert "Object.getOwnPropertyNames(value).forEach(key => deepFreeze(value[key]));" in HARNESS
    assert "const CONFORMANCE_FIXTURES = deepFreeze(JSON.parse(String.raw`" in HARNESS
    assert "const CONFORMANCE_FIXTURES = Object.freeze(JSON.parse(String.raw`" not in HARNESS

    result = subprocess.run(
        ["node", "--check", str(HARNESS_PATH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_harness_recursively_freezes_nested_fixture_state_without_browser_proof() -> None:
    deep_freeze_start = HARNESS.index("function deepFreeze(value)")
    deep_freeze_end = HARNESS.index("const CONFORMANCE_FIXTURES", deep_freeze_start)
    deep_freeze_source = HARNESS[deep_freeze_start:deep_freeze_end]
    fixture_json = json.dumps(_fixtures(), separators=(",", ":"))
    script = f"""
{deep_freeze_source}
const fixtures = deepFreeze({fixture_json});
const nested = [
  fixtures,
  fixtures.projects_response.runtime_config,
  fixtures.graphs.synthetic_parallel_join,
  fixtures.graphs.synthetic_parallel_join.nodes,
  fixtures.graphs.synthetic_parallel_join.nodes[0].needs,
  fixtures.graphs.folded_rework.layout,
];
if (nested.some(value => !Object.isFrozen(value))) process.exit(1);
"""
    result = subprocess.run(
        ["node", "-e", script],
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


def test_api_recipe_projections_preserve_authoritative_status() -> None:
    start = HARNESS.index("const apiRecipeFixtures = deepFreeze({")
    end = HARNESS.index("const STABLE_DOM_ATTRIBUTES", start)
    projections = HARNESS[start:end]
    library = load_library(ROOT / "recipes", persist=False)

    keys = ("dev-pipeline@14", "creative-video@1")
    for index, key in enumerate(keys):
        block_start = projections.index(f'  "{key}": {{')
        next_start = (
            projections.index(f'  "{keys[index + 1]}": {{', block_start)
            if index + 1 < len(keys)
            else len(projections)
        )
        block = projections[block_start:next_start]
        status = library.get(key).document["status"]
        assert f'    status: "{status}",' in block


def test_bundle_implements_payload_driven_native_svg_graph_renderer() -> None:
    """The renderer is source-checked until W3 provides rendered-browser proof."""
    for symbol in (
        "function GraphRenderer",
        "function GraphInspector",
        "function graphLayout",
        "function graphRanks",
        "function graphEdgePath",
        "function graphEdges",
    ):
        assert symbol in BUNDLE

    for svg_tag in ('"svg"', '"defs"', '"marker"', '"g"', '"rect"', '"polygon"', '"path"', '"text"'):
        assert "h(" + svg_tag in BUNDLE

    for attribute in (
        "data-graph-node", "data-step-id", "data-activation",
        "data-graph-edge", "data-edge-from", "data-edge-to", "data-edge-kind",
    ):
        assert attribute in BUNDLE

    for semantic in (
        "tabIndex: 0",
        "aria-label",
        "aria-labelledby",
        "onKeyDown",
        'event.key === "Enter"',
        'event.key === " "',
        'event.key === "Escape"',
        "operator-only",
        "review_rework",
        "rework_edges",
        "projection_only",
        "factory-graph-node--unsupported",
    ):
        assert semantic in BUNDLE

    for layout_field in (
        "direction", "rank_gap", "lane_gap",
        "node_width", "node_height", "diamond_size",
    ):
        assert "layout." + layout_field in BUNDLE

    assert 'schema_version !== "shipfactory.graph/v1"' in BUNDLE
    assert "Graph layout is unavailable" in BUNDLE
    assert "dangerouslySetInnerHTML" not in BUNDLE
    assert ".innerHTML" not in BUNDLE


def test_bundle_locks_graph_geometry_and_payload_semantics() -> None:
    """Lock the W2-D formulas/classes without claiming browser rendering proof."""
    assert "if (index) totalMajor += layout.rank_gap;" in BUNDLE
    assert "majorOffset[rank] = totalMajor;" in BUNDLE
    assert "totalMajor += rankMajorSizes[rank];" in BUNDLE
    assert "node.primitive === \"join\" ? \"factory-graph-join\" : \"\"" in BUNDLE
    assert (
        'points: (box.width / 2) + ",0 " + box.width + "," + (box.height / 2) + " " '
        '+ (box.width / 2) + "," + box.height + " 0," + (box.height / 2)'
    ) in BUNDLE


def test_renderer_does_not_add_a_workflow_definition_or_mutation_surface() -> None:
    renderer = BUNDLE[BUNDLE.index("function graphLayout"):BUNDLE.index("function useReportViewMeta")]
    assert "agent_task" not in renderer
    assert "approval_gate" not in renderer
    assert "/projects" not in renderer
    assert 'method: "POST"' not in renderer
    assert 'method: "PUT"' not in renderer
    assert 'method: "DELETE"' not in renderer
    assert "graph_node_width" not in renderer
    assert "graph_node_height" not in renderer
    assert "graph_diamond_size" not in renderer


def test_bundle_projects_is_primary_server_configured_launch_surface() -> None:
    projects = BUNDLE[BUNDLE.index("function validateProjectsRuntimeConfig"):BUNDLE.index("var VIEW_REGISTRY")]
    assert 'var _a = useState("projects")' in BUNDLE
    for symbol in (
        "function ProjectsView", "function ProjectDetails", "function ProjectRecipeCard",
        "function ProjectLaunchPanel", "function validateProjectsRuntimeConfig",
        "function canonicalRecipePolicy", "function GraphRenderer",
    ):
        assert symbol in BUNDLE
    for endpoint in (
        'request("/projects")',
        '"/projects/" + encodeURIComponent(projectId) + "/recipes"',
        '"/projects/" + encodeURIComponent(project.id) + "/recipe-policy"',
        '"/projects/" + encodeURIComponent(project.id) + "/flights"',
        '"/recipes/" + encodeURIComponent(selected.id) + "/versions/"',
        '"/instances/" + encodeURIComponent(launchResult.instance_id) + "/graph"',
    ):
        assert endpoint in BUNDLE
    for config_field in (
        "runtime_config", "policy_editing_enabled", "launch_enabled", "graph_enabled",
        "live_overlay_enabled", "history_enabled", "recent_flight_limit",
        "ui_refresh_interval_seconds", "history_fold_threshold", "graph_direction",
    ):
        assert config_field in projects
    for attribute in (
        "data-project-id", "data-project-launch", "data-recipe-key",
        "data-recipe-attach", "data-recipe-detach", "data-recipe-default",
        "data-flight-instance-id", "data-unclassified",
    ):
        assert attribute in projects
    assert 'skip_steps: skips.slice().sort()' in projects
    assert 'idempotency_key: idempotencyKey' in projects
    assert 'setIdempotencyKey(newNonce())' in projects
    assert 'body: JSON.stringify(next)' in projects
    assert '"board"' not in projects
    assert '"collector"' not in projects
    assert "dangerouslySetInnerHTML" not in projects
    assert ".innerHTML" not in projects


def test_projects_css_declares_responsive_primary_surface() -> None:
    for selector in (
        ".factory-projects-view", ".factory-projects-layout", ".factory-project-list",
        ".factory-project-detail", ".factory-project-row", ".factory-project-recipe",
    ):
        assert selector in CSS
    assert "grid-template-columns: minmax(15rem" in CSS
    assert "@media (max-width: 860px)" in CSS


def test_projects_resource_contracts_preserve_data_and_expose_catalog_failures() -> None:
    projects = BUNDLE[BUNDLE.index("function useProjectsResource"):BUNDLE.index("function ProjectRow")]
    catalog = BUNDLE[BUNDLE.index("function useProjectRecipeCatalogResource"):BUNDLE.index("function ProjectRow")]

    projects_catch = projects[projects.index("}).catch(function (err)"):projects.index("}).finally", projects.index("}).catch(function (err)"))]
    assert "setData(null)" not in projects_catch
    assert "setError(errorText(err))" in projects_catch
    assert "return { data: data, loading: loading, error: error" in catalog
    assert "Recipe catalog response is incomplete." in catalog
    assert 'request("/recipes")' in catalog
    assert "reload: load" in catalog


def test_projects_recipe_catalog_ui_contract_is_explicit_and_identity_safe() -> None:
    projects = BUNDLE[BUNDLE.index("function ProjectDetails"):BUNDLE.index("function ProjectsView")]
    controls = BUNDLE[BUNDLE.index("function ProjectPolicyControls"):BUNDLE.index("function ProjectRecipeCard")]
    recipe_card = BUNDLE[BUNDLE.index("function ProjectRecipeCard"):BUNDLE.index("function ProjectLaunchPanel")]

    assert "project.binding === \"bound\" && runtime.policy_editing_enabled" in projects
    assert "catalogResource.error" in projects
    assert 'title: "Recipe catalog unavailable"' in projects
    assert "onRetry: catalogResource.reload" in projects
    assert "catalogResource.enabled && catalogResource.error ? null" in projects
    assert 'recipe.recipe_hash ? h("span"' in recipe_card
    assert 'var inactive = !attached && recipe.status !== "active"' in controls
    assert "disabled = !props.enabled || props.busy || inactive" in controls
    assert "Attach disabled: recipe is inactive." in controls
    assert "Inactive — cannot attach" in controls
    assert "setLaunchResult(null)" in projects
    assert "setGraph(null)" in projects
    assert "[project.id, selected && selected.id, selected && selected.version, selected && selected.recipe_hash]" in projects
    identity_reset = projects[projects.index("setLaunchResult(null)"):projects.index("useEffect(function () {", projects.index("setLaunchResult(null)"))]
    assert "setIdempotencyKey" not in identity_reset
