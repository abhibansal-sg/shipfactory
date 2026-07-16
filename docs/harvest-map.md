# Harvest map

Factory deliberately borrows harness semantics, not Paperclip infrastructure.

| Paperclip source module | Semantics drawn from it | Factory module |
| --- | --- | --- |
| `adapter-codex-local/dist/server/codex-args.js` | `codex exec --json`, stdin prompt, model and reasoning configuration | `factory/executors/codex_exec.py` |
| `adapter-codex-local/dist/server/parse.js` | JSONL usage event parsing and graceful unknown usage | `factory/executors/codex_exec.py` |
| `adapter-codex-local/dist/server/execute.js` | workspace-scoped execution and streamed output handling | `factory/spawn.py` |
| `adapter-claude-local/dist/server/execute.js` | `--print - --output-format stream-json --verbose`, model/effort and workspace scope | `factory/executors/claude_exec.py` |
| `adapter-claude-local/dist/server/parse.js` | stream-JSON usage parsing with parse-failure fallback | `factory/executors/claude_exec.py` |
| `shared/dist/types/issue.d.ts`, `server/dist/services/issue-execution-policy.d.ts` | staged review/approval policy model | `factory/policy.py` (Lane A/C) |
| task-watchdog service modules | fingerprinted no-op watchdog semantics and recovery ladder | `factory/watchdog.py` (Lane C) |

## Artifact-discipline donors

| Donor | Files taken | Factory destinations / semantics | License | Retrieval reference |
| --- | --- | --- | --- | --- |
| GSD (`gsd-build/get-shit-done`) | `agents/gsd-executor.md`, `agents/gsd-verifier.md`, `agents/gsd-plan-checker.md`, `get-shit-done/templates/summary.md`, `get-shit-done/templates/continue-here.md`, `get-shit-done/references/gates.md` | `recipes/templates/{executor-discipline,verifier-discipline,plan-check,summary-frontmatter,continue-here}.md`; deviation authority, goal-backward verification, artifact/data-flow checks, revision-stall semantics, and ephemeral resume handoff | MIT | SHA `bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815`, retrieved 2026-07-14. The three `get-shit-done/...` paths were resolved from the GitHub API tree after the shorter paths returned 404. |
| Spec Kit (`github/spec-kit`) | `templates/commands/specify.md`, `templates/commands/clarify.md`, `templates/spec-template.md`, `templates/checklist-template.md` | `recipes/templates/{clarification-contract,requirements-checklist}.md`; informed defaults, maximum-three clarification markers, priority ordering, gate blocking, and requirements-as-unit-tests doctrine | MIT | Local donor clone SHA `6664cf813cb943fd9ac0ab2aab60c11798913c13`, read 2026-07-14 |
