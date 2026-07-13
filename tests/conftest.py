"""Shared real-integration fixtures for the Factory test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


HERMES_MOBILE = Path.home() / "Developer/products/hermes-mobile"
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
