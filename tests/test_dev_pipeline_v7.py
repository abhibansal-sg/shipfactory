"""Immutable dev-pipeline@7 budget-closure regressions."""
from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from pathlib import Path

import pytest

from shipfactory.recipes.loader import load_library
from shipfactory.config import FactoryConfig
from shipfactory.recipes.advancer import startup_guard


ROOT = Path(__file__).resolve().parents[1]
TOKEN_ALLOWANCE = 50_000
V7_SHA256 = "00228377dad2f0d24816d49c21ed40174252910c6acb5c7e415f6335e5969c0f"


def _recipe(version: int) -> dict:
    return load_library(ROOT / "recipes", persist=False).get(
        f"dev-pipeline@{version}"
    ).document


def test_dev_pipeline_7_is_only_the_budget_closure_successor():
    v6 = copy.deepcopy(_recipe(6))
    v7 = copy.deepcopy(_recipe(7))

    assert v7["version"] == 7
    assert v7["supersedes"] == "dev-pipeline@6"
    assert v7["status"] == "active"
    assert "dev-pipeline@7" in v7["steps"][-1]["params"]["message"]

    v6["version"] = v7["version"]
    v6["supersedes"] = v7["supersedes"]
    v6["budgets"] = v7["budgets"]
    v6["steps"][-1]["params"]["message"] = v7["steps"][-1]["params"]["message"]
    assert v7 == v6


def test_dev_pipeline_7_pools_cover_every_declared_activation_cap():
    recipe = _recipe(7)
    budgets = recipe["budgets"]
    caps = budgets["step_activation_caps"]
    required: dict[str, int] = defaultdict(int)

    for step in recipe["steps"]:
        if step["primitive"] not in {"agent_task", "review_gate"}:
            continue
        assert step["id"] in caps
        pool = step["params"]["execution_profile"]
        required[pool] += caps[step["id"]] * TOKEN_ALLOWANCE

    assert dict(required) == {
        "planning": 250_000,
        "review": 600_000,
        "build": 150_000,
    }
    assert budgets["token_pools"] == dict(required)
    assert budgets["max_tokens"] == sum(required.values()) == 1_000_000
    assert budgets["max_activations"] >= sum(caps.values())


def test_startup_guard_loads_the_published_library_under_the_count_only_regime(monkeypatch):
    """Token-budget closure was removed (finding #77); startup_guard now
    validates the whole published library on load, including the count
    invariant that max_activations covers the sum of step_activation_caps."""
    from hermes_cli import config as hermes_config  # type: ignore[import-not-found]

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"kanban": {"auto_decompose": False}},
    )
    profiles = {
        name: {"max_runtime_seconds": 1800, "max_retries": 2}
        for name in ("standard", "planning", "build", "review")
    }
    config = FactoryConfig(
        "test",
        {name: {} for name in (  # type: ignore[arg-type]
            "explorer", "author", "dev-backend", "verifier", "architect",
            "operator",
        )},
        {},
        {
            "enabled": True,
            "library_path": str(ROOT / "recipes"),
            "execution_profiles": profiles,
            "verification_profiles": {"browser-standard": {}},
        },
    )
    startup_guard(config)


def test_dev_pipeline_7_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@7.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V7_SHA256
