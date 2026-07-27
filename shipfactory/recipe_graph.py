"""Pure projection of an already validated immutable recipe document.

This module deliberately has no Factory, database, or dashboard dependencies.
The returned graph is source policy only: it contains no instance state and no
operation which could advance a recipe.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from shipfactory.config import projects_visual_recipes_config
from shipfactory.recipes.primitives import review_verdict_targets


GRAPH_SCHEMA = "shipfactory.graph/v1"
_WORK_PRIMITIVES = {"agent_task", "review_gate", "verification", "notify"}
_DIAMOND_PRIMITIVES = {"approval_gate", "wait_for_event"}


def _shape(primitive: str) -> str:
    if primitive in _WORK_PRIMITIVES:
        return "rectangle"
    if primitive in _DIAMOND_PRIMITIVES:
        return "diamond"
    return "diamond"


def _activation_cap(recipe: Mapping[str, Any], step: Mapping[str, Any]) -> int | None:
    budgets = recipe.get("budgets", {})
    caps = budgets.get("step_activation_caps")
    if isinstance(caps, Mapping) and step["id"] in caps:
        return caps[step["id"]]
    if step.get("primitive") in {"agent_task", "review_gate"}:
        legacy_cap = budgets.get("max_step_activations")
        if isinstance(legacy_cap, int) and not isinstance(legacy_cap, bool):
            return legacy_cap
    return None


def _step_node(recipe: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
    primitive = step["primitive"]
    params = deepcopy(step.get("params", {}))
    node: dict[str, Any] = {
        "id": step["id"],
        "title": step["title"],
        "primitive": primitive,
        "shape": _shape(primitive),
        "projection_only": False,
        "optional": step["optional"],
        "needs": list(step.get("needs", [])),
        "inputs": deepcopy(step.get("inputs", [])),
        "outputs": deepcopy(step.get("outputs", [])),
        "params": params,
        "seat": params.get("seat") if primitive in {"agent_task", "review_gate"} else None,
        "execution_profile": params.get("execution_profile") if primitive in {"agent_task", "review_gate"} else None,
        "access_mode": params.get("access_mode") if primitive in {"agent_task", "review_gate"} else None,
        "environment": params.get("environment") if primitive in {"agent_task", "review_gate", "verification"} else None,
        "activation_cap": _activation_cap(recipe, step),
        "legal_rework_targets": [],
    }
    if primitive == "review_gate":
        try:
            node["legal_rework_targets"] = list(review_verdict_targets(dict(recipe), dict(step)))
        except ValueError:
            # The recipe has already passed loader validation, but a review
            # without a legal Factory target is still representable.  The
            # unsupported marker is attached by project_graph below.
            node["legal_rework_targets"] = []
    if primitive == "approval_gate":
        node["operator_only"] = True
        node["approvers"] = deepcopy(params.get("approvers", []))
    return node


class _Edges:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        input_kind: str | None = None,
        label: str | None = None,
        projection_only: bool = False,
        reason: str | None = None,
    ) -> None:
        key = (source, target)
        edge = self._items.get(key)
        if edge is None:
            edge = {
                "id": "",
                "from": source,
                "to": target,
                "kind": kind,
                "kinds": [],
                "label": "",
                "projection_only": projection_only,
            }
            self._items[key] = edge
        if kind not in edge["kinds"]:
            edge["kinds"].append(kind)
        if input_kind is not None and input_kind not in edge.setdefault("input_kinds", []):
            edge["input_kinds"].append(input_kind)
        if label is not None:
            edge.setdefault("labels", []).append(label)
        if reason is not None:
            edge["reason"] = reason
        edge["projection_only"] = edge["projection_only"] or projection_only

    def finish(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for edge in self._items.values():
            kinds = edge["kinds"]
            edge["id"] = f"{edge['from']}->{edge['to']}:{'+'.join(kinds)}"
            explicit_labels = edge.pop("labels", [])
            if explicit_labels:
                edge["label"] = " + ".join(dict.fromkeys(explicit_labels))
            elif edge.get("input_kinds"):
                labels = [kind for kind in kinds if kind != "input"]
                labels.extend(f"input:{item}" for item in edge["input_kinds"])
                edge["label"] = " + ".join(labels)
            else:
                edge["label"] = " + ".join(kinds)
            result.append(edge)
        return result


def _router_node(review: Mapping[str, Any], targets: list[str]) -> dict[str, Any]:
    return {
        "id": f"{review['id']}:verdict",
        "title": f"{review['title']} verdict",
        "primitive": "review_verdict_router",
        "shape": "diamond",
        "projection_only": True,
        "optional": False,
        "needs": [review["id"]],
        "inputs": [],
        "outputs": [],
        "params": {},
        "seat": None,
        "execution_profile": None,
        "access_mode": None,
        "environment": None,
        "activation_cap": None,
        "legal_rework_targets": list(targets),
    }


def _unsupported_node(review: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": f"{review['id']}:unsupported",
        "title": f"Unsupported {review['title']} rework",
        "primitive": "unsupported",
        "shape": "diamond",
        "projection_only": True,
        "optional": False,
        "needs": [],
        "inputs": [],
        "outputs": [],
        "params": {},
        "seat": None,
        "execution_profile": None,
        "access_mode": None,
        "environment": None,
        "activation_cap": None,
        "legal_rework_targets": [],
        "state": "unsupported",
        "reason": reason,
    }


def project_graph(
    recipe: Mapping[str, Any],
    *,
    recipe_hash: str,
    pinned: bool,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic ``shipfactory.graph/v1`` recipe projection.

    ``recipe`` is intentionally not revalidated or normalized here.  Callers
    must pass the exact document that has already passed the immutable recipe
    loader; this function only copies declared policy into a graph.  ``config``
    is the fresh effective Projects/visual-recipes configuration; omitting it
    uses the validated defaults for compatibility with existing callers.
    """
    steps = list(recipe["steps"])
    nodes = [_step_node(recipe, step) for step in steps]
    edges = _Edges()

    for step in steps:
        for parent in step.get("needs", []):
            edges.add(parent, step["id"], "needs")
        for item in step.get("inputs", []):
            edges.add(item["from"], step["id"], "input", input_kind=item["kind"])

    # Synthetic routers are appended after the declared recipe order so the
    # source order of every real step remains directly inspectable.
    for step, node in zip(steps, nodes):
        if step["primitive"] != "review_gate":
            continue
        targets = node["legal_rework_targets"]
        router = _router_node(step, targets)
        nodes.append(router)
        edges.add(step["id"], router["id"], "needs", projection_only=True)
        if targets:
            for target in targets:
                edges.add(
                    router["id"], target, "review_rework",
                    label=f"request_changes -> {target}", projection_only=True,
                )
        else:
            reason = f"review gate {step['id']!r} has no legal Factory rework target"
            unsupported = _unsupported_node(step, reason)
            nodes.append(unsupported)
            edges.add(
                router["id"], unsupported["id"], "review_rework",
                label="unsupported: no legal Factory rework target",
                projection_only=True, reason=reason,
            )

    effective_config = projects_visual_recipes_config(None) if config is None else dict(config)
    return {
        "schema_version": GRAPH_SCHEMA,
        "source": {
            "recipe_id": recipe["id"],
            "version": recipe["version"],
            "recipe_key": f"{recipe['id']}@{recipe['version']}",
            "recipe_hash": recipe_hash,
            "pinned": pinned,
        },
        "nodes": nodes,
        "edges": edges.finish(),
        "layout": {
            "direction": effective_config["graph_direction"],
            "rank_gap": effective_config["graph_rank_gap"],
            "lane_gap": effective_config["graph_lane_gap"],
            "node_width": effective_config["graph_node_width"],
            "node_height": effective_config["graph_node_height"],
            "diamond_size": effective_config["graph_diamond_size"],
        },
    }
