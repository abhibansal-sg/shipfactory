"""Conformance checks for the Hermes Desktop Plugin SDK entrypoint."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "desktop-plugin" / "plugin.js"


def _source() -> str:
    return PLUGIN.read_text(encoding="utf-8")


def test_desktop_plugin_is_plain_esm_with_only_sdk_allowed_imports() -> None:
    source = _source()
    imports = re.findall(r"from\s+['\"]([^'\"]+)['\"]", source)

    assert imports
    assert set(imports) <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert "import(" not in source
    assert "fetch(" not in source
    assert "pluginRest(path, options)" in source


def test_desktop_plugin_registers_native_surfaces() -> None:
    source = _source()

    assert "id: ID" in source and "const ID = 'shipfactory'" in source
    assert "defaultEnabled: true" in source
    assert "area: ROUTES_AREA" in source
    assert "area: SIDEBAR_NAV_AREA" in source
    assert "area: STATUSBAR_AREAS.right" in source
    assert "area: PALETTE_AREA" in source
    assert "area: KEYBINDS_AREA" in source
    assert "data: { path: PAGE_PATH, label: 'ShipFactory'" in source
    assert "render: () => jsx(ShipFactoryPage, {})" in source


def test_desktop_plugin_uses_theme_tokens_without_hardcoded_colors() -> None:
    source = _source()

    assert "var(--ui-" not in source  # Tailwind v4 token syntax is used instead.
    assert "(--ui-text-" in source and "(--ui-stroke-" in source
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source)
    assert not re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", source)


def test_desktop_plugin_and_backend_share_one_identity() -> None:
    source = _source()
    manifest = (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")

    assert "'shipfactory'" in source
    assert '"name": "shipfactory"' in manifest
    assert (ROOT / "dashboard" / "plugin_api.py").is_file()


def test_desktop_plugin_normalizes_api_timestamps_for_sdk_formatter() -> None:
    source = _source()

    assert "const timestampMs = value =>" in source
    assert "Date.parse(String(value))" in source
    assert "Number.isFinite(parsed)" in source
    assert "relativeTime(parsed)" in source
    assert "relativeTime(value)" not in source


def test_desktop_plugin_opens_the_live_authenticated_xyflow_host() -> None:
    source = _source()

    assert "const BUILDER_URL = 'http://127.0.0.1:9130/shipfactory'" in source
