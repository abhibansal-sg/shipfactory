"""Fail-closed loading and validation of immutable Factory recipe YAML."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from shipfactory import store

_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOP = {"schema", "id", "version", "status", "description", "intent_tags", "supersedes", "parameters", "budgets", "steps"}
_STEP_V1 = {"id", "primitive", "title", "needs", "optional", "params"}
_STEP_V2 = {"id", "primitive", "title", "needs", "optional", "inputs", "outputs", "params"}
_INPUT_V2 = {"from", "kind", "required"}
_OUTPUT_V2 = {"kind", "schema", "path"}
_PRIMITIVES = {"agent_task", "review_gate", "approval_gate", "notify", "wait_for_event"}
_PARAM_TYPES = {"string", "integer", "boolean", "enum", "datetime"}
_AGENT_REQUIRED = {"seat", "instructions", "execution_profile", "workspace"}


class RecipeError(ValueError):
    """A recipe is malformed, unsafe, or differs from a published version."""


@dataclass(frozen=True)
class Recipe:
    document: dict[str, Any]
    hash: str
    seats: frozenset[str] | None = None
    profiles: frozenset[str] | None = None

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
    if isinstance(value, str):
        return set(re.findall(r"\$\{([a-z][a-z0-9_]*)\}", value))
    if isinstance(value, dict):
        return set().union(*(_substitution_names(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_substitution_names(item) for item in value)) if value else set()
    return set()


def _templated(value: Any) -> bool:
    return isinstance(value, str) and bool(re.search(r"\$\{[^}]+\}", value))


def _render(value: Any, parameters: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    for name, item in parameters.items():
        token = "${" + name + "}"
        if value == token:
            return item
        value = value.replace(token, "" if item is None else str(item))
    return value


def _validate_v2_io(step: dict[str, Any]) -> None:
    ident = step.get("id")
    inputs = step.get("inputs")
    if not isinstance(inputs, list):
        _error(f"v2 step {ident!r} inputs must be a list")
    for index, item in enumerate(inputs):
        if not isinstance(item, dict):
            _error(f"v2 step {ident!r} input {index} must be a mapping")
        unknown = sorted(set(item) - _INPUT_V2)
        missing = sorted(_INPUT_V2 - set(item))
        if unknown:
            _error(f"v2 step {ident!r} input {index} unknown keys: {', '.join(unknown)}")
        if missing:
            _error(f"v2 step {ident!r} input {index} missing keys: {', '.join(missing)}")
        if (not isinstance(item["from"], str) or not _ID.fullmatch(item["from"])
                or not isinstance(item["kind"], str) or not _ID.fullmatch(item["kind"])
                or not isinstance(item["required"], bool)):
            _error(f"invalid v2 input in step {ident!r}")
    outputs = step.get("outputs")
    if not isinstance(outputs, list):
        _error(f"v2 step {ident!r} outputs must be a list")
    kinds: set[str] = set()
    for index, item in enumerate(outputs):
        if not isinstance(item, dict):
            _error(f"v2 step {ident!r} output {index} must be a mapping")
        unknown = sorted(set(item) - _OUTPUT_V2)
        missing = sorted(_OUTPUT_V2 - set(item))
        if unknown:
            _error(f"v2 step {ident!r} output {index} unknown keys: {', '.join(unknown)}")
        if missing:
            _error(f"v2 step {ident!r} output {index} missing keys: {', '.join(missing)}")
        kind, schema, path = item["kind"], item["schema"], item["path"]
        if not isinstance(kind, str) or not _ID.fullmatch(kind) or kind in kinds:
            _error(f"invalid or duplicate v2 output kind in step {ident!r}")
        kinds.add(kind)
        if (not isinstance(schema, str)
                or not re.fullmatch(rf"shipfactory\.{re.escape(kind)}/v[1-9][0-9]*", schema)):
            _error(f"v2 output schema does not match kind {kind!r}")
        if not isinstance(path, str) or "\\" in path:
            _error(f"invalid v2 output path in step {ident!r}")
        parsed = PurePosixPath(path)
        if (parsed.is_absolute() or len(parsed.parts) < 2
                or parsed.parts[0] != ".shipfactory-output"
                or ".." in parsed.parts or path.endswith("/")):
            _error(f"v2 output path must stay under .shipfactory-output/ in step {ident!r}")


def validate(document: Any, *, seats: set[str] | None = None, profiles: set[str] | None = None) -> dict[str, Any]:
    """Validate recipe v1 or v2 exactly; never repair or coerce input."""
    if not isinstance(document, dict) or set(document) != _TOP:
        _error("recipe top-level keys must exactly match schema")
    schema = document["schema"]
    if schema not in {"shipfactory.recipe/v1", "shipfactory.recipe/v2"}: _error("unsupported recipe schema")
    v2 = schema == "shipfactory.recipe/v2"
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
        if not isinstance(step, dict): _error("step must be a mapping")
        expected_keys = _STEP_V2 if v2 else _STEP_V1
        unknown = sorted(set(step) - expected_keys)
        missing = sorted(expected_keys - set(step))
        if unknown:
            suffix = " in v2" if v2 else ""
            _error(f"unknown step keys{suffix}: {', '.join(unknown)}")
        if missing:
            suffix = " in v2" if v2 else ""
            _error(f"missing step keys{suffix}: {', '.join(missing)}")
        ident = step.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in known: _error("invalid or duplicate step id")
        if step.get("primitive") not in _PRIMITIVES or not isinstance(step.get("title"), str) or not isinstance(step.get("needs"), list) or not all(isinstance(x, str) for x in step["needs"]) or not isinstance(step.get("optional"), bool) or not isinstance(step.get("params"), dict): _error(f"invalid step {ident!r}")
        if v2:
            _validate_v2_io(step)
        known[ident] = step
    for step in steps:
        if any(n not in known or n == step["id"] for n in step["needs"]): _error(f"unknown/self need in {step['id']}")
        if v2:
            for item in step["inputs"]:
                if item["from"] not in known:
                    _error(f"v2 input in {step['id']!r} references nonexistent producer {item['from']!r}")
                if item["from"] == step["id"]:
                    _error(f"v2 input in {step['id']!r} references itself")
                produced = {output["kind"] for output in known[item["from"]]["outputs"]}
                if item["kind"] not in produced:
                    _error(
                        f"v2 input in {step['id']!r} references producer "
                        f"{item['from']!r} without output kind {item['kind']!r}"
                    )
        primitive, params = step["primitive"], step["params"]
        if primitive in {"agent_task", "review_gate"}:
            allowed = _AGENT_REQUIRED | ({"access_mode", "environment"} if v2 else set())
            if (not _AGENT_REQUIRED <= set(params) or set(params) - allowed
                    or (not v2 and set(params) != _AGENT_REQUIRED)):
                _error(f"{primitive} params are exact")
            if ("access_mode" in params
                    and (not isinstance(params["access_mode"], str)
                         or params["access_mode"] not in ("readonly", "workspace_write"))):
                _error("access_mode must be readonly or workspace_write")
            if "environment" in params and not isinstance(params["environment"], str):
                _error("environment must be string")
            if not _templated(params["workspace"]) and params["workspace"] not in {"worktree", "shared"}: _error("workspace must be worktree or shared")
            if seats is not None and not _templated(params["seat"]) and params["seat"] not in seats: _error(f"unknown seat {params['seat']!r}")
            if profiles is not None and not _templated(params["execution_profile"]) and params["execution_profile"] not in profiles: _error(f"unknown profile {params['execution_profile']!r}")
        elif primitive == "approval_gate":
            if set(params) != {"approvers", "instructions"} or not isinstance(params["approvers"], list) or not params["approvers"]: _error("approval_gate params are exact")
            if seats is not None and any(not _templated(x) and x not in seats for x in params["approvers"]): _error("unknown approver")
            if step["optional"]: _error("approval_gate cannot be optional")
        elif primitive == "notify":
            if set(params) != {"target", "message"}: _error("notify params are exact")
        else:
            if set(params) - {"event", "due_at"} or not isinstance(params.get("event"), str) or (params["event"] == "timer" and not params.get("due_at")): _error("invalid wait_for_event params")
        for value in [step["title"], *params.values()]:
            if not _substitution_names(value) <= set(parameters): _error(f"missing parameter substitution in {step['id']}")
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
        recipes[key] = Recipe(
            document,
            digest,
            frozenset(seats) if seats is not None else None,
            frozenset(profiles) if profiles is not None else None,
        )
    return RecipeLibrary(recipes)


def bind_parameters(recipe: Recipe, provided: dict[str, Any], skip_steps: list[str] | None = None,
                    *, seats: set[str] | frozenset[str] | None = None,
                    profiles: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
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
    seats = recipe.seats if seats is None else seats
    profiles = recipe.profiles if profiles is None else profiles
    for step in recipe.document["steps"]:
        params = step["params"]
        if step["primitive"] in {"agent_task", "review_gate"}:
            seat = _render(params["seat"], bound)
            profile = _render(params["execution_profile"], bound)
            workspace = _render(params["workspace"], bound)
            if seats is not None and seat not in seats:
                _error(f"unknown seat {seat!r}")
            if profiles is not None and profile not in profiles:
                _error(f"unknown profile {profile!r}")
            if workspace not in {"worktree", "shared"}:
                _error("workspace must be worktree or shared")
        elif step["primitive"] == "approval_gate" and seats is not None:
            approvers = [_render(value, bound) for value in params["approvers"]]
            if any(value not in seats for value in approvers):
                _error("unknown approver")
    return bound
