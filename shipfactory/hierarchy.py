"""Factory reporting-chain and permission helpers."""

from __future__ import annotations


def chain(cfg, seat) -> list[str]:
    """Return *seat* followed by each manager through the root."""
    if seat not in cfg.seats:
        raise KeyError(f"unknown seat: {seat}")
    result, seen = [], set()
    current = seat
    while current is not None:
        if current in seen:
            raise ValueError(f"reports_to cycle involving {current!r}")
        seen.add(current)
        result.append(current)
        current = cfg.seats[current].reports_to
    return result


def escalation_target(cfg, seat) -> str | None:
    """Return a seat's direct manager, or ``None`` at the board root."""
    if seat not in cfg.seats:
        raise KeyError(f"unknown seat: {seat}")
    return cfg.seats[seat].reports_to


def may_land(cfg, seat) -> bool:
    """Return whether *seat* belongs to the configured lander gate."""
    return seat in cfg.hierarchy_gates.get("landers", [])


def may_verdict(cfg, seat) -> bool:
    """Return whether *seat* belongs to the configured verdict gate."""
    return seat in cfg.hierarchy_gates.get("verdicts", [])


def validate_acyclic(cfg) -> None:
    """Raise when any reports-to edge is unknown or cyclic."""
    for name, seat in cfg.seats.items():
        if seat.reports_to and seat.reports_to not in cfg.seats:
            raise ValueError(f"seat {name!r} reports to unknown seat {seat.reports_to!r}")
        chain(cfg, name)


__all__ = ["chain", "escalation_target", "may_land", "may_verdict", "validate_acyclic"]
