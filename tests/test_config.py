import os
import sys
import types
from pathlib import Path

import pytest

from shipfactory.config import FactoryConfigError, load_seats


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
    import os
    path = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""company: demo
seats:
  lead:
    profile: lead
    executor: claude
    role: ceo
  dev:
    profile: dev
    executor: codex
    reports_to: missing
    role: engineer
    max_concurrent: 2
hierarchy_gates:
  landers: [lead]
  verdicts: [lead]
""")
    with pytest.warns(UserWarning, match="reports_to"):
        cfg = load_seats()
    assert cfg.company == "demo" and not hasattr(cfg.seats["dev"], "reports_to")
    assert cfg.seats["lead"].max_concurrent == 1


def test_unknown_hierarchy_gate_seat_rejected(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    path = tmp_path / "seats.yaml"
    path.write_text("""company: demo
seats:
  lead:
    profile: lead
    executor: hermes
    role: ceo
  dev:
    profile: dev
    executor: codex
    role: engineer
hierarchy_gates:
  landers: [unknown]
""")
    with pytest.raises(FactoryConfigError, match="unknown seat"):
        load_seats(path)


def test_unknown_executor_rejected(tmp_path, monkeypatch):
    _profiles(monkeypatch)
    path = tmp_path / "seats.yaml"
    path.write_text("company: demo\nseats:\n  dev:\n    profile: dev\n    executor: mystery\n    role: engineer\n")
    with pytest.raises(FactoryConfigError, match="unknown executor"):
        load_seats(path)


def test_config_blob_validated_per_executor(tmp_path, monkeypatch):
    """REQ-5: config keys are validated against the chosen executor's allowlist."""
    from shipfactory.executors import validate_seat_config
    validate_seat_config("codex", {"fast_mode": True})       # codex owns fast_mode
    with pytest.raises(FactoryConfigError, match="fast_mode"):
        validate_seat_config("grok", {"fast_mode": True})    # grok does not
    with pytest.raises(FactoryConfigError, match="nonsense"):
        validate_seat_config("codex", {"nonsense": 1})       # unknown key rejected
    validate_seat_config("claude", {"command": "claude"})    # COMMON_KEYS accepted everywhere


def test_non_hermes_seat_needs_no_profile(tmp_path, monkeypatch):
    """REQ-1 (config layer): a codex seat with an unknown profile still loads."""
    _profiles(monkeypatch)  # profile_exists only knows lead/dev
    path = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""company: demo
seats:
  spec-author:
    executor: codex
    model: gpt
    role: author
""")
    cfg = load_seats()
    assert cfg.seats["spec-author"].profile is None
    assert cfg.seats["spec-author"].config == {} and cfg.seats["spec-author"].skills == ()


def test_hermes_seat_still_requires_a_real_profile(tmp_path, monkeypatch):
    """The decoupling does not weaken the hermes carve-out."""
    _profiles(monkeypatch)
    import os
    path = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""company: demo
seats:
  op:
    executor: hermes
    profile: nonexistent
    model: m
    role: operator
""")
    with pytest.raises(FactoryConfigError, match="does not exist"):
        load_seats()


def test_unknown_seat_key_is_tolerated_not_fatal(tmp_path, monkeypatch):
    """REQ-10: a forward-compat unknown key warns and is dropped, never raises."""
    _profiles(monkeypatch)
    import os
    path = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""company: demo
seats:
  dev:
    executor: codex
    model: m
    role: engineer
    future_key: whatever
""")
    with pytest.warns(UserWarning, match="future_key"):
        cfg = load_seats()
    assert not hasattr(cfg.seats["dev"], "future_key")


def test_live_seats_migrate_no_op(tmp_path, monkeypatch):
    """REQ-7: the real 14-seat seats.yaml loads with fields intact, config={}, no rewrite."""
    import shutil
    from pathlib import Path
    live = Path.home() / ".hermes" / "shipfactory" / "seats.yaml"
    if not live.exists():
        pytest.skip("no live seats.yaml on this host")
    _profiles(monkeypatch)
    # Any profile name resolves so hermes seats validate in the copy.
    import sys
    sys.modules["hermes_cli.profiles"].profile_exists = lambda name: True
    dest = Path(os.environ["HERMES_HOME"]) / "shipfactory" / "seats.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(live, dest)
    before = dest.read_bytes()
    cfg = load_seats()
    assert len(cfg.seats) >= 6
    for seat in cfg.seats.values():
        assert seat.config == {} and seat.skills == ()
        assert seat.executor in {"hermes", "codex", "claude", "grok", "opencode"}
    assert dest.read_bytes() == before  # loader never rewrites the store
