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
