"""Core components for the dltrack server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash
import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, dcc

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
    basic_container_layout = dmc.AppShell(
        [
            dmc.AppShellHeader(
                dmc.Group(
                    [
                        dmc.Burger(id="burger", size="sm", hiddenFrom="sm", opened=False),
                        dmc.Title("DLTrack", c="blue"),
                    ],
                    h="100%",
                    px="md",
                )
            ),
            dmc.AppShellNavbar(
                id=constants.NAVBAR_ID,
                children=[dcc.Link("Home", href="/")],
                p="md",
            ),
            dmc.AppShellMain(dash.page_container),
        ],
        header={"height": 60},
        padding="md",
        navbar={
            "width": 300,
            "breakpoint": "sm",
            "collapsed": {"mobile": True},
        },
        id="appshell",
    )
    _app.layout = dmc.MantineProvider(id=constants.MANTINE_PROVIDER_ID, children=[basic_container_layout])
    return _app
