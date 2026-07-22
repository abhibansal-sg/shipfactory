"""Factory hierarchy-gate permission helpers."""

from __future__ import annotations


def may_land(cfg, seat) -> bool:
    """Return whether *seat* belongs to the configured lander gate."""
    return seat in cfg.hierarchy_gates.get("landers", [])


def may_verdict(cfg, seat) -> bool:
    """Return whether *seat* belongs to the configured verdict gate."""
    return seat in cfg.hierarchy_gates.get("verdicts", [])


__all__ = ["may_land", "may_verdict"]
