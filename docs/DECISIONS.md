# ShipFactory — Ratified Design Decisions

Operator-ratified decisions from the factory-tuning design loop. Each entry is
closed unless explicitly reopened. Reference these before proposing structure.

## D-001 · Projects: totality + 1:1 board binding (2026-07-23)

- Every unit of factory work belongs to exactly one **project**. Projects are
  Hermes-native objects (`hermes project`, `projects.db`) — ShipFactory reads
  them, never redefines them.
- **One project ↔ one board.** Every board belongs to exactly one project;
  every project has exactly one board. Board derives from project — the
  operator thinks in projects, the board is an implementation detail.
- Boards with no mapping render under **unclassified** — visible, never
  blocking. Totality by default, not by force.
- Mechanism (**revised, closed 2026-07-23**): reuse Hermes' native binding —
  `hermes project bind-board <project> <board>` (stored on the project row in
  `projects.db`). The factory READS this read-only to resolve a board's owning
  project; it builds no mapping store of its own. Unbound board → shows as
  **Unclassified**. A board bound by two projects (Hermes doesn't prevent it)
  renders under the first and warns — no enforcement machinery built.
  Zero Hermes core modification. Live: `shipfactory → factory-selfbuild1`
  bound.
- Rejected alternatives: ShipFactory-owned mapping table/config (superseded —
  native binding is exactly 1:1 already); 1:N boards per project (no real
  scenario survived scrutiny — recipe/priority/seats already cover the
  imagined splits); upstream `project_id` on Hermes boards (breaks the
  no-core-mod law, blocks on external review).
- Surfacing: a **Projects tab** in the ShipFactory dashboard view — project →
  its flights (board level flattened away), plus the unclassified bucket.

## D-002 · Modular video-production capability + governed creative recipe (2026-07-24)

- Build **two reusable Hermes skills and one ShipFactory recipe**, not one skill
  per sequential production step:
  - `procedural-video`: deterministic NumPy/Pillow/OpenCV/FFmpeg renderer,
    reusable code templates, resumable scene rendering, and machine QC.
  - `video-production`: orchestration and lane selection across procedural,
    ASCII, tldraw, HyperFrames, generative-video, music, and delivery skills.
  - `creative-video@1`: research → treatment → styleframe → build → machine
    verification → vision review → master → human operator approval.
- Sequential mechanics such as typography checks, frame rendering, contact
  sheets, frame diffs, and audio muxing belong as scripts/references inside the
  engine skill. A separate skill is warranted only for an independently
  reusable trigger and toolchain.
- **Seat policy:** every seat exercising creative direction or design judgment
  uses `gpt-5.6-sol`; bounded research/implementation/QC seats use the best-fit
  non-creative model; at least one final correctness/adversarial review remains
  cross-provider. The recipe records resolved seat/model evidence per run.
- V1 is deliberately narrow: square deterministic Python scenes, local assets,
  one soundtrack, independent scene clips, FFmpeg master, contact-sheet and
  frame-diff evidence. No node editor, plugin registry, asset database, 3D,
  cloud render farm, or automated subjective approval.
- First dogfood: build the capability through one Linear-backed Factory flight,
  then validate it with a separate flight producing a 15–20 second ShipFactory
  launch film. One issue = one flight; the factory never decomposes Linear work.
- Rejected alternatives: one skill per pipeline step (routing ambiguity and
  drift); forcing creative work through `dev-pipeline@14` forever (software
  artifact assumptions); making HyperFrames or generative video the default
  lane (contradicts the proven Nous NumPy/Pillow/OpenCV/FFmpeg workflow).

## D-003 · Structure-first recipe simplification (2026-07-28)

- Finalise the **recipe structure before changing the engine, converting real
  workflows, or building the visual designer**. The structure is the contract
  that tells every later layer what it must support.
- Begin with the smallest visible grammar: one work box records **who does the
  work**, **what they should do**, and **where each result goes next**; arrows
  express ordinary continuation, parallel splits, joins, branches, and loops.
- Use the operator's original planner → review → worker → parallel reviews →
  synthesis → pass/rework graph as the first completeness test. Freeze the
  structure only after that graph can be represented cleanly without hidden
  execution meaning.
- The engine will then be reduced to the smallest runner for the frozen
  structure. Real workflows are converted after the runner exists; the visual
  designer comes last and renders the same executable structure directly.
- Rejected alternatives: engine-first redesign (repeats current assumptions);
  workflow-by-workflow conversion before a common structure (creates special
  cases); designer-first work (risks another projection that differs from
  execution).

## D-004 · One box for work; arrows for flow (2026-07-28)

- Every visible work box has only three required parts: **name**, **who does
  it**, and **instructions**.
- A box automatically receives the work passed from its preceding box or
  boxes. Passing work forward is the default; recipe authors do not manually
  wire ordinary intermediate artifacts into every box.
- Arrows, not boxes, own routing. An arrow connects a reported result such as
  `approved` or `changes needed` to the next box or boxes. A backward arrow is
  rework; several outgoing arrows are a split.
- Governing shorthand: **box = work; arrow = flow**.
- Rejected alternatives: separate visible node types for planning, building,
  reviewing, and decisions; routing hidden inside box-specific engine logic;
  manual artifact plumbing as part of the basic recipe structure.

## D-005 · A box returns work plus one result (2026-07-28)

- Every completed box returns exactly two conceptual values: **the work it
  produced** and **one result label**.
- The result label selects the outgoing arrow. A review might return
  `approved` or `changes needed`; a box with no branch returns `done`.
- Models decide the substance of the work and report the result. ShipFactory
  stores both and follows the matching arrow; it does not reinterpret the
  model's work to invent a different route.
- Rejected alternatives: engine-specific verdict protocols for each kind of
  work; deriving routes by parsing arbitrary prose; separate completion rules
  for planners, builders, and reviewers.

## D-006 · Split freely; join active paths (2026-07-28)

- When one result points to several next boxes, ShipFactory starts all of them
  together. Parallel work needs no separate recipe primitive.
- When those paths meet at one box, that box waits until every path started by
  the split has finished, then receives their combined work.
- ShipFactory tracks only paths that were actually activated. A branch that
  was not selected cannot hold a later join open.
- Governing shorthand: **a split starts many; a join waits for every active
  line**.
- Rejected alternatives: dedicated fan-out, collector, and synthesis
  primitives; fixed global dependencies that wait for paths a branch never
  started; requiring recipe authors to coordinate parallel completion by hand.

## D-007 · Human boxes pause for the operator (2026-07-28)

- A human box uses the same visible structure as any work box: name, who does
  it, instructions, and result-labelled outgoing arrows.
- When an active path reaches a human box, ShipFactory pauses that path and
  presents the incoming work to the operator.
- Only the human operator may choose the box's result and release its matching
  arrow. A model, worker, dashboard automation, or engine recovery path cannot
  decide or bypass it.
- Governing shorthand: **model boxes run; human boxes wait**.
- Rejected alternatives: a separate visible approval-gate grammar; allowing a
  model verdict to stand in for the operator; automatic timeout approval.

## D-008 · One request enters one starting box (2026-07-28)

- Every recipe names one starting box. Starting the recipe passes the submitted
  work request into that box.
- The recipe does not care whether the request came from an operator button, a
  Linear issue, a schedule, a webhook, or another system. Those are submission
  methods outside the recipe grammar.
- Governing shorthand: **a recipe starts when it receives work**.
- Rejected alternatives: different recipe-start primitives for each external
  source; source-specific workflow structures; several implicit starting boxes
  whose activation cannot be read from the graph.

## D-009 · Recipes end only at an explicit End box (2026-07-28)

- A successful recipe finishes only when an active path reaches a box clearly
  marked **End**. An End box has no outgoing arrows.
- An ordinary box with no matching next arrow is a broken or incomplete graph.
  ShipFactory pauses and reports the problem instead of silently declaring the
  work finished.
- Different branches may reach different explicitly labelled End boxes, such
  as completed or cancelled.
- Governing shorthand: **no arrow is not enough; the box must say End**.
- Rejected alternative: inferring successful completion from any box that
  happens to have no outgoing arrow.

## D-010 · Three failed attempts pause and escalate (2026-07-28)

- If a box crashes, times out, or otherwise fails before returning work and a
  result, ShipFactory retries that same box.
- After **three consecutive failed attempts**, ShipFactory pauses the affected
  path and reports the failure to the main orchestrator for the owning project.
- The report includes the recipe run, failed box, all three attempts, and their
  errors so the orchestrator can diagnose, retry, redirect, or escalate. The
  engine does not guess a normal result or follow a success/rework arrow.
- A successful attempt resets the consecutive-failure count for that box.
- Governing shorthand: **three failures → pause → project orchestrator**.
- Rejected alternatives: infinite automatic retry; immediate operator
  interruption on the first transient failure; treating a technical failure as
  a model-authored workflow result.

## D-011 · Three rejected rework rounds pause and escalate (2026-07-28)

- A normal backward arrow may send completed work back for correction without
  human involvement.
- If the same rework loop is rejected after **three completed correction
  rounds**, ShipFactory pauses the affected path and reports the full work and
  feedback history to the main orchestrator for the owning project.
- The orchestrator may continue with better instructions, change the worker,
  alter the workflow, or ask the operator. ShipFactory does not loop forever.
- Technical failures under D-010 and completed-but-rejected work under D-011
  are counted separately.
- Governing shorthand: **three rejected fixes → pause → project orchestrator**.
- Rejected alternatives: unlimited model-review loops; treating reviewer
  rejection as a technical crash; interrupting the orchestrator on the first
  ordinary correction round.
