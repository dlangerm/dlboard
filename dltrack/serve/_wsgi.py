"""
The WSGI target Granian's worker processes import for a production `dltrack serve` run.

Not part of the public plugin-facing API. Which plugins to build the app from is resolved by
`WSGISettings` from the `DLTRACK_PLUGINS` env var (an import path, e.g.
`"dltrack.plugins:LOCAL_DEPLOYMENT"`) rather than hardcoded here, so this module works the same
whether it's serving `dltrack serve local` or a `dltrack serve custom --plugins ...` deployment
with a third party's plugin list -- see `dltrack.serve.run_production_server`.

Lives inside `dltrack.serve` (alongside `_production_server.py`, the module that spawns Granian
workers pointed at this file) rather than at the top-level `dltrack` package, so the production-
serving pieces stay in one place instead of `_production_server.py` reaching down into a sibling
top-level module for its own worker entrypoint.
"""

from __future__ import annotations

from dltrack.serve import app as build_app
from dltrack.serve._logging import configure_logging
from dltrack.serve._production_server import WSGISettings

# Granian can start its workers with the `spawn` method (always on Python 3.14+, see its own
# `spawn-ctx-methods`), which re-imports this module from scratch in each one with none of the
# parent process's structlog setup -- so this has to run again here, not just once in `_cli.py`.
configure_logging()

_settings = WSGISettings()  # pyright: ignore[reportCallIssue]
app = build_app(_settings.dltrack_plugins, url_prefix=_settings.dltrack_url_prefix).server
