"""Shared real-integration fixtures for the Factory test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


# ShipFactory imports the Hermes `hermes_cli` package from a checkout that must
# carry the recipe-kanban APIs (create_blocked_task / cancel_subtree). The
# default checkout may be switched to another branch for unrelated work, so the
# Hermes source is resolved from HERMES_MOBILE_PATH when set (e.g. a dedicated
# `feat-kanban-recipe-apis` git worktree), falling back to the conventional path.
HERMES_MOBILE = Path(
    os.environ.get("HERMES_MOBILE_PATH") or (Path.home() / "Developer/products/hermes-mobile")
)
if str(HERMES_MOBILE) not in sys.path:
    sys.path.insert(0, str(HERMES_MOBILE))


@pytest.fixture(autouse=True)
def hermetic_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give every test an isolated Hermes/Factory state root."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return home


@pytest.fixture
def kanban_conn(hermetic_hermes_home: Path):
    """Open the real feat-kanban-recipe-apis SQLite implementation."""
    from hermes_cli import kanban_db

    conn = kanban_db.connect(hermetic_hermes_home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()
