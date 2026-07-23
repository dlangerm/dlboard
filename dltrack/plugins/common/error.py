"""Error plugin."""

from dash import hooks
from structlog.stdlib import get_logger

_log = get_logger(__name__)


@hooks.error()
def on_error(err: Exception) -> None:
    """Print errors using structlog."""
    _log.exception("Uncaught exception", exc_info=True)
