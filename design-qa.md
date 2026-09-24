# ShipFactory design QA

## Comparison target

- Source visual truth:
  - `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/08-open-design-project-overview.jpg`
  - `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/09-open-design-workflow-builder-fullscreen.jpg`
- Browser-rendered implementation:
  - `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/13-live-project-bound-1366x768.png`
  - `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/14-live-workflow-auth-fixed-1366x768.png`
  - Focused inspector state: `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/19-live-workflow-inspector-1366x768.png`
- Combined comparison evidence:
  - `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/15-workflow-reference-vs-live.png`
  - `/Users/abbhinnav/.codex/visualizations/2026/08/07/019fda50-1160-79e1-af7f-187d6f545cc3/shipfactory-ui-audit/16-project-reference-vs-live.png`
- Viewport: 1366 × 768 CSS px, desktop, dark theme.
- Density normalization: source workflow 1366 × 768, source project 1365 × 768, implementation 1366 × 768, `devicePixelRatio: 1`. The one-pixel project-width difference is non-material; no scaling was used for the workflow comparison.
- State: `Designer First Flight` selected; project-bound unpublished workflow; default parallel graph; inspector closed for the full-view comparison and open on `Code review` for focused evidence.

## Findings

No actionable P0, P1, or P2 differences remain.

- Fonts and typography: the implementation deliberately inherits Hermes's Mondwest/mono theme rather than copying the standalone mock's sans face. Hierarchy, weights, wrapping, truncation, and small-label legibility remain clear at the target viewport.
- Spacing and layout rhythm: the project metadata, workflow list, recent runs, horizontal add-step rail, canvas, and conditional inspector align cleanly. The wider Hermes host sidebar reduces canvas width relative to the standalone mock, but the graph remains centered and readable without horizontal overflow.
- Colors and visual tokens: the implementation uses the host's teal/background/border/state tokens. Contrast and semantic status colors are consistent with Hermes and retain the mock's quiet, low-chrome character.
- Image quality and asset fidelity: these screens contain no raster product imagery, logos, or decorative image assets. Workflow controls use the real `@xyflow/react` canvas and its icon controls; no placeholder imagery is present.
- Copy and content: L0–L3 language is available from the Model drawer. Projects, immutable workflows, separate Runs, parallel branches, live adapters, and human approval use concise operator-facing copy.

## Comparison history

### Iteration 1

- [P1] The embedded workflow designer omitted the Hermes session header, so live seats failed to load and Publish/Run would fail with 401 despite a healthy-looking canvas.
  - Fix: added a same-origin parent-token bridge and centralized authenticated fetch helper; rebuilt the shipped designer.
  - Post-fix evidence: the focused inspector shows live adapter seats, the canvas reports `Graph valid`, and Cypress verifies the session header on protected plugin APIs.
- [P1] The project screen still exposed legacy recipe hashes, enable/disable controls, and a dense launch surface instead of the selected simple overview.
  - Fix: added a compact project overview with policy/adapters/boundary metadata, attached immutable workflows, recent runs, and one `New workflow` action. The advanced attachment policy remains available through the existing API instead of dominating the primary surface.
  - Post-fix evidence: `16-project-reference-vs-live.png`.
- [P2] Project workflow and run rows stacked because plugin-only arbitrary utility classes were absent from the Hermes host stylesheet.
  - Fix: added explicit plugin CSS grid classes and a responsive single-column fallback.
  - Post-fix evidence: `13-live-project-bound-1366x768.png`.

### Iteration 2

- Recompared the equal-size project and workflow captures. No actionable P0/P1/P2 differences remained.
- Verified the node inspector, Model drawer, project selection, New workflow entry, Projects/Workflows/Runs/Settings navigation, Graph runs expansion, live adapter loading, and keyboard-visible controls.
- Browser console checked after the final reload and interactions: no warnings or errors.

## Implementation checklist

- [x] Four primary destinations: Projects, Workflows, Runs, Settings.
- [x] Compact current-project selection and usable bound-project default.
- [x] xyflow workflow editor with editable nodes, routes, parallel branches, zoom, and fit controls.
- [x] Inspector hidden until a node or edge is selected.
- [x] L0–L3 model explanation.
- [x] Authenticated live seat loading.
- [x] Immutable publish and project attachment path covered by integration tests.
- [x] Run request path covered by integration tests.
- [x] Human approval remains operator-controlled.

## Follow-up polish

- [P3] A future pass can turn an expanded Run into a dedicated full-canvas execution graph; the current Graph runs overview and inline detail are intentionally compact and functional.

## Native Hermes Desktop SDK verification

- Verified the disk plugin at `$HERMES_HOME/desktop-plugins/shipfactory/plugin.js`
  loads through the released `@hermes/plugin-sdk` runtime with no build step.
- Verified the native `/shipfactory` route, sidebar item, status chip, command
  palette entries, and keybind are registered.
- Verified live scoped API data in all four native tabs: 15 Projects, 4
  immutable Workflows, 7 recent Runs, and live adapter/cost Settings.
- Visual QA caught and fixed ISO timestamp handling in the Runs tab; SDK
  `relativeTime` now receives finite epoch milliseconds.
- Verified `Open xyflow builder` opens the authenticated dashboard and that a
  project `New workflow` action renders the real `@xyflow/react` canvas with
  editable nodes, labeled edges, parallel review branches, rework routing,
  and the protected human-approval node.
- No approval or rejection action was exercised during verification.

final result: passed
