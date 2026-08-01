from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

import yaml


_TOP_LEVEL_KEYS = frozenset({"name", "start", "boxes", "arrows"})
_TOP_LEVEL_KEYS_WITH_APPROVAL_ARTIFACT = _TOP_LEVEL_KEYS | {"approval_artifact"}
_BOX_KEYS = frozenset({"id", "name", "who", "instructions"})
_BOX_KEYS_WITH_END = _BOX_KEYS | {"end"}
_ARROW_KEYS = frozenset({"from", "result", "to"})
_RESULT_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class GraphRecipeError(ValueError):
    """A graph recipe does not conform to the V1 contract."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode,
                              deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise GraphRecipeError(f"invalid YAML key {key!r}") from exc
        if duplicate:
            raise GraphRecipeError(f"duplicate YAML key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class GraphRecipe:
    name: str
    start: str
    boxes: tuple[dict[str, object], ...]
    arrows: tuple[dict[str, object], ...]
    document: dict[str, object]
    canonical_json: str
    hash: str
    approval_artifact: str | None = None

    def box(self, box_id: str) -> dict[str, object]:
        for box in self.boxes:
            if box["id"] == box_id:
                return box
        raise GraphRecipeError(f"unknown box {box_id!r}")

    def destinations(self, box_id: str, result: str) -> tuple[str, ...]:
        for arrow in self.arrows:
            if arrow["from"] == box_id and arrow["result"] == result:
                return tuple(arrow["to"])  # type: ignore[arg-type]
        return ()

    def incoming(self, box_id: str) -> tuple[tuple[str, str], ...]:
        self.box(box_id)
        return tuple(
            (str(arrow["from"]), str(arrow["result"]))
            for arrow in self.arrows
            if box_id in arrow["to"]  # type: ignore[operator]
        )

    def is_end(self, box_id: str) -> bool:
        return self.box(box_id).get("end") is True

    def is_rework_arrow(self, source_id: str, result: str) -> bool:
        positions = {
            str(box["id"]): index for index, box in enumerate(self.boxes)
        }
        if source_id not in positions:
            raise GraphRecipeError(f"unknown box {source_id!r}")
        for arrow in self.arrows:
            if arrow["from"] == source_id and arrow["result"] == result:
                return any(
                    positions[str(destination)] <= positions[source_id]
                    for destination in arrow["to"]  # type: ignore[union-attr]
                )
        return False


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    location: str,
) -> None:
    if not isinstance(value, dict):
        raise GraphRecipeError(f"{location} must be a mapping")
    if frozenset(value) != expected:
        raise GraphRecipeError(
            f"{location} keys must be exactly {sorted(expected)!r}"
        )


def _require_non_empty_string(value: Any, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GraphRecipeError(f"{location} must be a non-empty string")
    return value


def validate(document: object) -> GraphRecipe:
    if isinstance(document, dict) and "approval_artifact" in document:
        _require_exact_keys(
            document, _TOP_LEVEL_KEYS_WITH_APPROVAL_ARTIFACT,
            location="top-level document",
        )
    else:
        _require_exact_keys(document, _TOP_LEVEL_KEYS, location="top-level document")
    assert isinstance(document, dict)
    if not isinstance(document["boxes"], list):
        raise GraphRecipeError("boxes must be a list")
    if not isinstance(document["arrows"], list):
        raise GraphRecipeError("arrows must be a list")
    _require_non_empty_string(document["name"], location="name")
    _require_non_empty_string(document["start"], location="start")

    box_ids: set[str] = set()
    for index, box in enumerate(document["boxes"]):
        if not isinstance(box, dict):
            raise GraphRecipeError(f"boxes[{index}] must be a mapping")
        expected = _BOX_KEYS_WITH_END if "end" in box else _BOX_KEYS
        _require_exact_keys(box, expected, location=f"boxes[{index}]")
        box_id = _require_non_empty_string(
            box["id"], location=f"boxes[{index}].id"
        )
        if box_id in box_ids:
            raise GraphRecipeError("box ids must be unique")
        box_ids.add(box_id)
        for field in ("name", "who", "instructions"):
            _require_non_empty_string(
                box[field], location=f"boxes[{index}].{field}"
            )
        if "end" in box and box["end"] is not True:
            raise GraphRecipeError(f"boxes[{index}].end, when present, must be true")

    route_keys: set[tuple[str, str]] = set()
    for index, arrow in enumerate(document["arrows"]):
        _require_exact_keys(arrow, _ARROW_KEYS, location=f"arrows[{index}]")
        source = _require_non_empty_string(
            arrow["from"], location=f"arrows[{index}].from"
        )
        result = arrow["result"]
        if not isinstance(result, str) or _RESULT_RE.fullmatch(result) is None:
            raise GraphRecipeError(
                f"arrows[{index}].result must match ^[a-z][a-z0-9-]*$"
            )
        route_key = (source, result)
        if route_key in route_keys:
            raise GraphRecipeError(
                "each (from, result) route must be unique because one arrow "
                "carries all destinations"
            )
        route_keys.add(route_key)

        destinations = arrow["to"]
        if not isinstance(destinations, list) or not destinations:
            raise GraphRecipeError(
                f"arrows[{index}] destinations must be a non-empty list"
            )
        checked_destinations = [
            _require_non_empty_string(
                destination,
                location=f"arrows[{index}] destination",
            )
            for destination in destinations
        ]
        if len(set(checked_destinations)) != len(checked_destinations):
            raise GraphRecipeError(
                f"arrows[{index}] destinations must be unique"
            )

    if document["start"] not in box_ids:
        raise GraphRecipeError("start must reference an existing box")

    approval_artifact = document.get("approval_artifact")
    if approval_artifact is not None:
        approval_artifact = _require_non_empty_string(
            approval_artifact, location="approval_artifact",
        )
        if approval_artifact not in box_ids:
            raise GraphRecipeError(
                "approval_artifact must reference an existing box"
            )

    outgoing: dict[str, list[str]] = {box_id: [] for box_id in box_ids}
    for index, arrow in enumerate(document["arrows"]):
        if arrow["from"] not in box_ids:
            raise GraphRecipeError(
                f"arrows[{index}].from must reference an existing box"
            )
        for destination in arrow["to"]:
            if destination not in box_ids:
                raise GraphRecipeError(
                    f"arrows[{index}] destination must reference an existing box"
                )
        outgoing[arrow["from"]].extend(arrow["to"])

    end_ids = [box["id"] for box in document["boxes"] if box.get("end") is True]
    if len(end_ids) != 1:
        raise GraphRecipeError("exactly one box must have end: true")
    end_id = end_ids[0]
    if outgoing[end_id]:
        raise GraphRecipeError("the end box must have no outgoing arrows")

    for box_id in box_ids - {end_id}:
        if not outgoing[box_id]:
            raise GraphRecipeError(
                f"non-end box {box_id!r} must have an outgoing route"
            )

    reachable: set[str] = set()
    pending = [document["start"]]
    while pending:
        box_id = pending.pop()
        if box_id in reachable:
            continue
        reachable.add(box_id)
        pending.extend(outgoing[box_id])
    if reachable != box_ids:
        missing = sorted(box_ids - reachable)
        raise GraphRecipeError(
            f"all boxes must be reachable from start; unreachable: {missing!r}"
        )

    reverse: dict[str, list[str]] = {box_id: [] for box_id in box_ids}
    for source, destinations in outgoing.items():
        for destination in destinations:
            reverse[destination].append(source)
    can_reach_end: set[str] = set()
    pending = [end_id]
    while pending:
        box_id = pending.pop()
        if box_id in can_reach_end:
            continue
        can_reach_end.add(box_id)
        pending.extend(reverse[box_id])
    if can_reach_end != box_ids:
        missing = sorted(box_ids - can_reach_end)
        raise GraphRecipeError(
            f"all boxes must be able to reach the end; unable: {missing!r}"
        )

    snapshot_document = copy.deepcopy(document)
    canonical_json = json.dumps(
        snapshot_document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    frozen_document = _freeze(snapshot_document)
    return GraphRecipe(
        name=str(frozen_document["name"]),
        start=str(frozen_document["start"]),
        boxes=tuple(frozen_document["boxes"]),
        arrows=tuple(frozen_document["arrows"]),
        document=frozen_document,
        canonical_json=canonical_json,
        hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        approval_artifact=approval_artifact,
    )


def load(path: Path) -> GraphRecipe:
    try:
        with path.open("r", encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise GraphRecipeError(f"invalid YAML: {exc}") from exc
    return validate(document)


def load_library(path: Path) -> dict[str, GraphRecipe]:
    recipes: dict[str, GraphRecipe] = {}
    files = sorted({*path.glob("*.yaml"), *path.glob("*.yml")})
    for recipe_path in files:
        recipe = load(recipe_path)
        if recipe.name in recipes:
            raise GraphRecipeError(f"duplicate recipe name {recipe.name!r} in library")
        recipes[recipe.name] = recipe
    return recipes


# Transitional aliases for callers written against the pre-review draft.
V1Recipe = GraphRecipe
validate_v1_recipe = validate


def load_v1_recipe(path: str | Path) -> GraphRecipe:
    return load(Path(path))
