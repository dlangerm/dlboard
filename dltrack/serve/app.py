"""Core components for the dltrack server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash
import dash_mantine_components as dmc
from dash import Dash, dcc, html

from dltrack.models import constants

if TYPE_CHECKING:
    from dltrack import models


def app(plugins: list[models.PluginProtocol]) -> Dash:
    """Get the app initialized with a set of plugins."""
    _app = Dash(
        __name__,
        use_pages=True,
        pages_folder="_pages",
        suppress_callback_exceptions=True,
        plugins=plugins,
    )
    _app.layout = dmc.MantineProvider(id=constants.MANTINE_PROVIDER_ID, children=[dash.page_container])
    return _app
