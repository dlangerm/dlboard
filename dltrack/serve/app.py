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
from structlog.stdlib import get_logger

from dltrack.models import InstalledPlugin, constants
from dltrack.serve._assets import AssetKind, serve_asset
from dltrack.serve._backend import _artifact_download
from dltrack.serve._backend._auth import (
    SIGN_OUT_PATH,
    find_current_user,
    get_auth_provider,
    install_request_gate,
)
from dltrack.serve._backend._data_store import get_data_store, get_system_data_store
from dltrack.serve._backend._installed_plugins import set_installed_plugins
from dltrack.serve._backend._theme import get_theme
from dltrack.serve._clientside_script import ClientsideScript
from dltrack.serve._icons import Icon, icon, install_icons
from dltrack.serve._jump import install_jump, jump_modal, jump_trigger
from dltrack.serve._pages._dash_helpers import section_label

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
_NAVBAR_STATE_JS = ClientsideScript(Path(__file__).with_name("navbar_state.js"))
_NAVBAR_TOGGLE_JS = ClientsideScript(Path(__file__).with_name("navbar_toggle.js"))
_DEFAULT_NAVBAR_WIDTH = 300
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
    # Also core, non-optional: every chart that shows an artifact fetches it from this one route,
    # by id -- never by talking to whatever `ArtifactStore` a deployment happens to have plugged in.
    _artifact_download.register(_app)
    # Every route above (and every plugin route) sits behind this one gate -- see `_auth.py`.
    install_request_gate(_app, lambda: get_system_data_store(_app))
    # Only the immutable `InstalledPlugin` snapshots are retained on the app -- not the plugin
    # modules/objects themselves, so introspecting this later (the admin page's About tab) can't
    # reach back into a plugin's own state.
    set_installed_plugins(_app, installed)
    for kind, path in _ASSETS:
        serve_asset(_app, kind, path.name, path.read_bytes())
    install_icons(_app)
    install_jump(_app)
    # Pure client-side: whether the navbar shows at all only depends on the URL, and its
    # collapsed/width state lives in localStorage -- neither needs the server.
    _app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _NAVBAR_STATE_JS.source,
        Output("appshell", "navbar"),
        Output(constants.NAVBAR_COLLAPSE_TOGGLE_ID, "style"),
        Input(constants.LOCATION_ID, "pathname"),
        Input(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
        Input(constants.NAVBAR_WIDTH_STORE_ID, "data"),
    )
    _app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _NAVBAR_TOGGLE_JS.source,
        Output(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
        Input(constants.NAVBAR_COLLAPSE_TOGGLE_ID, "n_clicks"),
        State(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
        prevent_initial_call=True,
    )
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
                                    icon(Icon.SIDEBAR, size="1.25rem"),
                                    id=constants.NAVBAR_COLLAPSE_TOGGLE_ID,
                                    variant="subtle",
                                    color="gray",
                                    visibleFrom="sm",
                                    **cast("dict[str, Any]", {"aria-label": "Toggle sidebar"}),
                                ),
                                dmc.Anchor(
                                    dmc.Group(
                                        [
                                            html.Span(icon(Icon.LOGO), className="dl-logo"),
                                            dmc.Text("dltrack", fw=650, size="md"),
                                        ],
                                        gap=8,
                                        wrap="nowrap",
                                    ),
                                    href="/",
                                    refresh=True,
                                    underline="never",
                                    c="bright",
                                ),
                                dmc.Breadcrumbs(
                                    id=constants.PAGE_BREADCRUMB_ID,
                                    separator=icon(Icon.BREADCRUMB),
                                    separatorMargin=6,
                                    c="dimmed",
                                    visibleFrom="xs",
                                    children=[],
                                ),
                            ],
                            gap="md",
                            wrap="nowrap",
                        ),
                        dmc.Group(
                            [
                                jump_trigger(),
                                dmc.ColorSchemeToggle(
                                    lightIcon=icon(Icon.LIGHT_MODE, size="1.125rem"),
                                    darkIcon=icon(Icon.DARK_MODE, size="1.125rem"),
                                    variant="subtle",
                                    color="gray",
                                    **cast("dict[str, Any]", {"aria-label": "Toggle light/dark mode"}),
                                ),
                                html.Div(_user_menu(), id=constants.HEADER_USER_INDICATOR_ID),
                            ],
                            gap="xs",
                            wrap="nowrap",
                        ),
                    ],
                    justify="space-between",
                    h="100%",
                    px="md",
                    wrap="nowrap",
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
            jump_modal(),
            Store(id=constants.NAVBAR_COLLAPSED_STORE_ID, data=False, storage_type="local"),
            Store(id=constants.NAVBAR_WIDTH_STORE_ID, data=_DEFAULT_NAVBAR_WIDTH, storage_type="local"),
        ],
        header={"height": 52},
        padding="md",
        navbar={"width": _DEFAULT_NAVBAR_WIDTH, "breakpoint": "sm", "collapsed": {"mobile": True}},
        # Collapsing/expanding snaps instead of sliding: the navbar's state is applied as each page
        # mounts (see `navbar_state.js`), where a 200ms slide would read as the page jumping.
        transitionDuration=0,
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
def breadcrumbs(_: str, project_id: int | None, experiment_id: int | None) -> list[Component]:
    """Where the current page sits: Projects > project > experiment, the last one emphasized."""
    trail = [("Projects", "/")]
    if project_id is not None:
        store = get_data_store()
        trail.append((store.get_project(project_id).name, f"/project/{project_id}"))
        if experiment_id is not None:
            experiment = store.get_experiment(experiment_id)
            name = experiment.name if experiment and experiment.name else f"Experiment {experiment_id}"
            trail.append((name, f"/experiment/{experiment_id}"))
    *parents, (current, _href) = trail
    return [
        *(
            # Home is a full reload; in-project links stay client-side (see each page's layout).
            dmc.Anchor(label, href=href, refresh=href == "/", c="dimmed", size="sm", underline="hover")
            for label, href in parents
        ),
        dmc.Text(current, c="bright", size="sm", fw=500, truncate="end", maw=320),
    ]


def _user_menu() -> Component:
    """Who the app resolves the caller to be, how, and the account-level links (Admin, sign-out)."""
    user = find_current_user()
    if user is None:  # Dash's one-time layout validation, on whatever the first request happens to be
        return html.Div()
    provider = get_auth_provider()
    return dmc.Menu(
        [
            dmc.MenuTarget(
                dmc.ActionIcon(
                    dmc.Avatar(name=user.username, color="initials", size="sm", radius="xl"),
                    variant="subtle",
                    color="gray",
                    radius="xl",
                    size="lg",
                    **cast("dict[str, Any]", {"aria-label": f"Account menu for {user.username}"}),
                )
            ),
            dmc.MenuDropdown(
                [
                    dmc.MenuLabel("Signed in as"),
                    dmc.Text(user.username, size="sm", fw=600, px="sm"),
                    dmc.Text(f"via {provider.display_name}", size="xs", c="dimmed", px="sm", pb=6),
                    dmc.MenuDivider(),
                    *(
                        [dmc.MenuItem("Sign-in settings", href=provider.manage_url, refresh=True)]
                        if provider.manage_url is not None
                        else []
                    ),
                    dmc.MenuItem("Admin", href="/admin", refresh=True, leftSection=icon(Icon.ADMIN)),
                    *(
                        [
                            dmc.MenuItem(
                                "Sign out", href=SIGN_OUT_PATH, refresh=True, leftSection=icon(Icon.SIGN_OUT)
                            )
                        ]
                        if provider.verifies_identity
                        else []
                    ),
                ]
            ),
        ],
        position="bottom-end",
        width=220,
    )


@callback(
    Output(constants.NAVBAR_CONTENT_ID, "children"),
    Input(constants.STATE_PROJECT_ID, component_property="data", allow_optional=True),
    Input(constants.STATE_EXPERIMENT_ID, component_property="data", allow_optional=True),
)
def render_navbar(project_id: int | None, experiment_id: int | None) -> Component:
    """The current project's experiments. Empty outside a project, where the navbar is hidden."""
    if project_id is None:
        return html.Div()

    store = get_data_store()
    project = store.get_project(project_id)
    experiments = list(store.get_experiments(project_id))
    return dmc.Stack(
        [
            dmc.Anchor(
                project.name,
                href=f"/project/{project_id}",
                refresh=False,
                c="bright",
                fw=600,
                size="sm",
                underline="never",
                truncate="end",
            ),
            section_label(f"Experiments · {len(experiments)}"),
            dmc.Stack(
                [
                    dmc.NavLink(
                        label=e.name or f"Experiment {e.id}",
                        href=f"/experiment/{e.id}",
                        active=e.id == experiment_id,
                        variant="light",
                    )
                    for e in experiments
                ],
                gap=2,
            ),
        ],
        gap="xs",
        mb="md",
    )
