"""Conformance checks for the current Hermes plugin layout."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_root_manifest_is_the_single_current_plugin_identity() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "shipfactory"
    assert manifest["kind"] == "standalone"
    assert manifest["provides_tools"] == [
        "shipfactory_verdict",
        "shipfactory_costs",
        "shipfactory_monitor_add",
    ]
    assert manifest["provides_hooks"] == [
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
    ]
    assert not (ROOT / "shipfactory" / "plugin.yaml").exists()
    assert (ROOT / "__init__.py").is_file()


def test_dashboard_manifest_uses_safe_standard_relative_entries() -> None:
    dashboard = ROOT / "dashboard"
    manifest = yaml.safe_load((dashboard / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "shipfactory"
    assert manifest["tab"]["path"] == "/shipfactory"
    for field in ("entry", "css", "api"):
        value = manifest[field]
        assert not Path(value).is_absolute()
        assert ".." not in Path(value).parts
        assert (dashboard / value).is_file()


def test_embedded_designer_forwards_the_dashboard_session_token() -> None:
    designer = ROOT / "dashboard" / "designer"
    bundles = list((designer / "assets").glob("*.js"))

    assert len(bundles) == 1
    bundle = bundles[0].read_text(encoding="utf-8")
    assert "X-Hermes-Session-Token" in bundle
    assert "__HERMES_SESSION_TOKEN__" in bundle
