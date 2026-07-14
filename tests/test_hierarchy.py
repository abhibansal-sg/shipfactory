from types import SimpleNamespace

import pytest

from headframe.hierarchy import chain, escalation_target, may_land, may_verdict, validate_acyclic


def test_chain_and_gates():
    cfg = SimpleNamespace(seats={"dev": SimpleNamespace(reports_to="lead"), "lead": SimpleNamespace(reports_to=None)},
                          hierarchy_gates={"landers": ["lead"], "verdicts": ["lead"]})
    assert chain(cfg, "dev") == ["dev", "lead"]
    assert escalation_target(cfg, "dev") == "lead"
    assert may_land(cfg, "lead") and may_verdict(cfg, "lead")
    assert not may_land(cfg, "dev")


def test_validate_cycle_fails():
    cfg = SimpleNamespace(seats={"a": SimpleNamespace(reports_to="b"), "b": SimpleNamespace(reports_to="a")})
    with pytest.raises(ValueError, match="cycle"):
        validate_acyclic(cfg)
