"""Single write path for Factory employment contracts (seats).

``PyYAML`` does not round-trip comments, so seat writes retain the leading
comment/header block and re-serialize the YAML mapping in its existing order.
The profile config is intentionally written through this module too: a Hermes
seat without an explicit profile model would silently inherit the global
default (finding #12).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from factory.config import EXECUTORS, FactoryConfigError


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _seats_path() -> Path:
    return _hermes_home() / "factory" / "seats.yaml"


def _profile_path(profile: str) -> Path:
    # ``default`` is Hermes's unprofiled/default configuration.
    return _hermes_home() / "config.yaml" if profile == "default" else _hermes_home() / "profiles" / profile / "config.yaml"


def list_profiles() -> list[str]:
    """Return the available labor pool, including Hermes's default profile."""
    root = _hermes_home() / "profiles"
    profiles = [path.name for path in root.iterdir() if path.is_dir()] if root.is_dir() else []
    return ["default", *sorted(set(profiles) - {"default"})]


def _header(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "".join(lines)


def _load_mapping(path: Path, *, missing: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return dict(missing or {}), ""
    try:
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FactoryConfigError(f"cannot read YAML {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise FactoryConfigError(f"{path} must contain a mapping")
    return document, _header(text)


def _write_mapping(path: Path, document: dict[str, Any], header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(document, allow_unicode=True, default_flow_style=False, sort_keys=False)
    path.write_text(header + rendered, encoding="utf-8")


def _validate_fields(name: str, profile: str, executor: str, model: str, max_concurrent: int) -> None:
    if not isinstance(name, str) or not name.strip():
        raise FactoryConfigError("seat name is required")
    if not isinstance(profile, str) or profile not in list_profiles():
        raise FactoryConfigError(f"profile {profile!r} does not exist")
    if executor not in EXECUTORS:
        raise FactoryConfigError(f"unknown executor {executor!r}; expected hermes, codex, or claude")
    if not isinstance(model, str) or not model.strip():
        raise FactoryConfigError("model is required")
    if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or max_concurrent < 1:
        raise FactoryConfigError("max_concurrent must be a positive integer")


def _provider_document(provider_config: dict[str, Any], model: str) -> dict[str, Any]:
    if not isinstance(provider_config, dict):
        raise FactoryConfigError("provider_config must be an object")
    # API/UI's compact provider lane.  Full config documents are also accepted.
    if {"provider", "base_url"} & set(provider_config):
        provider = str(provider_config.get("provider") or "").strip()
        base_url = str(provider_config.get("base_url") or "").strip()
        configured_model = str(provider_config.get("model") or model).strip()
        if not provider or not base_url or not configured_model:
            raise FactoryConfigError("provider_config requires provider, base_url, and model")
        return {
            "model": {"base_url": base_url, "default": configured_model, "provider": provider},
            "providers": {provider: {"base_url": base_url, "default_model": configured_model}},
        }
    result = dict(provider_config)
    block = result.get("model")
    if not isinstance(block, dict) or not str(block.get("default") or "").strip():
        raise FactoryConfigError("provider_config must include model.default")
    return result


def _ensure_profile_model(profile: str, model: str, provider_config: dict[str, Any] | None) -> str:
    path = _profile_path(profile)
    if provider_config is not None:
        supplied = _provider_document(provider_config, model)
        effective_model = str(supplied["model"]["default"])
        if effective_model != model:
            raise FactoryConfigError("provider_config.model must match the seat model")
        if not ({"provider", "base_url"} & set(provider_config)):
            # Programmatic callers may supply the complete profile document.
            _write_mapping(path, supplied)
            return effective_model
        document, header = _load_mapping(path) if path.exists() else ({}, "")
        # Provider-lane input updates only the provider/model fields; the rest
        # of a real profile config (skills, policy, memories, etc.) survives.
        document["model"] = {**dict(document.get("model") or {}), **supplied["model"]}
        providers = dict(document.get("providers") or {})
        for provider, settings in (supplied.get("providers") or {}).items():
            providers[provider] = {**dict(providers.get(provider) or {}), **settings}
        document["providers"] = providers
        _write_mapping(path, document, header)
        return effective_model

    if not path.exists():
        raise FactoryConfigError(
            "finding #12: refusing Hermes seat because its profile config.yaml is missing; "
            "supply provider_config so it cannot inherit the global default"
        )
    document, header = _load_mapping(path)
    model_block = document.get("model")
    if not isinstance(model_block, dict):
        raise FactoryConfigError("finding #12: profile config.yaml has no explicit model block; supply provider_config")
    # The seat's model and profile model must remain the same contract.
    model_block = dict(model_block)
    model_block["default"] = model
    document["model"] = model_block
    _write_mapping(path, document, header)
    return model


def _seat_record(name: str, values: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, **values}


def _write_seat(name: str, values: dict[str, Any], *, create: bool, provider_config: dict[str, Any] | None) -> dict[str, Any]:
    path = _seats_path()
    document, header = _load_mapping(path, missing={"company": "hermes-factory", "seats": {}})
    seats = document.get("seats")
    if not isinstance(seats, dict):
        raise FactoryConfigError("seats must be a mapping")
    exists = name in seats
    if create and exists:
        raise FactoryConfigError(f"seat {name!r} already exists")
    if not create and not exists:
        raise FactoryConfigError(f"seat {name!r} does not exist")
    current = dict(seats.get(name) or {})
    record = {**current, **{key: value for key, value in values.items() if value is not None}}
    _validate_fields(name, str(record.get("profile", "")), str(record.get("executor", "")), str(record.get("model", "")), record.get("max_concurrent"))
    if record["executor"] == "hermes":
        _ensure_profile_model(record["profile"], record["model"], provider_config)
    elif provider_config is not None:
        raise FactoryConfigError("provider_config is only valid for executor 'hermes'")
    seats[name] = record
    document["seats"] = seats
    _write_mapping(path, document, header)
    return _seat_record(name, record)


def create_seat(name: str, profile: str, executor: str, model: str, reasoning: str,
                role: str, max_concurrent: int, provider_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a seat and, for Hermes seats, its explicit profile model config."""
    return _write_seat(name, {
        "profile": profile, "executor": executor, "model": model,
        "reasoning": reasoning, "role": role, "max_concurrent": max_concurrent,
    }, create=True, provider_config=provider_config)


def update_seat(name: str, profile: str | None = None, executor: str | None = None,
                model: str | None = None, reasoning: str | None = None, role: str | None = None,
                max_concurrent: int | None = None, provider_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Update an existing employment contract through the same invariant checks."""
    return _write_seat(name, {
        "profile": profile, "executor": executor, "model": model, "reasoning": reasoning,
        "role": role, "max_concurrent": max_concurrent,
    }, create=False, provider_config=provider_config)


def profile_model(profile: str) -> str | None:
    """Return the explicit model resolved from a profile config, if configured."""
    path = _profile_path(profile)
    if not path.exists():
        return None
    document, _ = _load_mapping(path)
    block = document.get("model")
    return str(block.get("default")) if isinstance(block, dict) and block.get("default") else None


def profile_provider_config(profile: str) -> dict[str, str] | None:
    """Expose the safe provider-lane fields needed to clone a Hermes seat."""
    path = _profile_path(profile)
    if not path.exists():
        return None
    document, _ = _load_mapping(path)
    block = document.get("model")
    if not isinstance(block, dict) or not block.get("default"):
        return None
    provider = str(block.get("provider") or "")
    base_url = str(block.get("base_url") or "")
    providers = document.get("providers")
    if isinstance(providers, dict) and provider and isinstance(providers.get(provider), dict):
        nested = providers[provider]
        base_url = base_url or str(nested.get("base_url") or nested.get("api") or "")
    return {"provider": provider, "base_url": base_url, "model": str(block["default"])}


def seat_details() -> list[dict[str, Any]]:
    """Read seats with the two source-of-truth models exposed for operators."""
    from factory.config import load_seats
    rows: list[dict[str, Any]] = []
    for seat in load_seats().seats.values():
        configured = profile_model(seat.profile)
        rows.append(vars(seat) | {
            "profile_model": configured,
            "provider_config": profile_provider_config(seat.profile),
            "model_mismatch": bool(configured and seat.model and configured != seat.model),
        })
    return rows


__all__ = ["create_seat", "list_profiles", "profile_model", "profile_provider_config", "seat_details", "update_seat"]
