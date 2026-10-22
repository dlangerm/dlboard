# Compatibility and stability

## What is this

dlboard is `0.x`: usable, but not yet `1.0`. This page says what you can rely on across releases and
what may still change, so you know what an upgrade can break. Releases are listed in the
[changelog](../CHANGELOG.md).

## What's stable

Breaking any of these needs a migration path and a deprecation window of at least one minor release:

- **The database upgrade path.** Any released version upgrades to any later one, with your data kept.
  See [upgrading.md](upgrading.md).
- **REST API `v1`.** Paths (all under `/api/v1/`), payload fields and status-code meanings. Only
  additive changes: a new optional field or a new path. The client ignores response fields it doesn't
  know, and the server ignores request fields it doesn't know.
- **Stored data.**
  - View and panel JSON. A view saves what it shows (its panels, the runs it selected, its Runs-table
    columns), not how it's being browsed: which panels are open and which tab is shown live in the page's
    address (`?open=`, `?tab=`). Views saved before that change may still hold `open_panel` and `active_tab`
    keys; they still load, and those keys are ignored.
    A view with no `selected_runs` draws the newest runs (20 by default, `DLBOARD_DEFAULT_SHOWN_RUNS`); one saved
    before they existed has `excluded_runs` instead, which it still leaves out of those.
  - Chart type names (`line`, `bar`, `image`, `table`, `histogram`).
  - The *values* of a table chart's `mode` (`runs`, `pivot`, `artifacts`), and of a histogram chart's `display`
    (`heatmap`, `bands`, `step`), as saved in a chart's parameters.
  - The *values* of persisted enums (roles, scopes, audit actions, entity types, experiment sources, run
    statuses).
  - Artifact refs (`file:///...`, `s3://...`).
  - The API token format, `dlb_<id>_<secret>`.
- **Configuration.** Environment variable names ([configuration.md](configuration.md)), the CLI commands
  and flags (`dlboard serve local`, `dlboard serve custom --plugins module:attr`, `dlboard users`), and
  the plugin bundle names in `dlboard.plugins`.
- **Telemetry.** The names and attributes of the exported metrics ([telemetry.md](telemetry.md)). New ones are
  added freely; one is renamed or dropped only after a release that exports both.

Enum values and chart type names are checked by a test, so removing or renaming one fails the build
and points here.

## Version skew

A client works against any server with the same REST API major (`v1`). On connect, the client reads
the server's API major and refuses to start training if it differs, with a message naming both
versions, instead of silently dropping every batch. The REST API major only changes for a breaking
change, which this policy says won't happen within `0.x` without a deprecation window.

`dlboard` requires a `dlboard-client` of its own minor version (`~=0.4.0`, say, so patch releases mix freely), so
any install that has both stays matched. A client in a training environment can still be any version with the
same API major, but fixes and new features land in both, so keep them close.

A client that logs histograms against a server that predates them still works: the server ignores the
`histograms` field of a metrics batch (the REST API only ever adds optional fields), so the histograms are
dropped and everything else is stored. Upgrade the server first to keep them.

An older server can't open a database a newer version has upgraded. Upgrade servers before you
upgrade clients, and never point an older server at a database a newer one has touched.

## What may still change before 1.0

Documented as able to change in any minor release:

- The **Python plugin APIs**: the `DataStore` and `ArtifactStore` protocols, `SQLStoreBase`,
  `BlobBackend`, chart type internals, and what `dlboard.serve` re-exports. A plugin you wrote against
  one minor version may need updating for the next. The [plugin docs](plugins/overview.md) describe the
  current shape.
- The **auth provider protocol**.
- Anything underscore-prefixed, and the look of the web UI.

## Schema changes: expand, then contract

How migrations are written, which is what makes the promises above possible:

- Migrations are additive and preserve data.
- A destructive step (dropping a column, say) lands one minor release after the code stopped using it.
  That is what lets servers that are already running keep working while a rolling upgrade replaces them
  (an old server can't *start* against an upgraded database; see [upgrading.md](upgrading.md)).
- A released migration is never edited.
