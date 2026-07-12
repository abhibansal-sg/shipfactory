# Hermes Factory

Hermes Factory adds teams, hierarchy, review/approval policy, watchdog recovery, GitHub Issue sync, and cost telemetry on top of Hermes kanban.

## Install

Copy this repository into the Hermes plugin directory:

```sh
mkdir -p "$HERMES_HOME/plugins"
cp -R . "$HERMES_HOME/plugins/factory"
```

If `HERMES_HOME` is unset, Hermes normally uses `~/.hermes`, so the installed path is `~/.hermes/plugins/factory/`.

## Seat configuration

`hermes factory init` creates `$HERMES_HOME/factory/seats.yaml`. A representative configuration is:

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
hermes factory init
hermes factory seats
hermes factory daemon --once
hermes factory dashboard
```

The dashboard prints its localhost URL with the required token. Its default port is 18820.

## GitHub webhook wake-up

Explicit synchronization is available through `hermes factory sync --board straits-lab-eng --repo OWNER/REPO`. To let Hermes wake targeted syncs for GitHub activity, add an Issues/Issue Comment/Pull Request subscription:

```sh
hermes webhook add --repo OWNER/REPO --events issues,issue_comment,pull_request --command "hermes factory sync --board straits-lab-eng --repo OWNER/REPO"
```

Hermes owns webhook transport and authentication; Factory uses the already-authenticated `gh` CLI and stores no GitHub token.
