"""Seat-contract writer tests, including finding #12's profile invariant."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shipfactory.config import FactoryConfigError
from shipfactory.seats_admin import create_seat, list_profiles, seat_details, update_seat


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
    seats = hermetic_hermes_home / "shipfactory" / "seats.yaml"
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
    (hermetic_hermes_home / "shipfactory").mkdir()
    (hermetic_hermes_home / "shipfactory" / "seats.yaml").write_text("company: demo\nseats:\n  builder:\n    profile: builder\n    executor: hermes\n    model: new-model\n    role: engineer\n    max_concurrent: 1\n")
    assert seat_details()[0]["model_mismatch"] is True
    updated = update_seat("builder", model="new-model")
    assert updated["model"] == "new-model"
    assert seat_details()[0]["model_mismatch"] is False


def test_home_isolation_and_validation(hermetic_hermes_home: Path):
    _profile(hermetic_hermes_home, "builder")
    assert list_profiles() == ["default", "builder"]
    # A profile that does not exist is rejected only for a hermes seat, whose
    # `hermes -p <profile>` argv genuinely needs it. A non-hermes seat's name
    # is a dispatch label decoupled from the profiles directory.
    with pytest.raises(FactoryConfigError, match="does not exist"):
        create_seat("bad", "outside", "hermes", "gpt", "low", "engineer", 1)
    with pytest.raises(FactoryConfigError, match="positive"):
        create_seat("bad", "builder", "codex", "gpt", "low", "engineer", 0)
    # A codex seat with no matching profile is now valid (the decoupling).
    create_seat("decoupled", "spec-author", "codex", "gpt", "low", "author", 1)
    create_seat("isolated", "builder", "codex", "gpt", "low", "custom-role", 1)
    assert (hermetic_hermes_home / "shipfactory" / "seats.yaml").exists()
