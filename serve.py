"""Run the dltrack server."""

import logging

import structlog
from pydantic.v1 import BaseSettings

from dltrack.plugins import LOCAL_DEPLOYMENT, themes
from dltrack.serve import app

structlog.configure_once(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

_log = structlog.stdlib.get_logger(__name__)


class AppSettings(BaseSettings):
    """Set debug on or off."""

    debug_mode: bool = False
    """Enable or disable debug mode."""


if __name__ == "__main__":
    _log.info("dltrack server is starting...")
    env = AppSettings()
    app(
        [
            *LOCAL_DEPLOYMENT,
            themes.dark,
        ]
    ).run(debug=env.debug_mode)  # pyright: ignore[reportUnknownMemberType]
