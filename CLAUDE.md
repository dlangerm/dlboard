# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DLBoard — a free, self-hosted ML experiment-tracking server. It's a Dash/Dash-Mantine web app for browsing ML
training runs, plus a `pytorch_lightning`-compatible logger client that ships metrics/hyperparams/artifacts
to it over a REST API. Published as two PyPI distributions from one source tree -- `dlboard-client` (the
client; Python >=3.10) and `dlboard` (the Dash app; Python >=3.12) -- see "Packaging" under
Architecture.

## Commands

Run everything through `uv`, from the repo root (deps pinned in `uv.lock`). This repo's own `pyproject.toml`
is a virtual uv workspace root with no package of its own -- `uv sync`/`uv run` default to every member
(`client/`, `server/`) without needing `--all-packages`, so the commands below need no extra flags.
Never run raw python commands, if a python command doesn't work through `uv` ask for further instructions.
Never use python to edit files, just use your normal mechanisms to do so.

```bash
uv run --env-file .env dlboard serve local     # start the dlboard server (this is how the user runs the app)
uv run pytest                       # run the full test suite
uv run pytest dlboard/serve/_backend/tests/sql_store_test.py  # run a single test file
uv run pytest dlboard/serve/_backend/tests/sql_store_test.py::test_name  # run a single test
uv run pytest --cov                 # run with coverage (see pyproject.toml for config)
uv run pytest -m browser            # run only the browser/e2e tests
uv run pytest -m "not browser"      # run everything except the browser/e2e tests
uv run pytest -m postgres           # run the store suites against a real Postgres (needs Docker; testcontainers)
uv run pytest -m s3                 # run the S3 artifact store against a real S3 endpoint (needs Docker; testcontainers)
uv run pytest -m "not browser and not postgres and not s3"  # everything that needs neither a browser nor Docker
uv run pytest --screenshots=check   # re-render the docs screenshots and fail if docs/images is stale
uv run pytest --screenshots=update  # rewrite docs/images (local preview; CI's render is the one to commit)
uv run ruff check                   # lint
uv run ruff format                  # format
uv run pyright                      # type check (strict mode)
uv run prek run --all-files         # run pre-commit hooks manually
```

Tests live next to the code they test, not in one top-level directory: a `tests/` subfolder sits beside
every source directory that has tests (e.g. `dlboard/plugins/charts/tests/line_chart_test.py` next to
`dlboard/plugins/charts/line_chart.py`). A test that exercises multiple files across directories (an
integration/end-to-end test, not a unit test that merely needs some object as fixture data) lives in the
`tests/` dir of those files' lowest common parent directory instead — e.g. a test spanning `serve/_pages/`
and `plugins/charts/` belongs in `dlboard/tests/` (see `chart_render_isolation_test.py`). Shared fixtures
(`store`, `experiment_id`, the `props`/`find_props` Dash-component helpers) live in `dlboard/conftest.py`
at the package root, which pytest's conftest discovery makes visible to every nested `tests/` dir
automatically.

`dlboard/serve/_pages/` is the one place this needs a tweak: Dash's `pages_folder` scanner walks it
looking for `register_page`-calling modules, and prunes any `_`-prefixed file or directory from that
walk (see "Page layout" below). A plain `_pages/tests/` would *not* be pruned, so page-implementation
tests live in `_pages/_tests/` instead — underscore-prefixed for the same reason every other
implementation module under `_pages/` is. `_pages/_experiment/`'s own tests need no such adjustment;
`_experiment/` itself is already underscore-prefixed, so `_pages/_experiment/tests/` is pruned before
the scanner ever looks inside it.

Ruff lint config lives in `pyproject.toml`; `**/tests/*` gets relaxed rules (docstrings, private-member
access, etc). Pyright runs in `strict` mode over `dlboard/` (tests included, since they now live under it).

A plugin that needs its own browser-side JS or CSS (a clientside callback helper, a functions-as-props
formatter for dash-mantine-components) keeps that file next to its own module and serves/registers it
itself from `plug()` — via `dlboard.serve.serve_asset(app, AssetKind.SCRIPT, path.name, path.read_bytes())`,
which serves it from that app only, under a content-hashed URL the browser caches forever. Never drop it in a shared `assets/` folder or use the global
`dash.hooks.route`/`hooks.script` registry: both leak across every `Dash` app built in the same process, not
just the one that owns the file, and a shared assets folder would tie a plugin's JS to the main app package
instead of the plugin. Every such `dlboard/**/*.js` file is linted/formatted by Biome (`biome.json`), wired
into `prek`/pre-commit via the `biome-check` hook. It runs in an isolated Node environment pre-commit
provisions itself, so no system Node/npm is required. These files target whatever JS the installed
Dash/browser runtime supports, not a specific Node version — keep that in mind before accepting an
autofix/suggestion that assumes newer syntax.

## Architecture

### Packaging: two distributions, one source tree

Everything still lives under one `dlboard/` directory, as one git repo -- but it's published as
two separate PyPI distributions, built from different subsets of that same tree:

- **`client/`** -- the `dlboard-client` distribution: `dlboard/__init__.py`, `dlboard/_identity.py`,
  `dlboard/_batching.py`, `dlboard/_mp_context.py`, `dlboard/_wire.py`, `dlboard/client/`, and
  `dlboard/models/`. Python >=3.10, and intentionally light -- pydantic, numpy, pendulum, requests,
  and the like. `pytorch_lightning` (needed only by `dlboard.client.dlboard_logger`) is the
  `[lightning]` extra, not a base dependency, so a client install never needs torch unless it
  actually wants the Lightning adapter.
- **`server/`** -- the `dlboard` distribution: `dlboard/serve/`, `dlboard/plugins/` (every
  plugin *except* `dlboard/client/artifacts/`, which is client-owned), `dlboard/_cli.py`, and
  `dlboard/scripts/`. Python >=3.12, depends on `dlboard-client` for the shared models, and is where
  dash/granian/sqlalchemy/pandas actually live -- a client install never needs any of them.

Each side's `pyproject.toml` lives in its own directory (`client/pyproject.toml`,
`server/pyproject.toml`) and pulls its files from `../dlboard/...` via a `hatch_build.py` build
hook (plain `[tool.hatch.build] include`/`exclude` can't reach outside its own project directory,
and `force-include` -- the one mechanism that can -- ignores `exclude` entirely, so each hook
walks its own subset of the tree and builds the `force_include` mapping itself, skipping every
`tests`/`_tests` directory on the way). Neither side ships `dlboard/__init__.py` *and* claims to
own the whole package: only `client/`'s wheel has it, so `dlboard`'s wheel is an implicit
namespace package (PEP 420) that layers its `dlboard/serve`, `dlboard/plugins`, etc. on top of the
client's `dlboard/` when both are installed together -- verified by building both wheels and
installing them into a clean venv, not just by inspecting the config.

This repo's own root `pyproject.toml` is a **virtual workspace root**: no `[project]` table of its
own, just `[tool.uv.workspace] members = ["client", "server"]` plus the shared dev tooling config
(`[tool.ruff]`, `[tool.pyright]`, `[tool.pytest.ini_options]`, `[dependency-groups] dev`, ...). A
virtual root is what makes bare `uv run`/`uv sync` default to every workspace member instead of
just one -- give it a real `[project]` table and those commands go back to defaulting to that one
package alone.

A change to the client/server boundary itself (moving a file between `dlboard/client/` and
`dlboard/serve/`, say) means updating whichever `hatch_build.py`'s include/exclude logic notices
it -- both are driven by path, not an explicit file list, so most changes within an existing
top-level directory need no change there at all.

### Plugin system

Genuinely swappable functionality — storage, auth, chart types, themes, backend routes — is
composed at startup as a flat list of plugins passed into `dlboard.serve.app.app(plugins)`. Each plugin is
any module implementing `PluginProtocol` (`dlboard/models/_plugin.py`): a `plug(cls, app: Dash) -> None`
classmethod that registers itself with the Dash app (adds routes, sets the data store, registers a chart
renderer, etc).

A logged artifact's *kind* (`dlboard/client/artifacts/`: `Image`, `Link`) isn't part of this list --
there's nothing server-side to register. Each is a plain client-side value type structurally matching
`AnyArtifact` (`dlboard/models/_artifact.py`): the client builds one and calls its own `to_artifact()`
to turn it into bytes/a ref the generic upload path sends, and the server never imports the concrete
class at all. Lives under `client/`, not `plugins/`, so a client install never needs the server stack
just to construct one.

`dlboard/plugins/__init__.py` defines the bundles used for a local deployment:
- `LOCAL_STORAGE` = `[sqlite, filesystem]` — the metadata DB and artifact blob storage
- `BUILTIN_CHARTS` = image_series, line_chart, table_chart
- `LOCAL_DEPLOYMENT` = all of the above, used by `dlboard serve local`
- `POSTGRES_STORAGE` = `[postgres, filesystem, artifact_purge_worker]` — for a shared deployment, composed
  into a `dlboard serve custom --plugins` list; configured by `DLBOARD_POSTGRES_*` env vars (`PostgresSettings`)
- `POSTGRES_S3_STORAGE` = `[postgres, s3, artifact_purge_worker]` — same, but artifacts go to any
  S3-protocol object store (AWS, MinIO, VAST, ...) instead of local disk; configured by `DLBOARD_S3_*` env
  vars (`S3Settings`). `filesystem` and `s3` are both just a `BlobBackend`
  (`plugins/data_stores/_blob_store.py`) plugged into the shared `BlobArtifactStore` -- see
  `docs/plugins/storage.md` for the pattern and `S3Settings`'s full env var list.
- `LOCAL_AUTH` = `[anonymous]` (no sign-in, access control off); `PASSWORD_AUTH` = `[password]` -- swap it
  in for a deployment where people sign in (needs `DLBOARD_SECRET_KEY`; see `docs/plugins/auth.md`)

New functionality (a new chart type, storage backend, or artifact kind) is added by writing a new plugin
module and including it in the list passed to `app()`, not by editing the core app. The core app should
expose minimal `Store` hooks for these plugins to use, but any opinionated design choices should rest with
them.

Page layout is deliberately **not** part of this plugin surface. The built-in pages (home, project, admin,
experiment, account) are opinionated, non-optional dlboard behavior — nobody swaps out the experiment page's
tab+accordion layout the way they might swap sqlite for postgres. They live under `dlboard/serve/_pages/`
and are wired unconditionally by `dlboard.serve.app.app()` itself, never listed in
`dlboard/plugins/__init__.py` and never something a `dlboard serve custom --plugins ...` deployment can
omit or override. Only chart *types* remain plugin-owned within a page — a chart's actual rendering is
always reached polymorphically through `ChartTypeRegistry` (`dlboard/models/_view.py`) via
`ChartInstance.render()`, never a direct import of a specific chart plugin's render function (auto-suggestion
logic is a narrow exception: it references concrete chart classes to decide what to suggest, not to render).

### Data flow: client → server

1. `dlboard.client.dlboard_logger.DLBoardLogger` is a `pytorch_lightning.loggers.Logger`. On init it creates
   an experiment/run via `BasicDlboardAPI` (`dlboard/plugins/backend/basic_rest_backend.py`) and spawns two
   daemon subprocesses (`process_metrics_async`, `process_artifacts_async`) that batch and flush metrics /
   artifacts from multiprocessing queues to the REST backend on an interval, so training isn't blocked on
   network I/O.
2. `dlboard/serve/_backend/basic_rest_backend` (server side) exposes the REST API that ingests these batches.
3. Server-side storage is split into a `DataStore` (structured metadata: projects, experiments, runs, metrics,
   hparams) and an `ArtifactStore` (blob storage), both defined as generic protocols in
   `dlboard/models/_data_store.py`. `SQLStoreBase` (`dlboard/serve/_backend/_sql_store_base.py`) implements
   the `DataStore` contract on SQLAlchemy Core (not the ORM): `_sql.py` generates each table from its
   pydantic model, and every query is a Core expression, so SQLAlchemy owns everything dialect-specific
   (types, identity columns, quoting, bind style). A concrete store only hands `SQLStoreBase` an `Engine`
   and implements `_insert_ignoring_conflicts`; `plugins/data_stores/sqlite.py` is the reference one, and
   `filesystem.py` supplies artifact blob storage on disk. Schema changes go through Alembic
   (`dlboard/serve/_backend/migrations/`): `build_metadata()` in `_sql_store_base.py` is the one place
   the full schema is assembled from the pydantic models, used both to build a concrete store's live
   tables and, with `schema=None`, as `migrations/env.py`'s `target_metadata` for autogenerating future
   revisions. Every store runs `alembic upgrade head` at construction (`_schema_upgrade.run_migrations`),
   in the same transaction as any Postgres-only schema-creation step (`_prepare_schema`). Released versions
   exist, so migrations follow expand/contract (`docs/compatibility.md`): additive and data-preserving,
   a destructive step only lands one minor release after the code stopped using it, and a released
   migration is never edited.

### Server-side app (Dash)

`dlboard/serve/app.py` builds a Mantine `AppShell` (header, collapsible navbar, page container) and wires
global callbacks: breadcrumbs, navbar collapse/expand (persisted to `localStorage` via `dcc.Store`), and
navbar content (lists a project's experiments). Actual routed pages live under `dlboard/serve/_pages/` and
use Dash's `use_pages` file-based routing, split into two halves per page: a flat, non-underscore-prefixed
file (`home.py`, `project.py`, `admin.py`, `experiment.py`, `account.py`) that Dash's `pages_folder` scanner picks up —
it calls `dash.register_page(...)` and defines `layout()` — plus an underscore-prefixed sibling (or, for
experiment, a whole underscore-prefixed package: `_experiment/`) holding the actual implementation
(callbacks, a `register(app)` entry point, everything else). The underscore prefix isn't cosmetic: Dash's
scanner prunes any `_`-prefixed file or directory from its walk, so that's what keeps the implementation
invisible to it while still being an ordinary Python import for `app.py` and the routed file to use. Unlike
a real plugin's `plug(app)`, these `register(app)` functions aren't called through Dash's own `plugins=`
constructor kwarg — `app.py` imports the page modules directly and calls `register(_app)` on each
itself, right after `Dash(...)` construction, since they aren't `PluginProtocol` and were never meant to be
swappable.

Callbacks that need storage reach it via `dlboard/serve/_backend/_data_store.py`: `get_data_store()` /
`get_artifact_store()` return per-request authorizing wrappers (`_authorization.py`) around the store a
storage plugin set on the app, bound to the user the request gate (`_auth.py`) authenticated -- every
route, callback included, is behind that gate. Never authorize by hand in a page or handler, and never
reach for `get_system_data_store()` from request code: it bypasses authorization, and is only for work no
user is behind (background workers, the gate itself, the password provider's credential checks). A new
`DataStore` method needs an explicit rule in `AuthorizingDataStore` -- `authorization_test.py` fails
without one.
Constants for Dash component IDs live in `dlboard/serve/_constants.py`.
Constants should only be added for globally-accessed values, not for per-plugin items that won't be used in other contexts.

### Models

`dlboard/models/` holds the Pydantic data models shared between client and server (`Experiment`, `Run`,
`Project`, `HyperParams`, `LoggedMetrics`, `Artifact`, chart/view models like `Page`/`ChartType`). Most have a
`New*` variant (e.g. `NewExperiment`, `NewRun`) for creation payloads versus the persisted/read model.

### Charts

Charts are plugins under `dlboard/plugins/charts/`. Chart rendering plugins register how a `Page`/`ChartType`
gets turned into a Dash component; `_chart_autogen.py` under `serve/_pages/_experiment/` auto-generates
default charts for an experiment's logged metrics.

### Code Style

Always prefer strong-types, enums, literals, classes, protocols, etc. Never use stringly-typed interfaces, they are
brittle and hard to maintain. Rely on static analysis wherever possible, specifically ensuring codepaths can only execute
deterministically (use `match` in place of multiple `if/elif` where appropritate, leave off the default case so the static analyzer can catch failures). Prefer end to end tests that assert behavior, not implementation. Don't make man-in-the-middle functions that only exist
to call other functions, prefer logical breakdowns of functional units that do actual work.

Pytest should always use functional-style tests, never class-based tests. Use parametrized tests instead of multiple test files.
Keep test files short, orthogonal, and specific, the test should never be harder to maintain than the target module.

Always keep in mind this code is meant to be read and maintained by humans, lines of code and complexity really matter. Exploded interfaces
and extra functions that serve only to break up blocks of code but not to separate logic are hard to parse and reason about. Wherever
possible, fold large blocks or repeated logic into compartmentalized units that can easily be reused.

Whenever you finish an instruction, make sure to at least run `uv run ruff check` and `uv run ruff format` as well as `uv run pyright` to ensure code quality is maintained before review.

Always rely on pydantic validation instead of performing your own, use `pendulum` instead of `datetime` and use `pydantic_settings` for environment variables.
