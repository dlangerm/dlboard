"""
The WSGI target Granian's worker processes import for a production `dltrack serve` run.

Not part of the public plugin-facing API. Which plugins to build the app from is resolved by
`WSGISettings` from the `DLTRACK_PLUGINS` env var (an import path, e.g.
`"dltrack.plugins:LOCAL_DEPLOYMENT"`) rather than hardcoded here, so this module works the same
whether it's serving `dltrack serve local` or a `dltrack serve custom --plugins ...` deployment
with a third party's plugin list -- see `dltrack.serve.run_production_server`.
"""

from __future__ import annotations

from dltrack.serve import app as build_app
from dltrack.serve._production_server import WSGISettings

app = build_app(WSGISettings().dltrack_plugins).server  # pyright: ignore[reportCallIssue]
