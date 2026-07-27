from __future__ import annotations

import copy
from pathlib import Path

from shipfactory.config import PROJECTS_VISUAL_RECIPES_DEFAULTS, projects_visual_recipes_config
from shipfactory.recipe_graph import project_graph
from shipfactory.recipes.loader import load_library, validate
from shipfactory.recipes.primitives import review_verdict_targets


ROOT = Path(__file__).resolve().parents[1]


def _recipe(key: str) -> tuple[dict, str]:
    loaded = load_library(ROOT / "recipes", persist=False).get(key)
    return loaded.document, loaded.hash


def _synthetic_recipe(*, root_review: bool = False) -> dict:
    steps = [
        {
            "id": "left",
            "primitive": "agent_task",
            "title": "Left branch",
            "needs": [],
            "optional": False,
            "inputs": [],
            "outputs": [{"kind": "left-result", "schema": "shipfactory.left-result/v1", "path": ".shipfactory-output/left.json"}],
            "params": {"seat": "builder", "instructions": "left", "execution_profile": "build", "workspace": "worktree", "access_mode": "workspace_write", "environment": "source"},
        },
        {
            "id": "right",
            "primitive": "agent_task",
            "title": "Right branch",
            "needs": [],
            "optional": False,
            "inputs": [],
            "outputs": [{"kind": "right-result", "schema": "shipfactory.right-result/v1", "path": ".shipfactory-output/right.json"}],
            "params": {"seat": "builder", "instructions": "right", "execution_profile": "build", "workspace": "worktree", "access_mode": "workspace_write", "environment": "source"},
        },
        {
            "id": "join",
            "primitive": "agent_task",
            "title": "Join branches",
            "needs": ["left", "right"],
            "optional": False,
            "inputs": [
                {"from": "left", "kind": "left-result", "required": True},
                {"from": "right", "kind": "right-result", "required": True},
            ],
            "outputs": [{"kind": "joined", "schema": "shipfactory.joined/v1", "path": ".shipfactory-output/joined.json"}],
            "params": {"seat": "builder", "instructions": "join", "execution_profile": "build", "workspace": "worktree", "access_mode": "workspace_write", "environment": "source"},
        },
        {
            "id": "review",
            "primitive": "review_gate",
            "title": "Review join",
            "needs": [] if root_review else ["join"],
            "optional": False,
            "inputs": [] if root_review else [{"from": "join", "kind": "joined", "required": True}],
            "outputs": [],
            "params": {"seat": "reviewer", "instructions": "review", "execution_profile": "review", "workspace": "worktree", "access_mode": "readonly", "environment": "source"},
        },
        {
            "id": "approval",
            "primitive": "approval_gate",
            "title": "Approve",
            "needs": ["review"],
            "optional": False,
            "inputs": [],
            "outputs": [],
            "params": {"approvers": ["operator"], "instructions": "Human operator approval only."},
        },
    ]
    recipe = {
        "schema": "shipfactory.recipe/v2",
        "id": "synthetic-join",
        "version": 1,
        "status": "active",
        "description": "synthetic parallel join",
        "intent_tags": ["test"],
        "supersedes": None,
        "verdict_contract": "shipfactory.verdict/v2",
        "parameters": {},
        "budgets": {"max_activations": 5, "step_activation_caps": {"left": 1, "right": 1, "join": 1, "review": 1}},
        "steps": steps,
    }
    validate(recipe)
    return recipe


def _edge(graph: dict, source: str, target: str) -> dict:
    return next(edge for edge in graph["edges"] if edge["from"] == source and edge["to"] == target)


def test_published_recipe_projection_preserves_source_and_recipe_order():
    recipe, recipe_hash = _recipe("dev-pipeline@14")
    graph = project_graph(recipe, recipe_hash=recipe_hash, pinned=True)

    assert graph["schema_version"] == "shipfactory.graph/v1"
    assert graph["source"] == {
        "recipe_id": "dev-pipeline",
        "version": 14,
        "recipe_key": "dev-pipeline@14",
        "recipe_hash": recipe_hash,
        "pinned": True,
    }
    declared = [step["id"] for step in recipe["steps"]]
    projected_declared = [node["id"] for node in graph["nodes"] if not node["projection_only"]]
    assert projected_declared == declared
    assert graph["layout"] == {
        "direction": "TB",
        "rank_gap": 56,
        "lane_gap": 28,
        "node_width": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_node_width"],
        "node_height": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_node_height"],
        "diamond_size": PROJECTS_VISUAL_RECIPES_DEFAULTS["graph_diamond_size"],
    }

    for step in recipe["steps"]:
        node = next(node for node in graph["nodes"] if node["id"] == step["id"])
        assert node["params"] == step["params"]
        assert node["needs"] == step["needs"]
        assert node["inputs"] == step["inputs"]
        assert node["outputs"] == step["outputs"]

    review_steps = [step for step in recipe["steps"] if step["primitive"] == "review_gate"]
    assert [node["id"] for node in graph["nodes"] if node["primitive"] == "review_verdict_router"] == [
        f"{step['id']}:verdict" for step in review_steps
    ]
    for step in review_steps:
        node = next(node for node in graph["nodes"] if node["id"] == step["id"])
        assert node["legal_rework_targets"] == review_verdict_targets(recipe, step)


def test_typed_fanin_is_coalesced_without_losing_needs_or_input_kinds():
    recipe = _synthetic_recipe()
    graph = project_graph(recipe, recipe_hash="a" * 64, pinned=False)

    left_join = _edge(graph, "left", "join")
    right_join = _edge(graph, "right", "join")
    assert left_join["kind"] == "needs"
    assert left_join["kinds"] == ["needs", "input"]
    assert left_join["input_kinds"] == ["left-result"]
    assert left_join["label"] == "needs + input:left-result"
    assert right_join["kinds"] == ["needs", "input"]
    assert right_join["input_kinds"] == ["right-result"]
    assert len([edge for edge in graph["edges"] if edge["from"] == "left" and edge["to"] == "join"]) == 1


def test_review_router_and_operator_approval_are_projection_only_metadata():
    recipe = _synthetic_recipe()
    graph = project_graph(recipe, recipe_hash="b" * 64, pinned=True)

    review = next(node for node in graph["nodes"] if node["id"] == "review")
    router = next(node for node in graph["nodes"] if node["id"] == "review:verdict")
    approval = next(node for node in graph["nodes"] if node["id"] == "approval")
    assert review["legal_rework_targets"] == ["join"]
    assert router["primitive"] == "review_verdict_router"
    assert router["shape"] == "diamond"
    assert router["projection_only"] is True
    assert _edge(graph, "review:verdict", "join")["kind"] == "review_rework"
    assert _edge(graph, "review:verdict", "join")["projection_only"] is True
    assert approval["shape"] == "diamond"
    assert approval["seat"] is None
    assert approval["operator_only"] is True
    assert approval["approvers"] == ["operator"]


def test_unsupported_review_target_is_visible_and_not_guessed_from_instructions():
    recipe = _synthetic_recipe(root_review=True)
    recipe["steps"][3]["params"]["instructions"] = "request_changes target_step: left"
    graph = project_graph(recipe, recipe_hash="c" * 64, pinned=True)

    review = next(node for node in graph["nodes"] if node["id"] == "review")
    unsupported = next(node for node in graph["nodes"] if node["primitive"] == "unsupported")
    assert review["legal_rework_targets"] == []
    assert unsupported["reason"]
    unsupported_edges = [edge for edge in graph["edges"] if edge["to"] == unsupported["id"]]
    assert unsupported_edges and unsupported_edges[0]["kind"] == "review_rework"
    assert "left" not in str(unsupported_edges[0])


def test_projection_does_not_mutate_recipe_and_creative_video_is_supported():
    recipe, recipe_hash = _recipe("creative-video@1")
    before = copy.deepcopy(recipe)
    graph = project_graph(recipe, recipe_hash=recipe_hash, pinned=True)

    assert recipe == before
    assert [node["id"] for node in graph["nodes"] if not node["projection_only"]] == [
        step["id"] for step in recipe["steps"]
    ]
    approval = next(node for node in graph["nodes"] if node["id"] == "approval")
    assert approval["operator_only"] is True


def test_graph_uses_runtime_layout_config_instead_of_owned_magic_values():
    recipe = _synthetic_recipe()
    config = projects_visual_recipes_config({
        "projects_visual_recipes": {
            "graph_direction": "RL",
            "graph_rank_gap": 101,
            "graph_lane_gap": 43,
            "graph_node_width": 222,
            "graph_node_height": 77,
            "graph_diamond_size": 35,
        },
    })

    graph = project_graph(recipe, recipe_hash="d" * 64, pinned=False, config=config)

    assert graph["layout"] == {
        "direction": "RL",
        "rank_gap": 101,
        "lane_gap": 43,
        "node_width": 222,
        "node_height": 77,
        "diamond_size": 35,
    }
