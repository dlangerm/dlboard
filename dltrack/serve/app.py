"""Core components for the dltrack server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash
import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, Input, Output, callback, dcc

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
                        dmc.Title("DLTrack"),
                        dmc.Breadcrumbs(
                            id=constants.PAGE_BREADCRUMB_ID,
                            separator="/",
                            children=[],
                        ),
                    ],
                    h="100%",
                    px="md",
                )
            ),
            dmc.AppShellNavbar(id=constants.NAVBAR_ID, p="md", children=[]),
            dmc.AppShellMain(dash.page_container),
            dcc.Location(id=constants.LOCATION_ID, refresh=False),
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


# show breadcrumbs to get back to the project list
@callback(
    Output(constants.PAGE_BREADCRUMB_ID, component_property="children"),
    Input(constants.LOCATION_ID, component_property="pathname"),
    Input(constants.STATE_PROJECT_ID, component_property="data", allow_optional=True),
    Input(constants.STATE_EXPERIMENT_ID, component_property="data", allow_optional=True),
)
def _breadcrumbs(_: str, project_id: int | None, experiment_id: int | None) -> list[dcc.Link | dmc.Text]:
    if project_id is None and experiment_id is None:
        return [dcc.Link("Projects", href="/", refresh=True)]
    if project_id is not None and experiment_id is None:
        return [
            dcc.Link("Projects", href="/", refresh=True),
            dcc.Link(f"Project {project_id}", href=f"/project/{project_id}", refresh=False),
        ]
    return [
        dcc.Link("Projects", href="/", refresh=True),
        dcc.Link(f"Project {project_id}", href=f"/project/{project_id}", refresh=False),
        dmc.Text(f"Experiment {experiment_id}"),
    ]
