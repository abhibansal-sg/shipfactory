# Headframe

> *A headframe is the hoist structure that stands over a mine shaft. Workers extract value below; nothing surfaces without the operator's signal.*

Headframe is a governed software factory for [Hermes](https://hermes-agent.nousresearch.com) agents. It adds teams, hierarchy, review/approval policy, watchdog recovery, GitHub Issue sync, and cost telemetry on top of Hermes kanban.

## Install

Copy this repository into the Hermes plugin directory:

```sh
mkdir -p "$HERMES_HOME/plugins"
cp -R . "$HERMES_HOME/plugins/headframe"
```

If `HERMES_HOME` is unset, Hermes normally uses `~/.hermes`, so the installed path is `~/.hermes/plugins/headframe/`.

## Seat configuration

`hermes headframe init` creates `$HERMES_HOME/headframe/seats.yaml`. A representative configuration is:

```yaml
company: straits-lab-eng
seats:
  verifier:
    profile: verifier
    executor: claude
    model: sonnet-5
    reasoning: adaptive
    reports_to: hermes-cos
    role: qa
    max_concurrent: 2
  dev-backend:
    profile: dev-backend
    executor: codex
    model: gpt-5.6
    reasoning: medium
    reports_to: architect
    role: engineer
hierarchy_gates:
  landers: [release]
  verdicts: [verifier]
```

## Quickstart

```sh
hermes headframe init
hermes headframe seats
hermes headframe daemon --once
hermes headframe dashboard
```

The dashboard prints its localhost URL with the required token. Its default port is 18820.

## Recipe artifact quick reference

`recipes/dev-pipeline@2.yaml` is the artifact-disciplined software-change flow:
plan-check → build → verify → operator approval → notify. Version 1 remains
published and immutable. The canonical agent contracts live under
`recipes/templates/`; recipe instructions reference those files and inline a
short executable summary.

Review loops park as `review_stall` when two consecutive parseable rejection
counts do not shrink. An operator can authorize one more bounded revision with
an audited reason:

```sh
hermes headframe recipe release INSTANCE VERIFY_STEP --reason "operator rationale" --board BOARD
```

Approval gates and event waits carry a `CONTINUE-HERE` kanban comment and gain
a `RESUMED <timestamp>` marker when consumed. Selector output with unresolved
clarifications parks the source task for input and never instantiates marked
work.

## GitHub webhook wake-up

Explicit synchronization is available through `hermes headframe sync --board straits-lab-eng --repo OWNER/REPO`. To let Hermes wake targeted syncs for GitHub activity, add an Issues/Issue Comment/Pull Request subscription:

```sh
hermes webhook add --repo OWNER/REPO --events issues,issue_comment,pull_request --command "hermes headframe sync --board straits-lab-eng --repo OWNER/REPO"
```

Hermes owns webhook transport and authentication; Factory uses the already-authenticated `gh` CLI and stores no GitHub token.
