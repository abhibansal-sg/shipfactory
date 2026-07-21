"""Decoupling: a ShipFactory seat spawns even when its name is not a Hermes profile.

The Hermes dispatcher buckets a ready task as ``nonspawnable`` when its
assignee is not a profile directory. ``daemon.tick``'s rescue pass claims and
spawns such tasks for real ShipFactory seats, so step-granular seats
(``spec-author``, ``*-reviewer``) run without a matching ``~/.hermes/profiles``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from shipfactory import daemon
from shipfactory.config import FactoryConfig, Seat


def _cfg(seats: dict[str, Seat]) -> FactoryConfig:
    # recipes disabled keeps tick's advancer/selector path out of the way; the
    # rescue pass runs regardless (it only needs cfg.seats + the dispatch result).
    return FactoryConfig("shipfactory", seats, {}, {"enabled": False})


def _seat(name: str, executor: str = "codex", profile=None, max_concurrent: int = 1) -> Seat:
    return Seat(name=name, executor=executor, profile=profile, model="m",
                role="general", max_concurrent=max_concurrent)


@pytest.fixture
def board(kanban_conn):
    kanban_db.create_board("decoupling")
    return kanban_db.connect(board="decoupling")


def _install(monkeypatch, recorder, seats):
    """Fake shipfactory_spawn (records calls) + tick's cfg (the test seats)."""
    spawn = types.ModuleType("shipfactory.spawn")
    spawn.shipfactory_spawn = lambda task, workspace, *, board=None: recorder.append(
        (task.id, task.assignee, workspace)
    ) or 123
    spawn.reap_finished = lambda: []
    spawn.restore_running = None
    spawn.WorkerCapacityExhausted = type("WorkerCapacityExhausted", (Exception,), {})
    monkeypatch.setitem(sys.modules, "shipfactory.spawn", spawn)
    import shipfactory
    monkeypatch.setattr(shipfactory, "spawn", spawn, raising=False)
    monkeypatch.setattr(daemon, "validate_recipe_mode", lambda **_: _cfg(seats))


def test_non_profile_codex_seat_is_rescued_and_spawned(board, monkeypatch):
    """REQ-1: a codex seat with no matching profile gets claimed + spawned."""
    spawned: list = []
    _install(monkeypatch, spawned, {"spec-author": _seat("spec-author")})
    task_id = kanban_db.create_task(board, title="spec", assignee="spec-author")

    daemon.tick(board, board="decoupling")

    assert len(spawned) == 1
    assert spawned[0][1] == "spec-author"
    assert kanban_db.get_task(board, task_id).status == "running"


def test_unknown_assignee_and_hermes_seat_are_not_rescued(board, monkeypatch):
    """REQ-3: a non-seat assignee and a hermes-executor seat stay gated."""
    spawned: list = []
    _install(monkeypatch, spawned,
             {"op-seat": _seat("op-seat", executor="hermes", profile="default")})
    kanban_db.create_task(board, title="ghost", assignee="ghost")          # not a seat
    kanban_db.create_task(board, title="op", assignee="op-seat")           # hermes carve-out

    daemon.tick(board, board="decoupling")

    assert spawned == []  # neither rescued


def test_rescue_respects_max_concurrent(board, monkeypatch):
    """REQ-4: two nonspawnable tasks for a max_concurrent=1 seat spawn only once."""
    spawned: list = []
    _install(monkeypatch, spawned, {"spec-author": _seat("spec-author", max_concurrent=1)})
    kanban_db.create_task(board, title="a", assignee="spec-author")
    kanban_db.create_task(board, title="b", assignee="spec-author")

    daemon.tick(board, board="decoupling")

    assert len(spawned) == 1  # second left unclaimed
