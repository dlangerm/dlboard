"""Run the dltrack server."""

import logging
from pathlib import Path

import structlog

from dltrack.plugins import sqlite
from dltrack.plugins.pages import (
    simple_admin_page,
    simple_experiment_page,
    simple_homepage,
    simple_project_page,
)
from dltrack.serve import app

structlog.configure_once(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
_log = structlog.stdlib.get_logger(__name__)

if __name__ == "__main__":
    _log.info("dltrack server is starting...")
    app(
        [
            sqlite.get_plugin(Path("/tmp/dltrack/dltrack.sqlite")),
            simple_homepage,
            simple_admin_page,
            simple_project_page,
            simple_experiment_page,
        ]
    ).run(debug=True)  # pyright: ignore[reportUnknownMemberType]
