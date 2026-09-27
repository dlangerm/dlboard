"""Core components for the dltrack server."""

from __future__ import annotations

from functools import partial
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, callback, dcc, html  # pyright: ignore[reportUnknownVariableType]
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dltrack.models import InstalledPlugin, constants
from dltrack.serve._assets import AssetKind, serve_asset
from dltrack.serve._backend._auth import get_auth_provider, get_current_user
from dltrack.serve._backend._data_store import get_data_store
from dltrack.serve._backend._installed_plugins import set_installed_plugins
from dltrack.serve._backend._theme import get_theme

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack import models
    from dltrack.models import ThemeSpec


_log = get_logger(__name__)

_ASSETS = [
    # The navbar drag-to-resize handle.
    (AssetKind.SCRIPT, Path(__file__).with_name("navbar_resize.js")),
    (AssetKind.STYLESHEET, Path(__file__).with_name("_shell.css")),
]
_COLOR_SCHEME_PRELOAD_JS = Path(__file__).with_name("color_scheme_preload.js").read_text()
_DEFAULT_NAVBAR_WIDTH = 300
_MIN_NAVBAR_WIDTH = 260
_MAX_NAVBAR_WIDTH = 640
PAGE_LOADING_CLASS = "dl-page-loading"


def app(plugins: list[models.PluginProtocol]) -> Dash:
    """Get the app initialized with a set of plugins."""
    # Imported here, not at module level: chart plugins import `from dltrack.serve import
    # ClientsideScript` at their own module level, so importing these page modules (which reach
    # back into concrete chart classes, e.g. `_chart_autogen.py` imports `ImageChart` directly) at
    # `dltrack.serve` import time can race that still-in-progress import. Deferring to call time
    # sidesteps it, since `app()` only ever runs after process startup import resolution finishes.
    from dltrack.serve._pages import _experiment as _experiment_page
    from dltrack.serve._pages import _simple_admin_page, _simple_homepage, _simple_project_page

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
    # The tab+accordion page layouts (home, project, admin, experiment) are opinionated,
    # non-optional dltrack behavior -- always wired into every app(), never part of a deployment's
    # own `plugins` list, and deliberately excluded from the `installed` snapshot above (which
    # should only reflect what a deployment actually chose to install). These page modules don't
    # implement `PluginProtocol`, so `register(app)` is called directly rather than threaded
    # through Dash's own `plugins=` constructor kwarg.
    for page in (_simple_homepage, _simple_project_page, _experiment_page, _simple_admin_page):
        page.register(_app)
    # Only the immutable `InstalledPlugin` snapshots are retained on the app -- not the plugin
    # modules/objects themselves, so introspecting this later (the admin page's About tab) can't
    # reach back into a plugin's own state.
    set_installed_plugins(_app, installed)
    for kind, path in _ASSETS:
        serve_asset(_app, kind, path.name, path.read_bytes())
    # A theme plugin's `plug()` has already run by now (inside `Dash(...)` above), so its theme
    # goes straight into the first response -- no callback, no flash of an unthemed page.
    theme = get_theme(_app)
    _app.index_string = _preload_color_scheme(_app.index_string, theme)
    _app.layout = partial(_layout, theme)
    return _app


def _preload_color_scheme(index_string: str, theme: ThemeSpec) -> str:
    """Paint the theme's page background before any JS or Mantine CSS has loaded."""
    background = theme.page_background
    preload = (
        f"<style>html[data-mantine-color-scheme=light]{{background-color:{background.light}}}"
        f"html[data-mantine-color-scheme=dark]{{background-color:{background.dark}}}</style>"
        f'<script data-default-scheme="{escape(theme.default_color_scheme)}">{_COLOR_SCHEME_PRELOAD_JS}</script>'
    )
    return index_string.replace("<head>", f"<head>{preload}", 1)


def _layout(theme: ThemeSpec) -> dmc.MantineProvider:
    """The app shell, rendered on every page load so the header can name the current user."""
    shell = dmc.AppShell(
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
                                html.Div(_user_indicator(), id=constants.HEADER_USER_INDICATOR_ID),
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
                children=[
                    html.Div(
                        style={"position": "relative", "height": "100%"},
                        children=[
                            html.Div(id=constants.NAVBAR_CONTENT_ID),
                            html.Div(id=constants.NAVBAR_RUN_LIST_ID),
                            html.Div(
                                id=constants.NAVBAR_RESIZE_HANDLE_ID,
                                style={
                                    "position": "absolute",
                                    "top": 0,
                                    "right": "-1rem",
                                    "bottom": 0,
                                    "width": "6px",
                                    "cursor": "col-resize",
                                },
                            ),
                        ],
                    ),
                ],
            ),
            dmc.AppShellMain(
                # Dash renders a routed page's `layout()` in a callback fired after this shell
                # mounts, so a slow page would otherwise show nothing at all until it returns.
                # Scoped to `_pages_content` (the div Dash fills with each page's layout) so a
                # page's own in-page callbacks never trigger this page-wide spinner. The
                # `target_components` stub is an empty TypedDict (a Dash codegen gap) -- hence the
                # cast, same as `serve/_pages/experiment.py`'s own scoped `dcc.Loading`.
                cast("Any", dcc.Loading)(
                    dash.page_container,
                    target_components={"_pages_content": "children"},
                    delay_show=250,
                    custom_spinner=dmc.Loader(size="lg"),
                    parent_className=PAGE_LOADING_CLASS,
                ),
                className="dl-app-main",
            ),
            dcc.Location(id=constants.LOCATION_ID, refresh=False),
            Store(id=constants.NAVBAR_COLLAPSED_STORE_ID, data=False, storage_type="local"),
            Store(id=constants.NAVBAR_WIDTH_STORE_ID, data=_DEFAULT_NAVBAR_WIDTH, storage_type="local"),
        ],
        header={"height": 60},
        padding="md",
        navbar={"width": _DEFAULT_NAVBAR_WIDTH, "breakpoint": "sm", "collapsed": {"mobile": True}},
        id="appshell",
    )
    return dmc.MantineProvider(
        id=constants.MANTINE_PROVIDER_ID,
        theme=theme.mantine,
        defaultColorScheme=theme.default_color_scheme.value,
        children=[shell],
    )


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


def _user_indicator() -> Component:
    """Who the app resolves the caller to be, and which auth mechanism resolved it."""
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
    Input(constants.NAVBAR_WIDTH_STORE_ID, "data"),
)
def sync_navbar_collapsed(
    collapsed: bool | None,  # noqa: FBT001
    width: int | None,
) -> dict[str, str | int | dict[str, bool]]:
    """Apply the persisted collapsed/width state, including on first load (from localStorage)."""
    return {
        "width": min(_MAX_NAVBAR_WIDTH, max(_MIN_NAVBAR_WIDTH, width)) if width else _DEFAULT_NAVBAR_WIDTH,
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
            dcc.Link(
                project.name,
                href=f"/project/{project_id}",
                refresh=False,
                style={"fontWeight": 600, "fontSize": "var(--mantine-font-size-sm)"},
            ),
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
