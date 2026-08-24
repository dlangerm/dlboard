"""Core components for the dltrack server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, callback, dcc, html  # pyright: ignore[reportUnknownVariableType]
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dltrack.models import InstalledPlugin, constants
from dltrack.serve._backend._auth import get_auth_provider, get_current_user
from dltrack.serve._backend._data_store import get_data_store
from dltrack.serve._backend._installed_plugins import set_installed_plugins

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack import models


_log = get_logger(__name__)


def app(plugins: list[models.PluginProtocol]) -> Dash:
    """Get the app initialized with a set of plugins."""
    installed = [InstalledPlugin.describe(p) for p in plugins]
    _log.info("DLTrack creating dash app with plugins:")
    for p in installed:
        _log.info(p.name)
    _app = Dash(
        __name__,
        use_pages=True,
        pages_folder="_pages",
        suppress_callback_exceptions=True,
        plugins=plugins,
    )
    # Only the immutable `InstalledPlugin` snapshots are retained on the app -- not the plugin
    # modules/objects themselves, so introspecting this later (the admin page's About tab) can't
    # reach back into a plugin's own state.
    set_installed_plugins(_app, installed)
    basic_container_layout = dmc.AppShell(
        [
            dmc.AppShellHeader(
                dmc.Group(
                    [
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
                            ]
                        ),
                        dmc.Group(
                            [
                                html.Div(id=constants.HEADER_USER_INDICATOR_ID),
                                dcc.Link("Admin", href="/admin", refresh=True),
                            ],
                            gap="md",
                        ),
                    ],
                    justify="space-between",
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
            Store(id=constants.NAVBAR_COLLAPSED_STORE_ID, data=False, storage_type="local"),
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
    if project_id is None:
        return [dcc.Link("Projects", href="/", refresh=True)]

    store = get_data_store()
    project_name = store.get_project(project_id).name
    if experiment_id is None:
        return [
            dcc.Link("Projects", href="/", refresh=True),
            dcc.Link(project_name, href=f"/project/{project_id}", refresh=False),
        ]

    experiment = store.get_experiment(experiment_id)
    experiment_name = experiment.name if experiment and experiment.name else f"Experiment {experiment_id}"
    return [
        dcc.Link("Projects", href="/", refresh=True),
        dcc.Link(project_name, href=f"/project/{project_id}", refresh=False),
        dmc.Text(experiment_name),
    ]


# show who the app currently resolves the caller to be, and which auth mechanism resolved it
@callback(
    Output(constants.HEADER_USER_INDICATOR_ID, component_property="children"),
    Input(constants.LOCATION_ID, component_property="pathname"),
)
def user_indicator(_: str) -> Component:
    """Render the current auth provider and resolved user in the header, refreshed on navigation."""
    store = get_data_store()
    user = get_current_user(store)
    provider_name = get_auth_provider().__class__.__name__
    return dmc.Group(
        [
            dmc.Badge(provider_name, variant="light", color="gray", size="sm"),
            dmc.Text(user.username, size="sm", fw=500),
        ],
        gap="xs",
    )


@callback(
    Output(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
    Input(constants.NAVBAR_COLLAPSE_TOGGLE_ID, "n_clicks"),
    State(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
    prevent_initial_call=True,
)
def toggle_navbar(n_clicks: int, collapsed: bool) -> bool:  # noqa: FBT001
    """Toggle the navbar visibility. Persisted client-side, so this survives a refresh."""
    if not n_clicks:
        raise PreventUpdate
    return not collapsed


@callback(
    Output("appshell", "navbar"),
    Input(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
)
def sync_navbar_collapsed(collapsed: bool | None) -> dict[str, str | int | dict[str, bool]]:  # noqa: FBT001
    """Apply the persisted collapsed state, including on first load (from localStorage)."""
    return {
        "width": 300,
        "breakpoint": "sm",
        "collapsed": {"mobile": bool(collapsed), "desktop": bool(collapsed)},
    }


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
    project = store.get_project(project_id)
    experiments = store.get_experiments(project_id)
    return dmc.Stack(
        [
            dmc.Text(project.name, fw=600, size="sm"),
            dmc.Stack(
                [
                    dmc.NavLink(
                        label=e.name or f"Experiment {e.id}",
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
