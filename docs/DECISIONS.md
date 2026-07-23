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
- Mechanism: mapping table in ShipFactory's own store (`board_slug →
  project_id`, `UNIQUE(board_slug)`; 1:1 enforced as policy with uniqueness on
  the project side too). Zero Hermes core modification. If Hermes later ships
  native project↔board linking, migrate the one table and delete ours.
- Rejected alternatives: 1:N boards per project (no real scenario survived
  scrutiny — recipe/priority/seats already cover the imagined splits);
  upstream `project_id` on Hermes boards (breaks the no-core-mod law, blocks
  on external review).
- Surfacing: a **Projects tab** in the ShipFactory dashboard view — project →
  its flights (board level flattened away), plus the unclassified bucket.
