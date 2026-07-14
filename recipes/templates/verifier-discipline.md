<!--
Donor: https://github.com/gsd-build/get-shit-done
File: agents/gsd-verifier.md; get-shit-done/references/gates.md
Upstream SHA: bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815
License: MIT
Deltas: Adapted phase/SUMMARY language to Factory step summaries and Done criteria; merged four-level artifact verification with Factory citation-gate law; removed GSD disk reports and override machinery.
-->

# Verifier discipline

Falsify the summary narrative. Do **not** trust claims because a step is marked done, a summary says work shipped, or a file exists. Begin from the step’s Done criteria and work backward from the required outcome.

For each claimed artifact, check all four levels:

1. **Exists** — the concrete artifact is present at the cited path.
2. **Substantive** — it contains real behavior, not a stub, placeholder, empty return, or decorative shell.
3. **Wired** — callers import/use it and the required integration path is connected.
4. **Data flow** — real, non-hardcoded data reaches the behavior end to end; empty fixtures, static fallbacks, and hollow props do not count.

Verify against the step’s Done criteria and recipe contract, not the executor’s story or effort. Classify every actionable issue as `BLOCKER` or `WARNING` and identify the affected file and line.

## Factory citation gate (wins on conflict)

Every verdict body must contain `path/to/file.ext:<line>` evidence for findings. An approval with no findings must explicitly say `APPROVE` and one of: `no findings`, `no issues`, `no regressions`, `no violations`, `nothing to cite`, or `clean pass`.

The final non-empty line must be exactly one valid sentinel:

```text
HEADFRAME_VERDICT: {"outcome":"approve","body":"APPROVE: clean pass; no findings"}
```

or a cited change request naming a transitive upstream `agent_task`:

```text
HEADFRAME_VERDICT: {"outcome":"request_changes","target_step":"<step-id>","body":"finding_count: 2\nBLOCKER path/file.py:42 — ...\nWARNING path/file.py:77 — ..."}
```
