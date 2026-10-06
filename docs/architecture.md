# Architecture

## What is this

dlboard is a single Dash web app assembled at startup from a flat list of **plugins** — storage,
charts, auth, theming, backend routes. The core app just gives plugins a few places to hook into a
running `Dash` instance. Page layout (home, project, admin, experiment, account) is the one deliberate
exception: it's opinionated, non-optional dlboard behavior, not a plugin category — see
[plugins/overview.md](plugins/overview.md) for why.

## When you'd need this

Read this before touching `dlboard/serve/app.py`, adding a new kind of plugin category (not a new
plugin — a new *category*, which is rare), or trying to understand how a request from a training
script ends up as a row in the database and eventually a chart in the browser.

## How it works

**Composing the app.** `dlboard.serve.app.app(plugins: list[PluginProtocol]) -> Dash`
(`dlboard/serve/app.py`) builds a `Dash` instance, calls `plug(app)` on every plugin in the caller's
list, then calls `register(app)` directly on each of the built-in pages (always, unconditionally
— they aren't `PluginProtocol` and are never part of the `plugins` argument at all), and wires the
shared `AppShell` layout (header, collapsible navbar, page container) plus a few global callbacks:
breadcrumbs, navbar collapse state (persisted to `localStorage`), and the navbar's project/experiment
listing. `dlboard serve local` is the reference composition — it passes `dlboard.plugins.LOCAL_DEPLOYMENT`
(sqlite storage, filesystem artifacts, anonymous auth, the REST backend, the four built-in chart types)
plus a theme.

**Where plugins reach shared state.** A plugin's `plug(app)` registers whatever it needs — routes,
a chart renderer, the data store — directly on the `Dash` app instance. Anything another
plugin needs to read back (the data store, the artifact store, the auth provider, the current
user) goes through small accessor functions in `dlboard/serve/_backend/` (e.g.
`get_data_store()`, `get_auth_provider()`, `get_current_user()`) rather than plugins reaching into
each other directly. See [plugins/overview.md](plugins/overview.md) for the plugin contract itself.

**Who's asking, and what they may do.** Every route sits behind one request gate, which resolves
the caller to a `User` before the route runs: by API token, session, or the auth plugin. Within a
request, `get_data_store()`/`get_artifact_store()` return wrappers bound to that user, which
enforce project-level access on every call. A storage backend never knows who's asking, and
background work no user is behind uses `get_system_data_store()` instead. See
[plugins/auth.md](plugins/auth.md).

**Request flow, client to server.**
1. `DLBoardLogger` (`dlboard/client/dlboard_logger.py`) creates a run via `BasicDlboardAPI`
   (`dlboard/plugins/backend/basic_rest_backend.py`) and spawns two daemon subprocesses that batch
   metrics/artifacts off multiprocessing queues and flush them to the REST API on an interval, so
   training never blocks on network I/O.
2. The server-side half of `basic_rest_backend` exposes the ingest routes those batches land on.
3. Storage is split into a `DataStore` (structured metadata — projects, experiments, runs, metrics,
   hparams) and an `ArtifactStore` (blob storage), both defined as generic protocols in
   `dlboard/models/_data_store.py`. `SQLStoreBase` (`dlboard/serve/_backend/_sql_store_base.py`)
   implements `DataStore` on SQLAlchemy Core, with tables generated from the pydantic models; the
   `sqlite`/`filesystem` plugins under `dlboard/plugins/data_stores/` supply the concrete backends
   `dlboard serve local` uses.
4. Browser-side, routed pages under `dlboard/serve/_pages/` (Dash's file-based `use_pages` routing)
   read back through the same `DataStore`/`ArtifactStore` accessors to render what got logged.

**Shared models.** `dlboard/models/` holds the Pydantic models both client and server import
(`Experiment`, `Run`, `Project`, `HyperParams`, `LoggedMetrics`, `Artifact`, and the chart/view
models `Page`/`ChartType`). Most have a `New*` variant for creation payloads versus the
persisted/read model. Dash component-id constants that are genuinely shared across plugins live in
`dlboard/models/constants.py` — a plugin-local id has no business there.
