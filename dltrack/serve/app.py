"""Core components for the dltrack server."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import dash
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, callback, dcc, html  # pyright: ignore[reportUnknownVariableType]
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dltrack.models import constants
from dltrack.serve._backend._data_store import get_data_store

if TYPE_CHECKING:
    from dltrack import models


_log = get_logger(__name__)


def app(plugins: list[models.PluginProtocol]) -> Dash:
    """Get the app initialized with a set of plugins."""
    _log.info("DLTrack creating dash app with plugins:")
    for p in plugins:
        mod = inspect.getmodule(p)
        _log.info(mod.__name__ if mod else repr(p))
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
                        dmc.ActionIcon(
                            "☰",
                            id=constants.NAVBAR_COLLAPSE_TOGGLE_ID,
                            variant="subtle",
                            visibleFrom="sm",
                        ),
                        dmc.Title("DLTrack"),
                        dmc.Breadcrumbs(id=constants.PAGE_BREADCRUMB_ID, separator="/", children=[]),
                    ],
                    h="100%",
                    px="md",
                )
            ),
            dmc.AppShellNavbar(
                id=constants.NAVBAR_ID,
                p="md",
                children=[html.Div(id=constants.NAVBAR_CONTENT_ID)],
            ),
            dmc.AppShellMain(dash.page_container),
            dcc.Location(id=constants.LOCATION_ID, refresh=False),
            Store(id=constants.NAVBAR_COLLAPSED_STORE_ID, data=False),
        ],
        header={"height": 60},
        padding="md",
        navbar={"width": 300, "breakpoint": "sm", "collapsed": {"mobile": True}},
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
def breadcrumbs(_: str, project_id: int | None, experiment_id: int | None) -> list[dcc.Link | dmc.Text]:
    """Breadcrumbs for the project list."""
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


@callback(
    Output("appshell", "navbar"),
    Output(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
    Input(constants.NAVBAR_COLLAPSE_TOGGLE_ID, "n_clicks"),
    State(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
    prevent_initial_call=True,
)
def toggle_navbar(n_clicks: int, collapsed: bool) -> tuple[dict[str, str | int | dict[str, bool]], bool]:  # noqa:  FBT001
    """Toggle the navbar visibility."""
    if not n_clicks:
        raise PreventUpdate
    new_collapsed = not collapsed
    return (
        {
            "width": 300,
            "breakpoint": "sm",
            "collapsed": {"mobile": new_collapsed, "desktop": new_collapsed},
        },
        new_collapsed,
    )


@callback(
    Output(constants.NAVBAR_CONTENT_ID, "children"),
    Input(constants.STATE_PROJECT_ID, component_property="data", allow_optional=True),
    Input(constants.STATE_EXPERIMENT_ID, component_property="data", allow_optional=True),
)
def render_navbar(project_id: int | None, experiment_id: int | None) -> dmc.Stack | dmc.Text:
    """Render the navbar for the app."""
    if project_id is None:
        return dmc.Text("Open a project to see its experiments", c="dimmed", size="sm")

    store = get_data_store()
    experiments = store.get_experiments(project_id)
    return dmc.Stack(
        [
            dmc.Text(f"Project {project_id}", fw=600, size="sm"),
            dmc.Stack(
                [
                    dmc.NavLink(
                        label=f"Experiment {e.id}",
                        href=f"/experiment/{e.id}",
                        active=e.id == experiment_id,
                    )
                    for e in experiments
                ],
                gap=2,
            ),
        ],
        gap="xs",
    )
