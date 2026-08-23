"""Run the dltrack server."""

import logging

import structlog

from dltrack.plugins import LOCAL_DEPLOYMENT
from dltrack.serve import app

structlog.configure_once(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

_log = structlog.stdlib.get_logger(__name__)


if __name__ == "__main__":
    _log.info("dltrack server is starting...")
    app(
        [
            *LOCAL_DEPLOYMENT,
            # themes.dark,
        ]
    ).run(debug=True)  # pyright: ignore[reportUnknownMemberType]
