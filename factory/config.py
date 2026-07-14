"""Factory seat configuration loading and validation."""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

AGENT_ROLES = frozenset({"ceo", "cto", "cmo", "cfo", "security", "engineer", "designer", "pm", "qa", "devops", "researcher", "general"})
EXECUTORS = frozenset({"hermes", "codex", "claude"})
SELECTOR_DEFAULTS = {
    "enabled": True,
    "max_per_tick": 3,
    "selection_allowance": 5_000,
}


class FactoryConfigError(ValueError):
    """Raised when the Factory seat configuration is invalid."""


@dataclass(frozen=True)
class Seat:
    """Configuration for one named Factory seat."""

    name: str
    profile: str
    executor: str
    model: str = ""
    reasoning: str = ""
    reports_to: str | None = None
    role: str = "general"
    max_concurrent: int = 1


@dataclass(frozen=True)
class FactoryConfig:
    """Validated company, seat registry, and hierarchy gates."""

    company: str
    seats: dict[str, Seat]
    hierarchy_gates: dict
    recipes: dict[str, Any] | None = None


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    if text in {"null", "Null", "NULL", "~"}:
        return None
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.startswith("["):
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return [item.strip().strip("'\"") for item in text[1:-1].split(",") if item.strip()]
    try:
        return int(text)
    except ValueError:
        return text.strip("'\"")


def _parse_yaml(text: str) -> dict[str, Any]:
    """Parse the small mapping/list YAML subset used by ``seats.yaml``."""
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    try:
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            return parsed
    except yaml.YAMLError as exc:
        raise FactoryConfigError(f"invalid YAML: {exc}") from exc
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if "\t" in raw[:indent] or ":" not in line:
            raise FactoryConfigError(f"invalid YAML at line {number}")
        key, value = line.strip().split(":", 1)
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        parsed = _scalar(value)
        parent[key.strip()] = parsed
        if isinstance(parsed, dict):
            stack.append((indent, parsed))
    return root


def _default_path() -> Path:
    """Return the configured Factory seats file path."""
    home = os.environ.get("HERMES_HOME")
    if not home:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    return Path(home) / "factory" / "seats.yaml"


def load_seats(path=None) -> FactoryConfig:
    """Load and validate a Factory configuration from YAML."""
    source = Path(path) if path is not None else _default_path()
    try:
        raw = _parse_yaml(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FactoryConfigError(f"cannot read seats config {source}: {exc}") from exc
    seats_raw = raw.get("seats", {})
    if not isinstance(seats_raw, dict):
        raise FactoryConfigError("seats must be a mapping")
    try:
        seats = {name: Seat(name=name, **(values or {})) for name, values in seats_raw.items()}
    except (TypeError, ValueError) as exc:
        raise FactoryConfigError(f"invalid seat: {exc}") from exc
    recipes = raw.get("recipes", {}) or {}
    if not isinstance(recipes, dict):
        raise FactoryConfigError("recipes must be a mapping")
    cfg = FactoryConfig(str(raw.get("company", "")), seats, raw.get("hierarchy_gates", {}) or {}, recipes)
    validate(cfg)
    return cfg


def validate(cfg) -> None:
    """Validate required values, profiles, executors, roles, and hierarchy."""
    if not isinstance(cfg.company, str) or not cfg.company.strip():
        raise FactoryConfigError("company must be a non-empty string")
    for name, seat in cfg.seats.items():
        if not name or seat.name != name:
            raise FactoryConfigError(f"seat name mismatch: {name!r}")
        if not seat.profile:
            raise FactoryConfigError(f"seat {name!r}: profile is required")
        try:
            from hermes_cli.profiles import profile_exists
        except ImportError:
            profile_exists = None
        if profile_exists is not None and not profile_exists(seat.profile):
            raise FactoryConfigError(f"seat {name!r}: profile {seat.profile!r} does not exist")
        if seat.executor not in EXECUTORS:
            raise FactoryConfigError(f"seat {name!r}: unknown executor {seat.executor!r}")
        if seat.role not in AGENT_ROLES:
            raise FactoryConfigError(f"seat {name!r}: unknown role {seat.role!r}")
        if not isinstance(seat.max_concurrent, int) or isinstance(seat.max_concurrent, bool) or seat.max_concurrent < 1:
            raise FactoryConfigError(f"seat {name!r}: max_concurrent must be a positive integer")
        if seat.reports_to and seat.reports_to not in cfg.seats:
            raise FactoryConfigError(f"seat {name!r}: reports_to {seat.reports_to!r} is unknown")
    from .hierarchy import validate_acyclic
    try:
        validate_acyclic(cfg)
    except (KeyError, ValueError) as exc:
        raise FactoryConfigError(str(exc)) from exc
    for gate in ("landers", "verdicts"):
        values = cfg.hierarchy_gates.get(gate, [])
        if not isinstance(values, list):
            raise FactoryConfigError(f"hierarchy_gates.{gate} must be a list")
        unknown = [name for name in values if name not in cfg.seats]
        if unknown:
            raise FactoryConfigError(f"hierarchy_gates.{gate} contains unknown seat {unknown[0]!r}")
    recipes = cfg.recipes or {}
    if recipes and not isinstance(recipes.get("enabled", False), bool):
        raise FactoryConfigError("recipes.enabled must be boolean")
    if recipes.get("enabled"):
        for field in ("library_path", "bare_task_recipe", "notify_target", "board_day_token_ceiling", "dispatcher_max_in_progress", "execution_profiles"):
            if field not in recipes:
                raise FactoryConfigError(f"recipes.{field} is required when enabled")
        profiles = recipes["execution_profiles"]
        if not isinstance(profiles, dict) or not profiles:
            raise FactoryConfigError("recipes.execution_profiles must be nonempty mapping")
        for name, profile in profiles.items():
            if not isinstance(profile, dict) or set(profile) != {"max_runtime_seconds", "max_retries", "token_allowance"} or any(not isinstance(profile[x], int) or profile[x] < 1 for x in profile):
                raise FactoryConfigError(f"invalid execution profile {name!r}")
        selector = recipes.get("selector", {}) or {}
        if not isinstance(selector, dict) or set(selector) - set(SELECTOR_DEFAULTS):
            raise FactoryConfigError("recipes.selector has unknown keys")
        if "enabled" in selector and not isinstance(selector["enabled"], bool):
            raise FactoryConfigError("recipes.selector.enabled must be boolean")
        for field in ("max_per_tick", "selection_allowance"):
            if field in selector and (
                not isinstance(selector[field], int)
                or isinstance(selector[field], bool)
                or selector[field] < 1
            ):
                raise FactoryConfigError(f"recipes.selector.{field} must be a positive integer")


def selector_config(recipes: dict[str, Any] | None) -> dict[str, Any]:
    """Return validated selector settings with the ratified defaults."""
    configured = dict(((recipes or {}).get("selector", {}) or {}))
    return {**SELECTOR_DEFAULTS, **configured}


__all__ = [
    "FactoryConfig", "FactoryConfigError", "SELECTOR_DEFAULTS", "Seat",
    "load_seats", "selector_config", "validate",
]
