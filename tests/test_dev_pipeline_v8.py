"""Immutable dev-pipeline@8 live-profile budget closure regressions."""
from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from pathlib import Path

import pytest

from shipfactory.recipes.loader import load_library, validate_budget_closure


ROOT = Path(__file__).resolve().parents[1]
RATIFIED_PROFILE_ALLOWANCES = {
    "planning": 50_000,
    "build": 75_000,
    "review": 50_000,
}
V8_SHA256 = "8f88a1c349005222918ab1b04525db26ce498a442c231c9ed9dab52b2d0a8d8f"


def _recipe(version: int) -> dict:
    return load_library(ROOT / "recipes", persist=False).get(
        f"dev-pipeline@{version}"
    ).document


def test_dev_pipeline_8_is_only_the_live_allowance_successor():
    v7 = copy.deepcopy(_recipe(7))
    v8 = copy.deepcopy(_recipe(8))

    assert v8["version"] == 8
    assert v8["supersedes"] == "dev-pipeline@7"
    assert "dev-pipeline@8" in v8["steps"][-1]["params"]["message"]

    v7["version"] = v8["version"]
    v7["supersedes"] = v8["supersedes"]
    v7["budgets"] = v8["budgets"]
    v7["steps"][-1]["params"]["message"] = v8["steps"][-1]["params"]["message"]
    assert v8 == v7


def test_dev_pipeline_8_closes_against_ratified_live_allowances():
    v7 = _recipe(7)
    v8 = _recipe(8)
    profiles = {
        name: {"token_allowance": allowance}
        for name, allowance in RATIFIED_PROFILE_ALLOWANCES.items()
    }

    with pytest.raises(ValueError, match="dev-pipeline@7 token pool 'build'.*225000"):
        validate_budget_closure(v7, profiles)
    validate_budget_closure(v8, profiles)

    required: dict[str, int] = defaultdict(int)
    caps = v8["budgets"]["step_activation_caps"]
    for step in v8["steps"]:
        if step["primitive"] in {"agent_task", "review_gate"}:
            pool = step["params"]["execution_profile"]
            required[pool] += caps[step["id"]] * RATIFIED_PROFILE_ALLOWANCES[pool]
    assert dict(required) == {
        "planning": 250_000,
        "review": 600_000,
        "build": 225_000,
    }
    assert v8["budgets"]["token_pools"] == dict(required)
    assert v8["budgets"]["max_tokens"] == sum(required.values()) == 1_075_000


def test_dev_pipeline_8_fails_closed_on_future_live_allowance_drift():
    profiles = {
        name: {"token_allowance": allowance}
        for name, allowance in RATIFIED_PROFILE_ALLOWANCES.items()
    }
    profiles["build"]["token_allowance"] += 1
    with pytest.raises(ValueError, match="token pool 'build'.*activation caps"):
        validate_budget_closure(_recipe(8), profiles)


def test_dev_pipeline_8_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@8.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V8_SHA256
