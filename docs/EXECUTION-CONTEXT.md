# Lane V2 execution context

Date: 2026-07-12. Scope: `daemon.py`, `spawn.py`, `dashboard/server.py`,
`github_sync.py`, and `store.py`. “Slow” means the callee does not return;
“crashed” means it raises or the child exits unsuccessfully. The daemon and
CLI paths are synchronous unless the table says otherwise.

## Call-site matrix

| File:line | What executes here | If the callee is slow | If the callee crashes |
|---|---|---|---|
| `factory/daemon.py:14` | Hermes `dispatch_once` runs in the daemon’s main tick thread and may claim tasks and invoke `factory_spawn`. | The whole tick waits, including reaping and watchdog work. | The exception escapes `tick`; there is no dispatch isolation. |
| `factory/daemon.py:15` | `reap_finished()` runs synchronously in the same tick thread. | Polling a live PID is non-blocking, but an exited child’s log read, usage parse, SQLite finalization, or board transition can delay the tick. | Exceptions outside the board-transition guard escape `tick`; board-transition exceptions are swallowed by `spawn.py:171-174`. |
| `factory/daemon.py:21` | Watchdog tick runs in the daemon main thread. | A slow watchdog blocks the next dispatch cycle. | Only `ImportError` is handled; other exceptions escape `tick`. |
| `factory/daemon.py:28` | GitHub sync runs synchronously in the daemon main thread. | Each wrapped CLI call is bounded by the 30-second timeout at `github_sync.py:26-28`; a tick can still spend up to that bound per call. | The daemon catches sync exceptions at `daemon.py:29-36`, returns `sync=None`, and preserves dispatch/reap/watchdog results. |
| `factory/daemon.py:52` | Main loop sleeps between ticks. | Intentional delay; it does not call a callee. | No callee can crash. |
| `factory/spawn.py:74,76` | Hermes kanban SQLite connection and worker-context query run synchronously while dispatch is claiming a task. | The dispatch tick waits on the database or query. | The exception escapes `factory_spawn` and is handled by Hermes dispatch according to its spawn callback contract. |
| `factory/spawn.py:81-82,93` | Workspace and run-directory `mkdir` calls perform filesystem I/O in the dispatch thread. | Filesystem stalls block dispatch. | `OSError` escapes spawn; the task remains subject to kanban stale-claim recovery. |
| `factory/spawn.py:102` | Opens the child log file for binary output. | Open can block on filesystem latency before the child exists. | The exception escapes; `Popen` is not attempted. |
| `factory/spawn.py:104-112` | `subprocess.Popen` creates the executor child; the child itself is a separate process with stdout redirected to the log. | `Popen` can briefly wait on OS process setup; once returned, executor slowness is outside the daemon thread. | Spawn errors escape and the log handle is closed by `116-121`; no run row is created. |
| `factory/spawn.py:114-115` | Writes the prompt to the child’s stdin pipe and closes it in the dispatch thread. | A child that does not read can fill a sufficiently large pipe and block dispatch. | `BrokenPipeError` or another pipe error escapes spawn; the caller sees a failed spawn. Accepted risk: prompt sizes are expected to stay below pipe capacity. |
| `factory/spawn.py:147` | `Popen.poll()` checks a child without waiting. | A live/hung executor returns `None` immediately; `reap_finished` leaves it in `_RUNNING`. | A polling exception escapes the reaper. |
| `factory/spawn.py:151-153` | Reads an exited child’s log file in the daemon thread. | Slow filesystem I/O delays the tick. | `OSError` becomes an empty log and the run is blocked as “no result sentinel”; other read failures are not caught. |
| `factory/spawn.py:156-159` | Finalizes the run through Factory SQLite in the daemon thread. | SQLite waits up to the connection busy timeout; the tick waits. | SQLite errors escape before the `_RUNNING` row is removed, so a later tick can retry the exited record. |
| `factory/spawn.py:163-170` | Opens kanban SQLite and completes/blocks the task synchronously. | Board I/O delays only the current reaping tick. | The broad guard at `171-174` preserves the finalized run and leaves kanban stale/crash recovery to repair the task. |
| `factory/dashboard/server.py:29-38` | Dashboard token directory/file reads and writes happen while creating the server, in the caller thread. | Server startup waits on filesystem latency. | Missing-token creation handles only `FileNotFoundError`; write or mkdir errors escape. `chmod` errors are accepted. |
| `factory/dashboard/server.py:60` | Writes an HTTP response to the client socket in that request’s handler thread. | A slow client can hold its handler thread while sending the body. | Socket errors terminate that request; other request threads continue. |
| `factory/dashboard/server.py:70-88,99` | Store accessors run in one handler thread per HTTP request. | A slow accessor blocks only that request because `ThreadingHTTPServer` creates separate handler threads. It can still consume threads if many requests are slow. | Accessor exceptions are not generally caught and terminate that request; `/seats` has a local fallback catch at `77-78`. |
| `factory/dashboard/server.py:111` | Binds the localhost listening socket in the caller thread. | Bind waits on OS socket setup. | `PermissionError`/bind errors escape `create_server`. |
| `factory/dashboard/server.py:118-120` | `serve_forever` accepts sockets and dispatches handler threads; shutdown/close runs in the serving thread’s caller. | The accept loop remains available while individual handlers wait. | KeyboardInterrupt is handled; socket/server errors escape `serve`, after `server_close` in `finally`. |
| `factory/github_sync.py:20` | Lazy imports of Factory/Hermes modules may perform Python module/file I/O in the caller thread. | Import latency delays sync or the daemon tick. | Import errors escape the sync function; daemon sync catches them, direct CLI callers see them. |
| `factory/github_sync.py:26-28` | `subprocess.run` invokes `gh` or `hermes kanban` synchronously with captured output and a 30-second timeout. | The call blocks the caller until completion or timeout; `TimeoutExpired` is bounded and reaches daemon best-effort handling. | Nonzero exit, timeout, or OS launch errors raise from `_run`; daemon catches them, direct sync callers do not. |
| `factory/github_sync.py:112,122-123` | Conflict logging creates a directory and appends one JSONL record in the sync caller thread. | Filesystem latency delays the sync tick. | File errors escape conflict handling and then daemon best-effort handling can discard the sync result. |
| `factory/github_sync.py:141,165,185,201,203,225-227` | These are all synchronous `_json_command`/`_run` call sites for create, edit, complete, and comment operations. | They inherit `_run`’s 30-second bound per subprocess and serialize the sync pass. | They inherit `_run`’s exception behavior; no partial operation is rolled back, but the next explicit sync can reconcile state. |
| `factory/github_sync.py:220,228,247,254` | Sync mapping reads/upserts call Factory SQLite in the sync caller thread. | SQLite contention waits under the store connection timeout. | SQLite errors abort the pass; daemon mode records `sync_error`, direct mode raises. |
| `factory/store.py:27-32` | Creates the Factory directory, opens SQLite, enables foreign keys, sets a 5-second busy timeout, and selects WAL mode. | Directory/SQLite/PRAGMA work blocks the accessor caller; writer contention waits rather than failing immediately. | Filesystem or SQLite errors escape `_connect`; public store methods propagate them. |
| `factory/store.py:43-62` | `executescript` creates the schema in a SQLite transaction. | Schema initialization waits on other writers. | SQLite errors propagate; callers cannot use the store until initialization succeeds. |
| `factory/store.py:68-71,78-80,86-88,95-96,102-104,110-111,117-121,127-128,134-135,141-142,148-151,157-158,164-165,171-173,179-180,193-197,203-205,211-214` | All public store accessors execute SQLite reads/writes using a fresh connection and a context manager. | The caller waits for SQLite; WAL reduces reader/writer interference and `busy_timeout` covers transient writer contention. | `sqlite3.Error` propagates from the accessor; context-manager rollback/close prevents a partial transaction from being committed. |

## Case-law findings

| Pattern | File:line | Result |
|---|---|---|
| V2-a: synchronous GitHub child with no timeout | `factory/github_sync.py:26-28` | **Fixed.** Added `_COMMAND_TIMEOUT = 30.0` and passed `timeout=`. `tests/test_adversarial.py::test_slow_github_call_has_timeout_and_does_not_wedge_tick` proves a simulated slow `gh` call leaves the daemon tick bounded. |
| V2-b: executor remains alive after claim TTL | `factory/spawn.py:147-149` | **Accepted risk by design.** Reaping uses `poll()` only and never waits or kills a live child. Hermes kanban owns stale-claim TTL and live-PID recovery; the adversarial test proves the Factory reaper remains bounded and retains the record. |
| V2-c: slow dashboard accessor | `factory/dashboard/server.py:9,111` | **Accepted risk by design.** `ThreadingHTTPServer` isolates a slow accessor to its request thread. The test proves another request completes; this localhost dashboard still has no per-request deadline or thread cap. |
| V2-d: malformed or conflicting result sentinels | `factory/spawn.py:130-138` | **Accepted risk, already correct.** Parsing inspects only the last non-empty line, so garbage or earlier sentinels cannot override the final line. The adversarial parametrization covers garbage, multiple sentinels, and malformed final lines. |
| V2-e: concurrent SQLite writers | `factory/store.py:28-32` | **Fixed.** Added explicit `timeout=5.0`, `busy_timeout=5000`, and WAL. Two threads perform 50 run/policy writes each without leaking `database is locked`; the test also verifies the PRAGMAs. |
| V2-f: citation regex on hostile input | `factory/policy.py:16,32-40` | **Fixed.** Replaced the greedy path regex with a linear suffix-plus-prefix scan. Empty, Unicode, regex metacharacters, clean approval, and a 10 MB body are covered, with the large case completing under two seconds. |

## Operator reconciliation (2026-07-12, post-V2)

Lane V2's table classified `spawn.py:114-115` (stdin pipe write) as
"Accepted risk: prompt sizes are expected to stay below pipe capacity."
**That classification was WRONG — overturned by empirical test:** kanban
worker context (task body + comments + parent chain) regularly exceeds
100KB; the macOS pipe buffer is 64KB; a live probe (200KB write to a
non-reading child) blocked indefinitely with no exception. This was the
single most dangerous defect in the plugin — the daemon-wedging class the
NousResearch sweeper caught in PR #62496.

Fixed (#16-OPERATOR F1): prompt is written to a durable file
(runs/<task>.prompt) and the child gets a real file as stdin. Regression
test: test_huge_prompt_to_non_reading_child_does_not_wedge_dispatch.
Bonus: the .prompt file is a durable audit artifact per run.

Remaining operator findings deferred with owners (not silent):
- F2 _RUNNING process-memory only — daemon restart orphans live harness
  runs (tasks recover via kanban TTL, telemetry lost). Fix when factory
  goes live: persist pid map in store, rescan on start.
- F3 record_run_start after Popen — locked-store failure leaves an
  untracked running harness. Mitigated by WAL+busy_timeout (V2 fix);
  full fix rides F2's persistence change.
- Model-capability note: the analysis half of this lane ran on luna-high;
  it produced a faithful call-site TABLE (26 rows, honest mechanics)
  but misjudged the one severity that mattered (F1 "accepted risk").
  Operator independent pass caught it. Lesson: cheap models enumerate,
  strong models judge severity — keep analysis-grade review on the
  strong lane.
