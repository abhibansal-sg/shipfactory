# LANE BRIEF — W5: Seat-creation write surface (dashboard + CLI)

You are a non-interactive build lane. You run headless — NEVER stop to ask
permission; build/install what you need (incl. Playwright if needed for
verification) and note it in your report. Work ONLY in your worktree:
`/tmp/lane-seat-ui` (branch `lane/seat-ui` off main).

## Context
Repo: hermes-factory (Hermes dashboard plugin). The factory tab already has
write surfaces W1–W4 (run-recipe form, triage form, reroute/cancel,
daemon-status chip). Operator-ratified design: a seat is an EMPLOYMENT
CONTRACT (profile × model × reasoning × role × max_concurrent), the profile
list is the labor pool; the seats page shows who's hired and lets the
operator hire.

**Finding #12 (hard law):** seat model resolution is split-brained — seats.yaml
model/executor fields govern only the codex spawn path; a profile-spawned
seat gets its model from `~/.hermes/profiles/<seat-profile>/config.yaml`, and
a MISSING profile config silently inherits the global default (caused live
429 crashes). Therefore seat creation = TWO files: the seats.yaml row AND
the profile's model config. A seats.yaml model that disagrees with the
profile config is a lie.

Current seats.yaml: `~/.hermes/factory/seats.yaml` (see `factory/config.py`
for the loader). Working example of a correct profile model config:
`~/.hermes/profiles/dev-backend/config.yaml` (claude-sonnet-5 via local
anthropic proxy :18808). READ BOTH before designing.

## Deliverables

**S1 — `factory/seats_admin.py` (new module): create/update seat.**
`create_seat(name, profile, executor, model, reasoning, role,
max_concurrent, provider_config=None) -> dict`:
- Validates: name unique, executor in {hermes, codex, claude}, profile
  exists under `~/.hermes/profiles/` OR profile == 'default',
  max_concurrent >= 1.
- Writes the seats.yaml row (preserve comments/ordering as best YAML
  round-trip allows; if ruamel.yaml is not a dep, a documented
  re-serialization that keeps the header comment block is acceptable —
  state which you did).
- **If executor == 'hermes' (profile-spawned): ensure the profile's
  config.yaml exists with an explicit model block.** If `provider_config`
  is given, write it; if the profile has NO config.yaml and no
  provider_config was supplied, REFUSE with a clear error naming finding
  #12 — never create a seat that would silently inherit the global default.
- Returns the effective seat record. Also `update_seat(...)` with the same
  validation, and `list_profiles() -> list[str]` (dirs under
  ~/.hermes/profiles/ + 'default').
- All paths must respect HERMES_HOME env override (tests depend on it —
  never touch the real ~/.hermes in tests).

**S2 — CLI verbs.** `hermes headframe seat-create` / `seat-update` /
`seat-list` wired to S1 (follow the existing cli.py verb pattern). CLI is
the single writer; keep business logic in S1, not in the endpoint.

**S3 — API endpoints.** In `dashboard/plugin_api.py`: extend GET `/seats`
to include per-seat resolved detail (profile-config model vs seats.yaml
model, flag mismatches), add GET `/profiles`, POST `/seats` (create), PUT
`/seats/{name}` (update). Every POST/PUT routes through the S1 functions
(same code path as CLI). Invalid params ⇒ 400/422 with the validation
message. NOTE: dashboard plugin API routes load at process startup — after
merge the dashboard needs a restart for new routes; say so in your report.

**S4 — UI: "Create seat" on the Seats view.** Form fields: profile
(dropdown from GET /profiles), executor, model + provider lane, reasoning
(low/medium/high), role (free text w/ suggestions engineer/qa/designer),
max_concurrent (default 1). For hermes-executor seats show the provider
config sub-form (base_url, provider name, model) prefilled from an existing
seat's config as template. Edit affordance per seat row. Show a visible
MISMATCH badge when seats.yaml model ≠ profile-config model.

## UI conformance LAWS (operator rejected two prior passes — binding)
- Compose the HOST's global Tailwind utilities + real host Button/Badge/
  Card components exactly as the existing factory tab does (main's
  `dashboard/` source is the reference; style.css is only for what
  utilities can't express).
- font-family ONLY `inherit` or var(--font-mono); ZERO literal font names.
  `grep -cE '#[0-9a-fA-F]{3,6}' dist/style.css` must stay 0. Every
  `var(--x)` must exist in the live host bundle or carry a fallback.
- The shared `request()` helper already sets Content-Type on write bodies
  (finding #16) — USE it, don't hand-roll fetch.
- **Verification MUST exercise the rendered buttons** (Playwright against
  the built harness or the live dashboard) — curl endpoint proofs are
  necessary but never sufficient (finding #16 law). Capture a screenshot or
  computed-style note as evidence.

## Requirements
- Tests: S1 validation matrix (refusal on missing profile config for
  hermes seats, mismatch detection, HERMES_HOME isolation), endpoint tests
  (200 + 400 paths). Full suite green ×2; print counts. Baseline on main:
  90 tests green.
- Do not touch advancer.py / daemon.py / primitives.py — a concurrent lane
  owns them.
- Commit in logical units on `lane/seat-ui`.

## Honesty clause
Print any clause you could not satisfy literally — say so plainly, do NOT
improvise around it. `DONE_WITH_CONCERNS` + enumerated deviations is a good
outcome. Never silently fix pre-existing baseline breakage.

## Final line of your output MUST be exactly one of:
`LANE_RESULT: done <one-line summary>`
`LANE_RESULT: blocked <one-line reason>`
