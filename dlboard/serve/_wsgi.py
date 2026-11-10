"""
The WSGI target Granian's worker processes import for a production `dlboard serve` run.

Not part of the public plugin-facing API. Which plugins to build the app from is resolved by
`WSGISettings` from the `DLBOARD_PLUGINS` env var (an import path, e.g.
`"dlboard.plugins:LOCAL_DEPLOYMENT"`) rather than hardcoded here, so this module works the same
whether it's serving `dlboard serve local` or a `dlboard serve custom --plugins ...` deployment
with a third party's plugin list -- see `dlboard.serve.run_production_server`.

Lives inside `dlboard.serve` (alongside `_production_server.py`, the module that spawns Granian
workers pointed at this file) rather than at the top-level `dlboard` package, so the production-
serving pieces stay in one place instead of `_production_server.py` reaching down into a sibling
top-level module for its own worker entrypoint.
"""

from __future__ import annotations

from dlboard.serve import app as build_app
from dlboard.serve._custodian._process import die_with_parent
from dlboard.serve._logging import configure_logging
from dlboard.serve._production_server import WORKERS, WSGISettings

# Granian can start its workers with the `spawn` method (always on Python 3.14+, see its own
# `spawn-ctx-methods`), which re-imports this module from scratch in each one with none of the
# parent process's structlog setup -- so this has to run again here, not just once in `_cli.py`.
configure_logging()
# A worker goes when the master does, as the custodian does: killed outright, the master can't stop it, and an orphaned
# worker would go on serving the port and holding the database (#584). Granian starts every worker from its main thread,
# which lives as long as the master, so this fires only when the master is gone. Linux only (`PR_SET_PDEATHSIG`): a
# deployment runs there; elsewhere a `kill -9` of the master still leaves its workers to be stopped by hand.
die_with_parent()

_settings = WSGISettings()
try:
    app = build_app(_settings.dlboard_plugins, url_prefix=_settings.dlboard_url_prefix).server
except Exception:  # noqa: BLE001 -- see below: only the exit code matters here
    # `build_app` has already logged why, as one structured error. Exiting here (still non-zero, so Granian counts
    # it a crashed worker) keeps the interpreter from printing the same traceback again, raw, on stderr.
    raise SystemExit(1) from None
WORKERS.add(1)
