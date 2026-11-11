# Documentation

Every page, grouped by what you're doing. If you're new, read the first two.

## Start here

- [Architecture](architecture.md) — how dlboard fits together: the plugin system, and the request flow from a training
  script to the browser.
- [Client & logging](client.md) — `DLBoardLogger`, the REST client, and how to log a new kind of artifact.

## Run it

Install, configure and deploy a server.

- [Docker](docker.md) — build and run the image: Postgres and local disk by default, S3 optional.
- [Configuration](configuration.md) — every environment variable, with its default.
- [Reverse proxy & TLS](reverse-proxy.md) — Caddy and nginx examples.
- [Upgrading](upgrading.md) — move to a newer version without losing data.
- [Backup & restore](backup.md) — what to back up, and how to restore it.
- [Compatibility](compatibility.md) — what stays stable across releases, and what may still change.

## Operate it

Day-to-day use and upkeep.

- [Runs, pins & comparisons](comparisons.md) — the run list, flagging the runs that matter, and comparing runs of
  several experiments.
- [Retention](retention.md) — how many runs, or how old, a project keeps, kept runs, and when the Trash is emptied.
- [Background tasks](background-tasks.md) — the custodian process, running several servers, watching it, and writing a
  task.
- [Telemetry](telemetry.md) — Prometheus and OpenTelemetry metrics and logs, for Grafana, Mimir and Loki.
- [Grafana dashboards](grafana/README.md) — the dashboards dlboard ships for its metrics, traces and logs.

## Extend it

Storage, auth, charts and theming are all plugins; page layout is the one deliberate exception (see
[architecture](architecture.md)).

- [Plugin overview](plugins/overview.md) — the plugin protocol, and how plugins are composed into a deployment.
- [Storage](plugins/storage.md) — where projects, runs and artifacts live: Postgres, S3, or your own backend.
- [Charts](plugins/charts.md) — a new way to draw logged metrics and artifacts.
- [Auth](plugins/auth.md) — signing people in, API tokens, and who can see which projects.
- [Themes](plugins/themes.md) — the Mantine theme the app renders with.
- [The plugin skill](../skills/dlboard-plugin/SKILL.md) — instructions for a coding agent writing a plugin for you.
