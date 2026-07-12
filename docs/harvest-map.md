# Paperclip harvest map

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
