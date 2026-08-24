# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DLTrack — an experiment-tracking server ("what if mlflow didn't suck and weights & biases was free?"). It's a
Dash/Dash-Mantine web app for browsing ML training runs, plus a `pytorch_lightning`-compatible logger client
that ships metrics/hyperparams/artifacts to it over a REST API.

## Commands

Run everything through `uv` (Python >=3.12, deps pinned in `uv.lock`).
Never run raw python commands, if a python command doesn't work through `uv` ask for further instructions.
Never use python to edit files, just use your normal mechanisms to do so.

```bash
uv run --env-file .env serve.py     # start the dltrack server (this is how the user runs the app)
uv run pytest                       # run the full test suite
uv run pytest tests/sql_store_test.py  # run a single test file
uv run pytest tests/sql_store_test.py::test_name  # run a single test
uv run pytest --cov                 # run with coverage (see pyproject.toml for config)
uv run ruff check                   # lint
uv run ruff format                  # format
uv run pyright                      # type check (strict mode)
uv run prek run --all-files         # run pre-commit hooks manually
```

Prefix the one-shot commands above (`pytest`, `ruff`, `pyright`, `prek`) with `rtk` per the RTK instructions
below for compact output. Do **not** prefix `serve.py` — it's a long-running server, and RTK's filters are
built for commands that produce output and exit, not for something you need to tail live.

Ruff lint config lives in `pyproject.toml`; `tests/*` and `dltype/tests/*` get relaxed rules (docstrings,
private-member access, etc). Pyright runs in `strict` mode over `dltrack/` and `tests/`.

## Architecture


### Plugin system

The whole app — storage, pages, charts, artifact types, themes — is composed at startup as a flat list of
plugins passed into `dltrack.serve.app.app(plugins)`. Each plugin is any module implementing
`PluginProtocol` (`dltrack/models/_plugin.py`): a `plug(cls, app: Dash) -> None` classmethod that registers
itself with the Dash app (adds routes/pages, sets the data store, registers a chart renderer, etc).

`dltrack/plugins/__init__.py` defines the bundles used for a local deployment:
- `LOCAL_STORAGE` = `[sqlite, filesystem]` — the metadata DB and artifact blob storage
- `BUILTIN_PAGES` = homepage, project page, experiment page
- `BUILTIN_CHARTS` = image_series, line_chart, table_chart
- `LOCAL_DEPLOYMENT` = all of the above, used by `serve.py`

New functionality (a new chart type, storage backend, page, or artifact kind) is added by writing a new
plugin module and including it in the list passed to `app()`, not by editing the core app.
The core app should expose minimal `Store` hooks for plugins to use, but any opinionated design choices should rest with plugins.

### Data flow: client → server

1. `dltrack.client.dltrack_logger.DLTrackLogger` is a `pytorch_lightning.loggers.Logger`. On init it creates
   an experiment/run via `BasicDltrackAPI` (`dltrack/plugins/backend/basic_rest_backend.py`) and spawns two
   daemon subprocesses (`process_metrics_async`, `process_artifacts_async`) that batch and flush metrics /
   artifacts from multiprocessing queues to the REST backend on an interval, so training isn't blocked on
   network I/O.
2. `dltrack/serve/_backend/basic_rest_backend` (server side) exposes the REST API that ingests these batches.
3. Server-side storage is split into a `DataStore` (structured metadata: projects, experiments, runs, metrics,
   hparams) and an `ArtifactStore` (blob storage), both defined as generic protocols in
   `dltrack/models/_data_store.py`. `SQLStoreBase` (`dltrack/serve/_backend/_sql_store_base.py`) implements
   the `DataStore` contract against raw SQL; `plugins/data_stores/sqlite.py` supplies the sqlite
   `_execute_raw_sql` implementation, `filesystem.py` supplies artifact blob storage on disk.

### Server-side app (Dash)

`dltrack/serve/app.py` builds a Mantine `AppShell` (header, collapsible navbar, page container) and wires
global callbacks: breadcrumbs, navbar collapse/expand (persisted to `localStorage` via `dcc.Store`), and
navbar content (lists a project's experiments). Actual routed pages live under `dltrack/serve/_pages/` and
`dltrack/plugins/pages/` and use Dash's `use_pages` file-based routing.

Callbacks that need storage reach it via `dltrack/serve/_backend/_data_store.py`: `get_data_store()` /
`get_artifact_store()` are `@cache`d accessors that pull the store off the running `Dash` app instance (set
once at startup via `set_data_store`/`set_artifact_store`, called by the storage plugins' `plug()`).
Constants for Dash component IDs live in `dltrack/models/constants.py`.
Constants should only be added for globally-accessed values, not for per-plugin items that won't be used in other contexts.

### Models

`dltrack/models/` holds the Pydantic data models shared between client and server (`Experiment`, `Run`,
`Project`, `HyperParams`, `LoggedMetrics`, `Artifact`, chart/view models like `Page`/`ChartType`). Most have a
`New*` variant (e.g. `NewExperiment`, `NewRun`) for creation payloads versus the persisted/read model.

### Charts

Charts are plugins under `dltrack/plugins/charts/`. Chart rendering plugins register how a `Page`/`ChartType`
gets turned into a Dash component; `_chart_autogen.py` under `plugins/pages/` auto-generates default charts
for an experiment's logged metrics.

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

Whenever you finish an instruction, make sure to at least run ruff `rtk uv run ruff check` and `rtk uv run ruff format` as well as `rtk uv run pyright` to ensure code quality is maintained before review.

Always rely on pydantic validation instead of performing your own, use `pendulum` instead of `datetime` and use `pydantic_settings` for environment variables.

## RTK (Rust Token Killer) - Token-Optimized Commands

<!-- rtk-instructions v2 -->
## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

**Wrapper commands** (`rtk test <cmd>`, `rtk err <cmd>`, `rtk summary <cmd>`, `rtk proxy <cmd>`) take the raw,
un-prefixed inner command — `rtk` is the wrapper, not a prefix to repeat inside it:
```bash
# ❌ Wrong — double-wrapped
rtk err rtk cargo build

# ✅ Correct
rtk err cargo build
```

## RTK Commands by Workflow

### Test (90% savings)
```bash
rtk pytest              # Python test failures only (90%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh api              # Compact API responses (26%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%). With format flags (-c, -l, -L, -o, -Z),
                         # still fine to type `rtk grep`, but it runs unfiltered — those flags
                         # already produce compact output, so there's nothing for rtk to strip.
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | pytest | 90% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh api | 26-87% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Savings vary by command, roughly 26-99% depending on how compressible the underlying output is; most
everyday commands (tests, git, file search) land in the 60-90% range.
<!-- /rtk-instructions -->
