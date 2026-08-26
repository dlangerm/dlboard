# dltrack

A free, self-hosted experiment-tracking server. dltrack is a Dash/Dash-Mantine web app for browsing
ML training runs, plus a `pytorch_lightning`-compatible logger client that ships metrics,
hyperparameters, and artifacts to it over a REST API (by default).

## Running it

Installed as a package (e.g. `uv tool install dltrack` or `pip install dltrack`), dltrack gives
you a `dltrack` command, the same idea as `tensorboard`:

```bash
dltrack serve local     # anonymous, single-user, sqlite + local disk -- sane defaults, no setup
dltrack serve local --sqlite-location ./runs.sqlite --artifact-store-location ./artifacts
dltrack serve local --host 0.0.0.0 --port 8050 --workers 4   # behind a reverse proxy / in a container
```

Outside of `--debug`, this runs on [Granian](https://github.com/emmett-framework/granian) (Rust,
multi-worker, auto-respawns a crashed worker) instead of Dash's own development server.

Bringing your own storage/auth/pages/chart plugins instead of `local`'s built-in set? `dltrack serve
custom` runs the same production server against any `list[PluginProtocol]` you point it at:

```bash
dltrack serve custom --plugins mypackage.deployment:PLUGINS --workers 4
```

`mypackage/deployment.py` just needs a module-level `PLUGINS: list[PluginProtocol]` -- see
[docs/plugins/overview.md](docs/plugins/overview.md).

Working in this repo instead, everything runs through [`uv`](https://docs.astral.sh/uv/)
(Python >=3.12, deps pinned in `uv.lock`):

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

## AI Usage

I started dltrack as a personal project coded by yours truly. As I went down the rabbit hole of implementation I realized
I was spending a _ton_ of time twiddling with webdev instead of actually doing useful work. At some level, experiment
tracker libraries live and die by their browser experience and their offered feature set. I set out to make a free, open-source
tool that I actually wanted to use for my own projects. Making something I actually wanted to use became a behemoth of
wiring up dash app components in ways that were performant and made sense. So, I decided that it'd be better to get an app
out the door into hands of users than it would be to do everything by hand.

So, is the app coded in a way I personally would have written it? Absolutely not. Does it work and provide value today instead of 6 months
down the line? Yeah, it does. However, I remain a healthy skeptic of AI generated code. Therefore, I will only accept PRs (for now) that I myself
can review and understand. That means contributors to this library need to tools to a high standard and follow best practices for submitting
PRs and features just like you would if you had coded it yourself.

tl;dr
Can I submit AI vibe-coded-PRs for plugins I want that I think are useful to the community? Yes you can.
Will I give serious reviews and (if they're too big) ask you to split features up into a stack up so I can review them properly? Also yes.
