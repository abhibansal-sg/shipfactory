"""Static and conformance guards for the GraphRunner v1 dashboard surface."""

from pathlib import Path

from shipfactory import store as _store  # noqa: F401 -- establish package import order


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "dashboard" / "dist" / "index.js"
HARNESS = ROOT / "dashboard" / "conformance-harness.js"


def test_dashboard_bundle_uses_direct_v1_project_run_and_decision_endpoints():
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'request("/v1/recipes")' in source
    assert '"/v1/projects/" + encodeURIComponent(project.id) + "/recipes"' in source
    assert '"/v1/projects/" + encodeURIComponent(project.id) + "/runs"' in source
    assert '"/v1/runs/" + encodeURIComponent(runId) + "/graph"' in source
    assert '"/v1/human-boxes/" + encodeURIComponent(attempt.id) + "/decision"' in source


def test_dashboard_v1_decisions_are_declared_human_actions_with_fresh_nonces():
    source = BUNDLE.read_text(encoding="utf-8")
    assert "function GraphV1ProjectPanel" in source
    assert "function GraphV1Run" in source
    assert "function decideHuman(attempt, result)" in source
    assert 'graph.arrows.filter(function (arrow) { return arrow.from === attempt.box_id; })' in source
    assert "nonce: newNonce()" in source
    assert 'actor_kind: "human"' in source
    assert 'actor_id: "local-operator"' in source
    assert 'channel: "dashboard"' in source
    assert "dangerouslySetInnerHTML" not in source
    assert ".innerHTML" not in source


def test_conformance_harness_supplies_direct_graph_v1_fixtures():
    source = HARNESS.read_text(encoding="utf-8")
    assert "graphV1RecipeFixture" in source
    assert "graphV1RunFixture" in source
    assert 'path === "/api/plugins/shipfactory/v1/recipes"' in source
    assert 'path === "/api/plugins/shipfactory/v1/runs/fixture-v1-run/graph"' in source
    assert 'path === "/api/plugins/shipfactory/v1/human-boxes/fixture-human-attempt/decision"' in source
