"""Run the dltrack server."""

import logging
from pathlib import Path

import structlog
from pydantic_settings import BaseSettings

from dltrack.plugins import metric_chart, sqlite, themes
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


class AppSettings(BaseSettings):
    """Environment variables."""

    sqlite_location: Path = Path.home() / ".dltrack.sqlite"


if __name__ == "__main__":
    _log.info("dltrack server is starting...")
    settings = AppSettings()
    app(
        [
            sqlite.get_plugin(settings.sqlite_location),
            simple_homepage,
            simple_admin_page,
            simple_project_page,
            simple_experiment_page,
            metric_chart,
            themes.DarkTheme,
        ]
    ).run(debug=True)  # pyright: ignore[reportUnknownMemberType]
