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
