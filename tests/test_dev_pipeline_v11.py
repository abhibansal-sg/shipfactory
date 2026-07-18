"""Immutable dev-pipeline@11 count-only budget regressions (finding #77)."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from shipfactory.recipes.loader import load_library


ROOT = Path(__file__).resolve().parents[1]
V11_SHA256 = "da4f45a84f3a327ccedb9f3e882304153f553013b0f8b19efa8410bb72906e61"


def _recipe(version: int) -> dict:
    return load_library(ROOT / "recipes", persist=False).get(
        f"dev-pipeline@{version}"
    ).document


def test_dev_pipeline_11_drops_token_budgets_keeping_count_caps():
    """@11 is @10 with the token-budget fields removed: the journey of record
    is now count-only (max_activations + step_activation_caps), no max_tokens,
    no token_pools."""
    v10 = copy.deepcopy(_recipe(10))
    v11 = copy.deepcopy(_recipe(11))

    assert v11["version"] == 11
    assert v11["supersedes"] == "dev-pipeline@10"
    assert v11["verdict_contract"] == "shipfactory.verdict/v2"
    assert set(v11["budgets"]) == {"max_activations", "step_activation_caps"}
    assert "max_tokens" not in v11["budgets"]
    assert "token_pools" not in v11["budgets"]
    assert "dev-pipeline@11" in v11["steps"][-1]["params"]["message"]

    # Everything except the token fields, version, supersedes, and the notify
    # message string is identical to @10.
    v10["version"] = v11["version"]
    v10["supersedes"] = v11["supersedes"]
    v10["steps"][-1]["params"]["message"] = v11["steps"][-1]["params"]["message"]
    del v10["budgets"]["max_tokens"]
    del v10["budgets"]["token_pools"]
    assert v11 == v10


def test_dev_pipeline_11_count_caps_close_within_max_activations():
    budgets = _recipe(11)["budgets"]
    assert sum(budgets["step_activation_caps"].values()) <= budgets["max_activations"]


def test_dev_pipeline_11_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@11.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V11_SHA256
