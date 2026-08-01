"""Static guards for the operator approval card (findings #133-#136).

The card is the operator's trust surface: it must show the PLAN being approved
(not a critic's attack on it), render worker markdown as real elements rather
than one flat ``<pre>``, collect feedback when the operator rejects, and expose
an audited cancel path. These are source-level guards in the same style as
``test_dashboard_graph_v1.py`` — the bundle ships as hand-written ES5 with no
build step, so its text IS the artifact.
"""

from pathlib import Path

from shipfactory import store as _store  # noqa: F401 -- establish package import order


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "dashboard" / "dist" / "index.js"
STYLE = ROOT / "dashboard" / "dist" / "style.css"


def test_operator_surface_never_injects_raw_html():
    """Finding #51 is law: model text renders as React children, never HTML.

    A worker authors the plan and the critic verdicts. If any of that reached
    an HTML-injection prop, a hostile or merely careless model could execute
    script in the operator's approval surface.
    """
    source = BUNDLE.read_text(encoding="utf-8")
    assert "dangerouslySetInnerHTML" not in source
    assert "innerHTML" not in source


def test_markdown_renders_as_elements_not_a_flat_pre():
    source = BUNDLE.read_text(encoding="utf-8")
    assert "function renderMarkdown(source, keyPrefix)" in source
    assert "function MarkdownBlock(props)" in source
    # Block constructs the operator actually receives from planners.
    for element in ('h("table"', 'h("thead"', 'h("blockquote"', 'h("hr"', 'h("li"'):
        assert element in source, element
    # Unrecognised input must still reach the operator as text.
    assert "// Anything unrecognised becomes a paragraph — never dropped, never executed." in source


def test_plan_selection_prefers_declared_artifact_then_skips_critics():
    """The card must not label the adversary's attack as the deliverable."""
    source = BUNDLE.read_text(encoding="utf-8")
    assert "var GRAPH_V1_CRITIC_RE = /review|attack|critic|adversar/i;" in source
    assert "var declared = graph.recipe && graph.recipe.approval_artifact;" in source
    assert "planAttempt = graphV1LatestCompleted(attempts, declared);" in source
    # Positional fallback: walk predecessors backwards, skipping critics.
    assert "for (var i = preceding.length - 1; i >= 0; i -= 1) {" in source
    assert "if (candidateId && !graphV1IsCritic(candidateBox, candidateId)) {" in source
    # And the original behaviour survives as the last resort.
    assert ": graphV1WorkText(proposedItem && proposedItem.output_work);" in source


def test_critic_verdicts_are_rendered_and_kept_collapsed():
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'h("strong", null, "Critic verdicts")' in source
    # A box named `decomposition-attack` must appear as a critic; the old
    # /review/-only filter dropped it from the card entirely.
    assert "if (!graphV1IsCritic(box, item.box_id)) return;" in source
    assert 'className: "factory-v1-review-stack"' in source


def test_rejection_requires_operator_feedback_before_submit():
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'var needsFeedback = pendingResult === "rejected";' in source
    assert "var canSubmit = pendingResult && (!needsFeedback || feedback.trim());" in source
    assert 'h("textarea", {' in source
    assert '"Why are you rejecting? (required)"' in source
    # The confirm control is disabled until the contract is satisfied.
    assert "disabled: !canSubmit || !!props.busy," in source
    # And the reason actually reaches the API.
    assert "if (reason) payload.reason = reason;" in source


def test_cancel_run_is_two_step_and_posts_the_audited_endpoint():
    source = BUNDLE.read_text(encoding="utf-8")
    assert '"/v1/runs/" + encodeURIComponent(graph.run.id) + "/cancel"' in source
    # Two-step: an opener, then a distinct confirm control.
    assert '"data-graph-v1-cancel": graph.run.id' in source
    assert '"data-graph-v1-cancel-confirm": graph.run.id' in source
    # Only offered while the Run can actually be cancelled.
    assert 'var cancellable = ["running", "paused", "escalated"].indexOf(graph.run.state) >= 0;' in source


def test_protected_decision_contract_is_not_regressed():
    """The card gained UI; it must not have lost its protected-decision shape."""
    source = BUNDLE.read_text(encoding="utf-8")
    assert "nonce: newNonce()" in source
    assert 'actor_kind: "human"' in source
    assert 'actor_id: "local-operator"' in source
    assert '"/v1/human-boxes/" + encodeURIComponent(attempt.id) + "/decision"' in source


def test_markdown_styles_ship_with_the_bundle():
    style = STYLE.read_text(encoding="utf-8")
    for selector in (
        ".factory-md-h", ".factory-md-table", ".factory-md-pre",
        ".factory-md-list", ".factory-v1-review-stack", ".factory-v1-feedback",
        ".factory-v1-cancel",
    ):
        assert selector in style, selector
