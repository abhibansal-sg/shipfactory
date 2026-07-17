"""Immutable dev-pipeline@9 verdict-contract and budget closure regressions."""
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
V9_SHA256 = "5bb3f0f0b3146a81813f6b0f6d14ec9c4207035548c5d2d2416b15200ee765d8"


def _recipe(version: int) -> dict:
    return load_library(ROOT / "recipes", persist=False).get(
        f"dev-pipeline@{version}"
    ).document


def test_dev_pipeline_9_is_only_the_verdict_contract_successor():
    v8 = copy.deepcopy(_recipe(8))
    v9 = copy.deepcopy(_recipe(9))

    assert v9["version"] == 9
    assert v9["supersedes"] == "dev-pipeline@8"
    assert v9["verdict_contract"] == "shipfactory.verdict/v2"
    assert "dev-pipeline@9" in v9["steps"][-1]["params"]["message"]
    reworded = {"spec-attack", "plan-attack", "correctness-review", "adversarial-review"}
    for step in v9["steps"]:
        if step["id"] in reworded:
            assert "structured SHIPFACTORY_VERDICT v2 JSON sentinel" in step["params"]["instructions"]

    v8["version"] = v9["version"]
    v8["supersedes"] = v9["supersedes"]
    v8["verdict_contract"] = v9["verdict_contract"]
    v8["steps"][-1]["params"]["message"] = v9["steps"][-1]["params"]["message"]
    for old, new in zip(v8["steps"], v9["steps"]):
        if old["id"] in reworded:
            old["params"]["instructions"] = new["params"]["instructions"]
    assert v9 == v8


def test_dev_pipeline_9_reworded_instructions_only_swap_the_sentinel_sentence():
    v8 = _recipe(8)
    v9 = _recipe(9)
    for old, new in zip(v8["steps"], v9["steps"]):
        old_text = old["params"].get("instructions", "")
        new_text = new["params"].get("instructions", "")
        assert new_text == old_text.replace(
            "the exact SHIPFACTORY_VERDICT JSON sentinel",
            "the exact structured SHIPFACTORY_VERDICT v2 JSON sentinel",
        ).replace(
            "the exact\nSHIPFACTORY_VERDICT JSON sentinel",
            "the exact\nstructured SHIPFACTORY_VERDICT v2 JSON sentinel",
        )


def test_dev_pipeline_9_closes_against_ratified_live_allowances():
    v9 = _recipe(9)
    profiles = {
        name: {"token_allowance": allowance}
        for name, allowance in RATIFIED_PROFILE_ALLOWANCES.items()
    }
    validate_budget_closure(v9, profiles)

    required: dict[str, int] = defaultdict(int)
    caps = v9["budgets"]["step_activation_caps"]
    for step in v9["steps"]:
        if step["primitive"] in {"agent_task", "review_gate"}:
            pool = step["params"]["execution_profile"]
            required[pool] += caps[step["id"]] * RATIFIED_PROFILE_ALLOWANCES[pool]
    assert dict(required) == {
        "planning": 250_000,
        "review": 600_000,
        "build": 225_000,
    }
    assert v9["budgets"] == _recipe(8)["budgets"]
    assert v9["budgets"]["token_pools"] == dict(required)
    assert v9["budgets"]["max_tokens"] == sum(required.values()) == 1_075_000


def test_dev_pipeline_9_fails_closed_on_future_live_allowance_drift():
    profiles = {
        name: {"token_allowance": allowance}
        for name, allowance in RATIFIED_PROFILE_ALLOWANCES.items()
    }
    profiles["build"]["token_allowance"] += 1
    with pytest.raises(ValueError, match="token pool 'build'.*activation caps"):
        validate_budget_closure(_recipe(9), profiles)


def test_dev_pipeline_9_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@9.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V9_SHA256
