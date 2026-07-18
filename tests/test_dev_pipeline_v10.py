"""Immutable dev-pipeline@10 calibrated-gate and budget closure regressions."""
from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from pathlib import Path

import pytest

from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
RATIFIED_PROFILE_ALLOWANCES = {
    "planning": 50_000,
    "build": 75_000,
    "review": 50_000,
}
V10_SHA256 = "80a2394ff84522e17fef84fe67f858d0bf7549424e3db8789e74c0b8116c8afe"
CALIBRATED = {"spec-attack", "plan-attack", "correctness-review", "adversarial-review"}


def _recipe(version: int) -> dict:
    return load_library(ROOT / "recipes", persist=False).get(
        f"dev-pipeline@{version}"
    ).document


def test_dev_pipeline_10_is_only_the_calibration_and_headroom_successor():
    """finding #73: four live flights died at the spec gate because the attack
    instructions stated necessary approval conditions but no sufficiency — a
    maximally diligent reviewer finds the next-deeper gap forever. @10 adds
    explicit blocker criteria + a MUST-approve clause to all four gates and one
    extra convergence round for both draft steps."""
    v9 = copy.deepcopy(_recipe(9))
    v10 = copy.deepcopy(_recipe(10))

    assert v10["version"] == 10
    assert v10["supersedes"] == "dev-pipeline@9"
    assert v10["verdict_contract"] == "shipfactory.verdict/v2"
    assert "dev-pipeline@10" in v10["steps"][-1]["params"]["message"]
    for step in v10["steps"]:
        if step["id"] in CALIBRATED:
            text = " ".join(step["params"]["instructions"].split())
            assert "blocker exists ONLY" in text
            assert "MUST approve" in text
            assert "structured SHIPFACTORY_VERDICT v2 JSON sentinel" in text

    v9["version"] = v10["version"]
    v9["supersedes"] = v10["supersedes"]
    v9["steps"][-1]["params"]["message"] = v10["steps"][-1]["params"]["message"]
    v9["budgets"]["max_activations"] = v10["budgets"]["max_activations"]
    v9["budgets"]["max_tokens"] = v10["budgets"]["max_tokens"]
    v9["budgets"]["step_activation_caps"]["spec-draft"] = 3
    v9["budgets"]["step_activation_caps"]["plan-draft"] = 3
    v9["budgets"]["token_pools"]["planning"] = 350_000
    for old, new in zip(v9["steps"], v10["steps"]):
        if old["id"] in CALIBRATED:
            old["params"]["instructions"] = new["params"]["instructions"]
    assert v10 == v9


def test_dev_pipeline_10_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@10.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V10_SHA256
