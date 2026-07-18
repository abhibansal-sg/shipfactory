"""Immutable dev-pipeline@8 live-profile budget closure regressions."""
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


def test_dev_pipeline_8_published_bytes_are_pinned():
    path = ROOT / "recipes" / "dev-pipeline@8.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V8_SHA256
