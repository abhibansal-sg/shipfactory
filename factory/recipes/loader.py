"""Fail-closed loading and validation of immutable Factory recipe YAML."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from factory import store

_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOP = {"schema", "id", "version", "status", "description", "intent_tags", "supersedes", "parameters", "budgets", "steps"}
_STEP = {"id", "primitive", "title", "needs", "optional", "params"}
_PRIMITIVES = {"agent_task", "review_gate", "approval_gate", "notify", "wait_for_event"}
_PARAM_TYPES = {"string", "integer", "boolean", "enum", "datetime"}
_AGENT_REQUIRED = {"seat", "instructions", "execution_profile", "workspace"}


class RecipeError(ValueError):
    """A recipe is malformed, unsafe, or differs from a published version."""


@dataclass(frozen=True)
class Recipe:
    document: dict[str, Any]
    hash: str

    @property
    def key(self) -> str:
        return f"{self.document['id']}@{self.document['version']}"


class RecipeLibrary:
    def __init__(self, recipes: dict[str, Recipe]):
        self.recipes = recipes

    def get(self, key: str) -> Recipe:
        try:
            return self.recipes[key]
        except KeyError as exc:
            raise RecipeError(f"unknown recipe {key!r}") from exc

    def active_manifest(self) -> list[dict[str, Any]]:
        return [r.document for r in self.recipes.values() if r.document["status"] == "active"]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _error(message: str) -> None:
    raise RecipeError(message)


def _substitution_names(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return set(re.findall(r"\$\{([a-z][a-z0-9_]*)\}", value))


def validate(document: Any, *, seats: set[str] | None = None, profiles: set[str] | None = None) -> dict[str, Any]:
    """Validate exactly the v1 schema; no repair or permissive coercion occurs."""
    if not isinstance(document, dict) or set(document) != _TOP:
        _error("recipe top-level keys must exactly match factory.recipe/v1")
    if document["schema"] != "factory.recipe/v1": _error("unsupported recipe schema")
    if not isinstance(document["id"], str) or not _ID.fullmatch(document["id"]): _error("invalid recipe id")
    if not isinstance(document["version"], int) or isinstance(document["version"], bool) or document["version"] < 1: _error("version must be positive integer")
    if document["status"] not in {"active", "deprecated"}: _error("invalid recipe status")
    if not isinstance(document["description"], str) or not isinstance(document["intent_tags"], list) or not all(isinstance(x, str) for x in document["intent_tags"]): _error("invalid description or intent_tags")
    supersedes = document["supersedes"]
    if supersedes is not None and (not isinstance(supersedes, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}@[1-9][0-9]*", supersedes)): _error("invalid supersedes")
    parameters = document["parameters"]
    if not isinstance(parameters, dict): _error("parameters must be mapping")
    for name, spec in parameters.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(name)) or not isinstance(spec, dict) or set(spec) - {"type", "required", "default", "values"}:
            _error(f"invalid parameter {name!r}")
        if spec.get("type") not in _PARAM_TYPES or not isinstance(spec.get("required"), bool): _error(f"invalid parameter {name!r}")
        if spec["type"] == "enum" and (not isinstance(spec.get("values"), list) or not spec["values"]): _error(f"enum {name!r} requires values")
    budgets = document["budgets"]
    if not isinstance(budgets, dict) or set(budgets) != {"max_activations", "max_step_activations", "max_tokens"} or any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in budgets.values()): _error("invalid budgets")
    steps = document["steps"]
    if not isinstance(steps, list) or not steps: _error("steps must be nonempty list")
    known: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict) or set(step) != _STEP: _error("step keys must exactly match schema")
        ident = step.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in known: _error("invalid or duplicate step id")
        if step.get("primitive") not in _PRIMITIVES or not isinstance(step.get("title"), str) or not isinstance(step.get("needs"), list) or not all(isinstance(x, str) for x in step["needs"]) or not isinstance(step.get("optional"), bool) or not isinstance(step.get("params"), dict): _error(f"invalid step {ident!r}")
        known[ident] = step
    for step in steps:
        if any(n not in known or n == step["id"] for n in step["needs"]): _error(f"unknown/self need in {step['id']}")
        primitive, params = step["primitive"], step["params"]
        if primitive in {"agent_task", "review_gate"}:
            if set(params) != _AGENT_REQUIRED: _error(f"{primitive} params are exact")
            if params["workspace"] not in {"worktree", "shared"}: _error("workspace must be worktree or shared")
            if seats is not None and params["seat"] not in seats: _error(f"unknown seat {params['seat']!r}")
            if profiles is not None and params["execution_profile"] not in profiles: _error(f"unknown profile {params['execution_profile']!r}")
        elif primitive == "approval_gate":
            if set(params) != {"approvers", "instructions"} or not isinstance(params["approvers"], list) or not params["approvers"]: _error("approval_gate params are exact")
            if seats is not None and any(x not in seats for x in params["approvers"]): _error("unknown approver")
            if step["optional"]: _error("approval_gate cannot be optional")
        elif primitive == "notify":
            if set(params) != {"target", "message"}: _error("notify params are exact")
        else:
            if set(params) - {"event", "due_at"} or not isinstance(params.get("event"), str) or (params["event"] == "timer" and not params.get("due_at")): _error("invalid wait_for_event params")
        for text in [step["title"], *[v for v in params.values() if isinstance(v, str)]]:
            if not _substitution_names(text) <= set(parameters): _error(f"missing parameter substitution in {step['id']}")
    # directed cycle check and v1 shared-workspace total ordering rule
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting: _error("recipe dependency cycle")
        if node not in visited:
            visiting.add(node)
            for parent in known[node]["needs"]: visit(parent)
            visiting.remove(node); visited.add(node)
    for node in known: visit(node)
    ancestors: dict[str, set[str]] = {}
    def ups(node: str) -> set[str]:
        if node not in ancestors: ancestors[node] = set(known[node]["needs"]) | set().union(*(ups(x) for x in known[node]["needs"])) if known[node]["needs"] else set()
        return ancestors[node]
    shared = [x for x in steps if x["primitive"] in {"agent_task", "review_gate"} and x["params"]["workspace"] == "shared"]
    for index, left in enumerate(shared):
        for right in shared[index + 1:]:
            if left["id"] not in ups(right["id"]) and right["id"] not in ups(left["id"]): _error("shared workspace steps must be totally ordered")
    return document


def load_library(path: str | Path, *, seats: set[str] | None = None, profiles: set[str] | None = None, persist: bool = True) -> RecipeLibrary:
    """Load a directory of recipes and pin each normalized document immutably."""
    recipes: dict[str, Recipe] = {}
    for source in sorted(Path(path).glob("*.y*ml")):
        try: document = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc: raise RecipeError(f"cannot load {source}: {exc}") from exc
        validate(document, seats=seats, profiles=profiles)
        normalized = _canonical(document); digest = hashlib.sha256(normalized.encode()).hexdigest(); key = f"{document['id']}@{document['version']}"
        if key in recipes: _error(f"duplicate recipe {key}")
        if persist:
            store.init_db()
            with store._connect() as conn:  # atomic immutable publication check
                row = conn.execute("SELECT hash FROM recipe_versions WHERE id=? AND version=?", (document["id"], document["version"])).fetchone()
                if row and row["hash"] != digest: _error(f"published recipe {key} is immutable")
                conn.execute("INSERT OR IGNORE INTO recipe_versions(id,version,hash,status,normalized_yaml,created_at) VALUES(?,?,?,?,?,?)", (document["id"], document["version"], digest, document["status"], normalized, store._now()))
        recipes[key] = Recipe(document, digest)
    return RecipeLibrary(recipes)


def bind_parameters(recipe: Recipe, provided: dict[str, Any], skip_steps: list[str] | None = None) -> dict[str, Any]:
    """Type-check instantiation values and reject illegal skips, fail closed."""
    spec = recipe.document["parameters"]
    if set(provided) - set(spec): _error("unknown recipe parameters")
    bound: dict[str, Any] = {}
    for name, desc in spec.items():
        value = provided.get(name, desc.get("default"))
        if value is None and desc["required"]: _error(f"missing required parameter {name}")
        typ = desc["type"]
        good = value is None or (typ == "string" and isinstance(value, str)) or (typ == "integer" and isinstance(value, int) and not isinstance(value, bool)) or (typ == "boolean" and isinstance(value, bool)) or (typ == "enum" and value in desc["values"])
        if typ == "datetime" and value is not None:
            try: datetime.fromisoformat(str(value).replace("Z", "+00:00")); good = True
            except ValueError: good = False
        if not good: _error(f"parameter {name} has wrong type")
        bound[name] = value
    skips = set(skip_steps or [])
    steps = {x["id"]: x for x in recipe.document["steps"]}
    if not skips <= set(steps) or any(not steps[x]["optional"] or steps[x]["primitive"] in {"review_gate", "approval_gate"} for x in skips): _error("invalid skip_steps")
    return bound
