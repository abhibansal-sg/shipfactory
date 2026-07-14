"""Seat-contract writer tests, including finding #12's profile invariant."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from factory.config import FactoryConfigError
from factory.seats_admin import create_seat, list_profiles, seat_details, update_seat


def _profile(home: Path, name: str, model: str = "claude-sonnet-5") -> Path:
    config = home / "profiles" / name / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"model": {"provider": "proxy", "base_url": "http://proxy", "default": model}}))
    return config


def test_create_hermes_requires_explicit_profile_config(hermetic_hermes_home: Path):
    (hermetic_hermes_home / "profiles" / "builder").mkdir(parents=True)
    with pytest.raises(FactoryConfigError, match="finding #12"):
        create_seat("builder", "builder", "hermes", "claude-sonnet-5", "medium", "engineer", 1)


def test_hermes_create_writes_both_contract_files_and_keeps_header(hermetic_hermes_home: Path):
    profile = hermetic_hermes_home / "profiles" / "builder"
    profile.mkdir(parents=True)
    seats = hermetic_hermes_home / "factory" / "seats.yaml"
    seats.parent.mkdir()
    seats.write_text("# retained factory header\ncompany: demo\nseats: {}\n")
    created = create_seat("builder", "builder", "hermes", "claude-sonnet-5", "high", "engineer", 2, {
        "provider": "proxy", "base_url": "http://proxy", "model": "claude-sonnet-5",
    })
    assert created["name"] == "builder"
    assert seats.read_text().startswith("# retained factory header")
    assert yaml.safe_load((profile / "config.yaml").read_text())["model"]["default"] == "claude-sonnet-5"
    assert yaml.safe_load(seats.read_text())["seats"]["builder"]["max_concurrent"] == 2


def test_mismatch_is_visible_and_update_resynchronizes_hermes_model(hermetic_hermes_home: Path):
    _profile(hermetic_hermes_home, "builder", "old-model")
    (hermetic_hermes_home / "factory").mkdir()
    (hermetic_hermes_home / "factory" / "seats.yaml").write_text("company: demo\nseats:\n  builder:\n    profile: builder\n    executor: hermes\n    model: new-model\n    role: engineer\n    max_concurrent: 1\n")
    assert seat_details()[0]["model_mismatch"] is True
    updated = update_seat("builder", model="new-model")
    assert updated["model"] == "new-model"
    assert seat_details()[0]["model_mismatch"] is False


def test_home_isolation_and_validation(hermetic_hermes_home: Path):
    _profile(hermetic_hermes_home, "builder")
    assert list_profiles() == ["default", "builder"]
    with pytest.raises(FactoryConfigError, match="does not exist"):
        create_seat("bad", "outside", "codex", "gpt", "low", "engineer", 1)
    with pytest.raises(FactoryConfigError, match="positive"):
        create_seat("bad", "builder", "codex", "gpt", "low", "engineer", 0)
    create_seat("isolated", "builder", "codex", "gpt", "low", "custom-role", 1)
    assert (hermetic_hermes_home / "factory" / "seats.yaml").exists()
