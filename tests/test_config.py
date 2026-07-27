import os
import sys
import types
from pathlib import Path

import pytest

from shipfactory.config import (
    FactoryConfigError,
    PROJECTS_VISUAL_RECIPES_DEFAULTS,
    load_seats,
    projects_visual_recipes_config,
)


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


def _write_minimal_seats(path: Path, *, block: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "company: demo\n"
        "seats: {}\n"
        + (f"recipes:\n  projects_visual_recipes:\n{block}" if block else ""),
        encoding="utf-8",
    )


def test_projects_visual_recipes_absent_is_default_on_and_has_no_safety_switches(tmp_path):
    path = tmp_path / "seats.yaml"
    _write_minimal_seats(path)

    cfg = load_seats(path)
    effective = projects_visual_recipes_config(cfg.recipes)

    assert effective == PROJECTS_VISUAL_RECIPES_DEFAULTS
    assert all(effective[name] is True for name in (
        "enabled", "policy_editing_enabled", "launch_enabled", "graph_enabled",
        "live_overlay_enabled", "history_enabled",
    ))
    assert not {"unclassified_launch_enabled", "auto_approve", "approval_enabled"} & set(effective)


def test_projects_visual_recipes_same_file_is_hot_reloaded(tmp_path):
    path = tmp_path / "seats.yaml"
    _write_minimal_seats(path)
    first = load_seats(path)
    assert projects_visual_recipes_config(first.recipes)["recent_flight_limit"] == 20

    _write_minimal_seats(path, block="    recent_flight_limit: 7\n")
    second = load_seats(path)

    assert second is not first
    assert projects_visual_recipes_config(second.recipes)["recent_flight_limit"] == 7


def test_projects_visual_recipes_every_setting_can_be_overridden(tmp_path):
    path = tmp_path / "seats.yaml"
    overrides = {
        "enabled": False,
        "policy_editing_enabled": False,
        "launch_enabled": False,
        "graph_enabled": False,
        "live_overlay_enabled": False,
        "history_enabled": False,
        "recent_flight_limit": 9,
        "ui_refresh_interval_seconds": 11,
        "graph_direction": "LR",
        "graph_rank_gap": 70,
        "graph_lane_gap": 31,
        "graph_node_width": 210,
        "graph_node_height": 80,
        "graph_diamond_size": 30,
        "history_fold_threshold": 12,
    }
    block = "".join(
        f"    {key}: {str(value).lower() if isinstance(value, bool) else value}\n"
        for key, value in overrides.items()
    )
    _write_minimal_seats(path, block=block)

    effective = projects_visual_recipes_config(load_seats(path).recipes)

    assert effective == {**PROJECTS_VISUAL_RECIPES_DEFAULTS, **overrides}


@pytest.mark.parametrize(
    "block, message",
    [
        ("    unknown_setting: true\n", "unknown keys"),
        ("    enabled: 'yes'\n", "enabled must be boolean"),
        ("    recent_flight_limit: 0\n", "positive integer"),
        ("    graph_rank_gap: false\n", "positive integer"),
        ("    graph_direction: diagonal\n", "direction"),
        ("    graph_direction: []\n", "direction"),
    ],
)
def test_projects_visual_recipes_invalid_values_fail_closed(tmp_path, block, message):
    path = tmp_path / "seats.yaml"
    _write_minimal_seats(path, block=block)

    with pytest.raises(FactoryConfigError, match=message):
        load_seats(path)
