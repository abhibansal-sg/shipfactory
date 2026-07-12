import sys
import types

import pytest

from factory.config import FactoryConfigError, load_seats


def _profiles(monkeypatch):
    module = types.ModuleType("hermes_cli.profiles")
    module.profile_exists = lambda name: name in {"lead", "dev"}
    package = types.ModuleType("hermes_cli")
    package.profiles = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", module)


def test_load_seats_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profiles(monkeypatch)
    path = tmp_path / "factory" / "seats.yaml"
    path.parent.mkdir()
    path.write_text("""company: demo
seats:
  lead:
    profile: lead
    executor: claude
    role: ceo
  dev:
    profile: dev
    executor: codex
    reports_to: lead
    role: engineer
    max_concurrent: 2
hierarchy_gates:
  landers: [lead]
  verdicts: [lead]
""")
    cfg = load_seats()
    assert cfg.company == "demo" and cfg.seats["dev"].reports_to == "lead"
    assert cfg.seats["lead"].max_concurrent == 1


def test_cycle_rejected(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    path = tmp_path / "seats.yaml"
    path.write_text("""company: demo
seats:
  lead:
    profile: lead
    executor: hermes
    role: ceo
    reports_to: dev
  dev:
    profile: dev
    executor: codex
    role: engineer
    reports_to: lead
""")
    with pytest.raises((FactoryConfigError, ValueError), match="cycle"):
        load_seats(path)


def test_unknown_executor_rejected(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    path = tmp_path / "seats.yaml"
    path.write_text("company: demo\nseats:\n  dev:\n    profile: dev\n    executor: mystery\n    role: engineer\n")
    with pytest.raises(FactoryConfigError, match="unknown executor"):
        load_seats(path)
