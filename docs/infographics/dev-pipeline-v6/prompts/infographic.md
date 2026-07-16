---
layout: linear-progression
style: technical-schematic
aspect_ratio: 16:9
language: English
backend: Hermes native image_generate
---

Create a professional landscape 16:9 raster infographic.

TYPE: Engineering workflow infographic.
LAYOUT: Linear progression, process variant. Use three stacked horizontal phase bands connected as one continuous serpentine path. Number every node. Clear start and end. Precise directional arrows. Add thin backward rework arrows without clutter.
STYLE: Technical schematic / modern blueprint. Deep navy blueprint background (#0B1F33) with a subtle engineering grid. Crisp white and cyan lines, blue model-work nodes (#2563EB), violet independent-review nodes, teal Factory-control nodes, and one amber human-only gate (#F59E0B). Clean sans-serif typography, consistent stroke weights, strong contrast, generous whitespace. Flat precise geometry, not 3D, not cyberpunk, no decorative characters.

TITLE:
ShipFactory dev-pipeline@6

SUBTITLE:
Sealed inputs → independent evidence → human approval

MAIN WORKFLOW — preserve these labels and this exact order:

PHASE BAND 1 — DISCOVER & SPECIFY
1 EXPLORE
small caption: readonly map
2 SPEC
small caption: typed task-spec
3 SPEC ATTACK
small caption: adversarial review
4 PLAN
small caption: executable plan
5 PLAN ATTACK
small caption: adversarial review

PHASE BAND 2 — BUILD & PROVE
6 BUILD
small caption: approved source paths
Place a teal shield immediately after BUILD labeled FACTORY COMMIT with tiny caption journal → atomic ref
7 VERIFY
small caption: runtime evidence
8 CORRECTNESS
small caption: exact sealed inputs
9 ADVERSARIAL
small caption: exact sealed inputs

PHASE BAND 3 — DECIDE & REPORT
10 REVIEW STORY
small caption: canonical operator view
11 HUMAN APPROVAL
small caption: operator only
Make this the single amber stop-gate with a clear human-hand icon; add a small warning label AGENTS NEVER APPROVE.
12 NOTIFY
small caption: after decision

REWORK ARROWS:
- SPEC ATTACK loops back to SPEC
- PLAN ATTACK loops back to PLAN
- CORRECTNESS and ADVERSARIAL loop back to BUILD
- HUMAN APPROVAL rejection loops back to BUILD node 6. Draw this dotted arrow
  from the amber gate upward/left across the phase boundary to BUILD itself.
  It must not terminate at REVIEW STORY.
Use thin dotted backward arrows and a tiny legend label BOUNDED REWORK.

BOTTOM TRUST-BOUNDARY STRIP — six concise badges with simple icons:
- Git metadata ≠ identity
- Recipe bytes rehashed
- Durable run identity
- App = instance + HEAD + workspace
- Review executor strict MCP
- Human-only approval

BOTTOM LEGEND:
Blue: model work
Violet: independent review
Teal: Factory control
Amber: human gate

Hard requirements:
- English only.
- Spell every title and node label exactly as supplied.
- No private file paths, tracker IDs, competitor names, provider logos, test counts, or invented claims.
- Do not add paragraphs. Keep captions short and legible at PR-page width.
- Make the workflow and human-only stop understandable within five seconds.
- Output one polished bitmap; no mock device frame and no watermark.
