"""Error plugin."""

from dash import Dash, hooks
from structlog.stdlib import get_logger

_log = get_logger(__name__)


def plug(app: Dash) -> None:  # noqa: ARG001
    """Custom error logging."""

    @hooks.error()
    def on_error(err: Exception) -> None:  # noqa: ARG001
        """Print errors using structlog."""
        _log.exception("Uncaught exception", exc_info=True)
