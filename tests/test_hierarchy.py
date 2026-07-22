from types import SimpleNamespace

import shipfactory.hierarchy as hierarchy


def test_gate_predicates_remain():
    cfg = SimpleNamespace(hierarchy_gates={"landers": ["lead"], "verdicts": ["lead"]})
    assert hierarchy.may_land(cfg, "lead") and hierarchy.may_verdict(cfg, "lead")
    assert not hierarchy.may_land(cfg, "dev")


def test_reporting_chain_helpers_are_absent():
    assert hierarchy.__all__ == ["may_land", "may_verdict"]
    for name in ("chain", "escalation_target", "validate_acyclic"):
        assert not hasattr(hierarchy, name)
