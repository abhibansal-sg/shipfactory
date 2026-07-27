from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = (ROOT / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")


def test_live_overlay_uses_atomic_wrapper_polling_and_cleans_up():
    assert "setLivePayload(payload)" in BUNDLE
    assert "livePayload && livePayload.graph" in BUNDLE
    assert "overlay: livePayload" in BUNDLE
    assert "runtime.ui_refresh_interval_seconds" in BUNDLE
    assert "runtime.enabled" in BUNDLE
    assert "runtime.graph_enabled" in BUNDLE
    assert "runtime.live_overlay_enabled" in BUNDLE
    assert "clearInterval(timer)" in BUNDLE
    assert "setLivePayload(null)" in BUNDLE
    assert 'request("/instances/" + encodeURIComponent(launchResult.instance_id) + "/graph")' in BUNDLE


def test_inspector_contract_is_read_only_and_safe_text():
    for token in (
        "function GraphOverlaySummary", "function GraphInspector", "historyPayload",
        "fold_threshold", "exactHistory", "Human operator approval required",
        'href: API + receipt.endpoint', '"/log"', '"/prompt"',
        'if (event.key === "Escape")', "Open exact receipts", "Evidence:",
    ):
        assert token in BUNDLE
    assert "dangerouslySetInnerHTML" not in BUNDLE
    assert ".innerHTML" not in BUNDLE
    assert "method: \"POST\"" not in BUNDLE[BUNDLE.index("function GraphRenderer"):BUNDLE.index("function useReportViewMeta")]


def test_frozen_graph_and_overlay_error_are_independent_and_poll_uses_exact_runtime_interval():
    assert 'var _l = useState(""), overlayError' in BUNDLE
    assert "setOverlayError(errorText(err))" in BUNDLE
    assert "setOverlayError(\"\")" in BUNDLE
    assert "Live overlay refresh failed; showing the last frozen graph." in BUNDLE
    assert "overlayError: overlayError" in BUNDLE
    assert "timer = setInterval(loadOverlay, runtime.ui_refresh_interval_seconds * 1000)" in BUNDLE
    assert "Math.max(1" not in BUNDLE
    assert "ui_refresh_interval_seconds || 1" not in BUNDLE
    assert 'href: "/runs/' not in BUNDLE
    assert 'href: "/instances/' not in BUNDLE


def test_poll_cleanup_and_runtime_visibility_guards_are_explicit():
    for token in (
        "if (!instanceId || !selected || !runtime.enabled || !runtime.graph_enabled || !runtime.live_overlay_enabled)",
        "if (!selected || !runtime.enabled || !runtime.graph_enabled)",
        "if (timer !== null) clearInterval(timer);",
        "setLivePayload(null);",
        "setOverlayError(\"\");",
    ):
        assert token in BUNDLE
