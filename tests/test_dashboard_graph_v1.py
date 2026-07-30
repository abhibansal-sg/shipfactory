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
    assert 'request("/v1/runs?project_id=" + encodeURIComponent(project.id))' in source
    assert '"/v1/runs/" + encodeURIComponent(runId) + "/graph"' in source
    assert '"/v1/human-boxes/" + encodeURIComponent(attempt.id) + "/decision"' in source


def test_dashboard_rediscover_existing_v1_run_after_reload():
    source = BUNDLE.read_text(encoding="utf-8")
    assert "function loadExistingRun()" in source
    assert "var runs = payload && Array.isArray(payload.runs) ? payload.runs : [];" in source
    assert 'var current = runs.find(function (run) { return run.state === "running"; }) || runs[0];' in source
    assert 'setRunId(current ? current.id : "");' in source
    assert "Promise.all([loadRecipes(), loadExistingRun()]);" in source


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


def test_dashboard_v1_approval_card_precedes_graph_history_and_summarizes_decision():
    source = BUNDLE.read_text(encoding="utf-8")
    assert "function GraphV1ApprovalCard" in source
    assert "function graphV1ApprovalContext" in source
    assert '"Decision required"' in source
    assert '"Recommended outcome"' in source
    assert '"Proposed deliverable"' in source
    assert '"Review results"' in source
    assert '"Approve"' in source
    assert '"Reject"' in source
    assert source.index("h(GraphV1ApprovalCard") < source.index('"aria-label": "Declared GraphRunner recipe"')


def test_dashboard_v1_raw_attempt_payloads_are_collapsed_by_default():
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'if (typeof value === "string") return value;' in source
    assert 'if (typeof value === "string") return value.trim();' not in source
    assert 'h("details", { className: "factory-v1-attempt-details"' in source
    assert 'h("summary", null, "Attempt details")' in source
    assert 'h("pre", { className: "factory-v1-raw"' in source
    assert 'h("summary", null, "Full review")' in source
    assert 'h("p", { className: "mt-2 whitespace-pre-wrap text-xs text-text-secondary" }, "Input: "' not in source
    assert 'h("p", { className: "mt-1 whitespace-pre-wrap text-xs text-text-secondary" }, "Work: "' not in source


def test_conformance_fixture_exercises_readable_v1_approval_packet():
    source = HARNESS.read_text(encoding="utf-8")
    assert 'id: "fixture-review-attempt"' in source
    assert 'id: "fixture-synthesis-attempt"' in source
    assert 'box_id: "synthesize"' in source
    assert 'result: "pass"' in source


def test_conformance_harness_supplies_direct_graph_v1_fixtures():
    source = HARNESS.read_text(encoding="utf-8")
    assert "graphV1RecipeFixture" in source
    assert "graphV1RunFixture" in source
    assert 'path === "/api/plugins/shipfactory/v1/recipes"' in source
    assert 'path === "/api/plugins/shipfactory/v1/runs"' in source
    assert 'path === "/api/plugins/shipfactory/v1/runs/fixture-v1-run/graph"' in source
    assert 'path === "/api/plugins/shipfactory/v1/human-boxes/fixture-human-attempt/decision"' in source
