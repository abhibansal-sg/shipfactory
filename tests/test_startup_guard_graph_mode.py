"""startup_guard Kanban decoupling under GraphRunner v1 (runner_mode=graph).

In graph mode the legacy recipe engine is drain-only and never instantiates
kanban tasks, so the two Kanban-coupled compatibility checks (kanban_db API
presence and Hermes kanban.auto_decompose) must be skipped.  Every other
runner mode keeps the exact legacy fail-closed behavior.
"""
from __future__ import annotations

import pytest

from shipfactory.config import FactoryConfig
from shipfactory.recipes.advancer import startup_guard


def _config(runner_mode: str | None) -> FactoryConfig:
    recipes: dict = {"enabled": True}
    if runner_mode is not None:
        recipes["runner_mode"] = runner_mode
    return FactoryConfig("test", {}, {}, recipes)


def _enable_auto_decompose(monkeypatch) -> None:
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"kanban": {"auto_decompose": True}},
    )


def _drop_kanban_apis(monkeypatch) -> None:
    from hermes_cli import kanban_db

    monkeypatch.delattr(kanban_db, "create_blocked_task", raising=False)
    monkeypatch.delattr(kanban_db, "cancel_subtree", raising=False)


# --- graph mode: Kanban-coupled checks are skipped --------------------------


def test_graph_mode_ignores_kanban_auto_decompose(monkeypatch):
    """(a) graph mode + Hermes kanban.auto_decompose=true must not raise."""
    _enable_auto_decompose(monkeypatch)
    startup_guard(_config("graph"))


def test_graph_mode_ignores_missing_kanban_apis(monkeypatch):
    """(b) graph mode + missing kanban_db APIs must not raise."""
    _drop_kanban_apis(monkeypatch)
    _enable_auto_decompose(monkeypatch)
    startup_guard(_config("graph"))


# --- every other mode: exact legacy fail-closed behavior --------------------


@pytest.mark.parametrize("runner_mode", [None, "mixed", "legacy"])
def test_non_graph_mode_still_refuses_auto_decompose(monkeypatch, runner_mode):
    """(c) mixed/legacy/missing mode + auto_decompose=true still raises."""
    _enable_auto_decompose(monkeypatch)
    with pytest.raises(RuntimeError, match="auto_decompose=true"):
        startup_guard(_config(runner_mode))


@pytest.mark.parametrize("runner_mode", [None, "mixed", "legacy"])
def test_non_graph_mode_still_requires_kanban_apis(monkeypatch, runner_mode):
    """(d) mixed/legacy/missing mode + missing kanban_db APIs still raises."""
    _drop_kanban_apis(monkeypatch)
    with pytest.raises(RuntimeError, match="create_blocked_task"):
        startup_guard(_config(runner_mode))
