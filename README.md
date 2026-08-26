# dltrack

An experiment-tracking server — what if MLflow didn't suck and Weights & Biases was free? dltrack
is a Dash/Dash-Mantine web app for browsing ML training runs, plus a `pytorch_lightning`-compatible
logger client that ships metrics, hyperparameters, and artifacts to it over a REST API.

## Running it

Everything runs through [`uv`](https://docs.astral.sh/uv/) (Python >=3.12, deps pinned in `uv.lock`).

```bash
uv run --env-file .env serve.py     # start the server
uv run pytest                       # run the test suite
uv run pytest -m browser            # run only the browser/e2e tests
uv run ruff check && uv run ruff format   # lint / format
uv run pyright                      # type check
uv run prek run --all-files         # run pre-commit hooks manually
```

To log a training run against a running server, point `dltrack.client.DLTrackLogger` at it the
way you'd use any other `pytorch_lightning` logger — see [docs/client.md](docs/client.md).

## Docs

- [Architecture](docs/architecture.md) — how the app is put together: the plugin system, and the
  request flow from a training script to the browser.
- [Client & logging](docs/client.md) — `DLTrackLogger`, the REST client, and how to log a new kind
  of artifact.
- **Plugins** — dltrack has no built-in opinions about storage, auth, charts, pages, or theming;
  all of it is plugins. Start with the [overview](docs/plugins/overview.md), then the category you
  need:
  - [Storage](docs/plugins/storage.md) — where projects/runs/metrics and artifact blobs live
  - [Charts](docs/plugins/charts.md) — how a metric/artifact gets turned into a rendered chart
  - [Pages](docs/plugins/pages.md) — routed Dash pages (homepage, project, experiment, admin, ...)
  - [Auth](docs/plugins/auth.md) — who a request is attributed to, and gating access
  - [Themes](docs/plugins/themes.md) — the Mantine theme the app renders with

## Contributing

Pull requests run lint, type-check, and test CI (see `.github/workflows/ci.yml`) — run the
commands above locally before pushing. See `CLAUDE.md` for the fuller set of code-style
conventions this repo follows.
