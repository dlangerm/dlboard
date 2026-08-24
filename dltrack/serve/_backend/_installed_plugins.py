"""A plugin using dash hooks that exposes the installed plugin snapshot to callbacks via callback context."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Final, cast

from dash import Dash, get_app
from structlog.stdlib import get_logger

_log = get_logger(__name__)

if TYPE_CHECKING:
    from dltrack.models import InstalledPlugin

_DLTRACK_INSTALLED_PLUGINS: Final = "_dltrack_installed_plugins_"


def set_installed_plugins(app: Dash, plugins: list[InstalledPlugin]) -> None:
    """Set the installed-plugin snapshot for a dash app. Called once, from `dltrack.serve.app.app`."""
    if hasattr(app, _DLTRACK_INSTALLED_PLUGINS):
        msg = "Refusing to overwrite the already-set installed plugin list."
        raise AttributeError(msg)
    setattr(app, _DLTRACK_INSTALLED_PLUGINS, plugins)


@cache
def get_installed_plugins() -> list[InstalledPlugin]:
    """Strongly typed helper function for callbacks who need the installed plugin snapshot."""
    app = cast("Dash", get_app())
    if not hasattr(app, _DLTRACK_INSTALLED_PLUGINS):
        msg = "Installed plugins were not set for the app"
        raise AttributeError(msg)
    return cast("list[InstalledPlugin]", getattr(app, _DLTRACK_INSTALLED_PLUGINS))
