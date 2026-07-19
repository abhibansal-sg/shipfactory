"""Immutable dev-pipeline@12 split-author regressions (finding #83)."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
V12_SHA256 = "b4aa06630a503f2509f4f4152e78949a4726bfec67ecfc7b70370ef2ff5de3a8"


def _recipe(version: int) -> dict:
    return load_library(ROOT / "recipes", persist=False).get(
        f"dev-pipeline@{version}"
    ).document


def test_dev_pipeline_12_only_splits_author_from_builder():
    v11 = copy.deepcopy(_recipe(11))
    v12 = copy.deepcopy(_recipe(12))

    v11["version"] = 12
    v11["supersedes"] = "dev-pipeline@11"
    for step in v11["steps"]:
        if step["id"] in {"spec-draft", "plan-draft", "review-story"}:
            step["params"]["seat"] = "author"
    v11["steps"][-1]["params"]["message"] = v12["steps"][-1]["params"]["message"]

    assert v12 == v11


def test_dev_pipeline_12_role_graph_matches_the_ratified_cast():
    seats = {
        step["id"]: step.get("params", {}).get("seat")
        for step in _recipe(12)["steps"]
        if step.get("params", {}).get("seat")
    }

    assert seats == {
        "explore": "explorer",
        "spec-draft": "author",
        "spec-attack": "verifier",
        "plan-draft": "author",
        "plan-attack": "verifier",
        "build": "dev-backend",
        "correctness-review": "verifier",
        "adversarial-review": "architect",
        "review-story": "author",
    }


def test_dev_pipeline_12_count_caps_still_close():
    budgets = _recipe(12)["budgets"]
    assert sum(budgets["step_activation_caps"].values()) <= budgets["max_activations"]


def test_dev_pipeline_12_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@12.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V12_SHA256
