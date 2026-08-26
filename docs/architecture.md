# Architecture

## What is this

dltrack is a single Dash web app assembled at startup from a flat list of **plugins** — storage,
pages, charts, auth, theming. There's no separate "core" that hardcodes any of those choices; the
core app just gives plugins a few places to hook into a running `Dash` instance.

## When you'd need this

Read this before touching `dltrack/serve/app.py`, adding a new kind of plugin category (not a new
plugin — a new *category*, which is rare), or trying to understand how a request from a training
script ends up as a row in the database and eventually a chart in the browser.

## How it works

**Composing the app.** `dltrack.serve.app.app(plugins: list[PluginProtocol]) -> Dash`
(`dltrack/serve/app.py`) builds a `Dash` instance, calls `plug(app)` on every plugin in order, and
wires the shared `AppShell` layout (header, collapsible navbar, page container) plus a few global
callbacks: breadcrumbs, navbar collapse state (persisted to `localStorage`), and the navbar's
project/experiment listing. `serve.py` is the reference composition — it passes
`dltrack.plugins.LOCAL_DEPLOYMENT` (sqlite storage, filesystem artifacts, anonymous auth, the REST
backend, the four built-in pages, the four built-in chart types) plus a theme.

**Where plugins reach shared state.** A plugin's `plug(app)` registers whatever it needs — routes,
pages, a chart renderer, the data store — directly on the `Dash` app instance. Anything another
plugin needs to read back (the data store, the artifact store, the auth provider, the current
user) goes through small `@cache`d accessor functions in `dltrack/serve/_backend/` (e.g.
`get_data_store()`, `get_auth_provider()`, `get_current_user()`) rather than plugins reaching into
each other directly. See [plugins/overview.md](plugins/overview.md) for the plugin contract itself.

**Request flow, client to server.**
1. `DLTrackLogger` (`dltrack/client/dltrack_logger.py`) creates a run via `BasicDltrackAPI`
   (`dltrack/plugins/backend/basic_rest_backend.py`) and spawns two daemon subprocesses that batch
   metrics/artifacts off multiprocessing queues and flush them to the REST API on an interval, so
   training never blocks on network I/O.
2. The server-side half of `basic_rest_backend` exposes the ingest routes those batches land on.
3. Storage is split into a `DataStore` (structured metadata — projects, experiments, runs, metrics,
   hparams) and an `ArtifactStore` (blob storage), both defined as generic protocols in
   `dltrack/models/_data_store.py`. `SQLStoreBase` (`dltrack/serve/_backend/_sql_store_base.py`)
   implements `DataStore` against raw SQL; the `sqlite`/`filesystem` plugins under
   `dltrack/plugins/data_stores/` supply the concrete backends `serve.py` uses.
4. Browser-side, routed pages under `dltrack/serve/_pages/` and `dltrack/plugins/pages/` (Dash's
   file-based `use_pages` routing) read back through the same `DataStore`/`ArtifactStore` accessors
   to render what got logged.

**Shared models.** `dltrack/models/` holds the Pydantic models both client and server import
(`Experiment`, `Run`, `Project`, `HyperParams`, `LoggedMetrics`, `Artifact`, and the chart/view
models `Page`/`ChartType`). Most have a `New*` variant for creation payloads versus the
persisted/read model. Dash component-id constants that are genuinely shared across plugins live in
`dltrack/models/constants.py` — a plugin-local id has no business there.
