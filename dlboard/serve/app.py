"""Core components for the dlboard server."""

from __future__ import annotations

import json
from functools import partial, wraps
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import dash
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, dcc, html
from dash.dcc import Store
from structlog.stdlib import get_logger

from dlboard._telemetry.traces import SpanKind, span
from dlboard.models import InstalledPlugin
from dlboard.serve import _constants as constants
from dlboard.serve import telemetry
from dlboard.serve._assets import AssetKind, serve_asset
from dlboard.serve._backend import _artifact_download, _health, _request_logging
from dlboard.serve._backend._app_slot import DASH_APP_EXTENSION
from dlboard.serve._backend._auth import (
    SIGN_OUT_PATH,
    find_current_user,
    get_auth_provider,
    install_request_gate,
)
from dlboard.serve._backend._data_store import get_data_store, get_system_data_store
from dlboard.serve._backend._installed_plugins import set_installed_plugins
from dlboard.serve._backend._sql_store_base import SQLStoreBase
from dlboard.serve._backend._startup_checks import add_startup_check, run_startup_checks
from dlboard.serve._backend._theme import get_theme
from dlboard.serve._clientside_script import ClientsideScript
from dlboard.serve._grid import GRID_CELL_COMPONENTS_CSS, GRID_CELL_COMPONENTS_JS, GRID_FORMATTERS_JS
from dlboard.serve._icons import Icon, icon, install_icons
from dlboard.serve._jump import install_jump, jump_modal, jump_trigger
from dlboard.serve._pages._dash_helpers import section_label
from dlboard.serve._proxy import apply_proxy_fix
from dlboard.serve._stale_page import install_stale_page_guard
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component

    from dlboard import models
    from dlboard.models import ThemeSpec


_log = get_logger(__name__)

_ASSETS = [
    (AssetKind.STYLESHEET, Path(__file__).with_name("_shell.css")),
    # The value formatters every ag-grid column def names (`serve/_grid.py`).
    (AssetKind.SCRIPT, GRID_FORMATTERS_JS),
    # The slider and the key picker the chart editor edits its tables with (`serve/_grid.py`).
    (AssetKind.SCRIPT, GRID_CELL_COMPONENTS_JS),
    (AssetKind.STYLESHEET, GRID_CELL_COMPONENTS_CSS),
]
_COLOR_SCHEME_PRELOAD_JS = Path(__file__).with_name("color_scheme_preload.js").read_text()
_NAVBAR_STATE_JS = ClientsideScript(Path(__file__).with_name("navbar_state.js"))
_NAVBAR_TOGGLE_JS = ClientsideScript(Path(__file__).with_name("navbar_toggle.js"))
_NAVBAR_RESIZE_JS = Path(__file__).with_name("navbar_resize.js")


class _NavbarWidth(NamedTuple):
    """The navbar's width in pixels: what a drag may reach, and what it starts at."""

    min: int
    max: int
    default: int


_NAVBAR_WIDTH = _NavbarWidth(min=260, max=640, default=300)
PAGE_LOADING_CLASS = "dl-page-loading"


def _reported_when_it_fails[**P](build: Callable[P, Dash]) -> Callable[P, Dash]:
    """
    Run `build` as the `dlboard.startup` span, and log a failure of it as one structured error.

    A server that can't start otherwise dies with the interpreter's raw traceback on stderr: outside the log format,
    out of reach of `DLBOARD_TELEMETRY_LOGS`, and tied to no trace. Logged here it is one record with its exception,
    on the span that failed, which the platform's alerting can see.
    """

    @wraps(build)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> Dash:
        # Before the span, and before any store creates its database engine -- see `telemetry`.
        telemetry.instrument_libraries()
        with span("dlboard.startup", SpanKind.INTERNAL):
            try:
                return build(*args, **kwargs)
            except Exception:
                _log.exception("dlboard could not start")
                raise

    return wrapped


@_reported_when_it_fails
def app(plugins: list[models.PluginProtocol], *, url_prefix: str = "") -> Dash:
    """
    Get the app initialized with a set of plugins.

    `url_prefix` (`DLBOARD_URL_PREFIX`, e.g. `"dlboard"`) is for a reverse proxy that forwards the
    full, un-rewritten path (`https://host/dlboard/...`) straight through rather than stripping the
    prefix first -- the simpler and far more common nginx/Caddy config. Every route this app
    registers (Dash's own, and every `add_public_route`/`add_url_rule` call elsewhere, which already
    read `app.config.routes_pathname_prefix`) lives under that same prefix, via Dash's single
    `url_base_pathname` knob rather than its `requests_pathname_prefix`/`routes_pathname_prefix`
    split -- that split exists for a proxy that *does* strip the prefix (pairing with a WSGI-level
    `SCRIPT_NAME`), a fiddlier setup this doesn't try to support.
    """
    # Imported here, not at module level: chart plugins import `from dlboard.serve import
    # ClientsideScript` at their own module level, so importing these page modules (which reach
    # back into concrete chart classes, e.g. `_chart_autogen.py` imports `ImageChart` directly) at
    # `dlboard.serve` import time can race that still-in-progress import. Deferring to call time
    # sidesteps it, since `app()` only ever runs after process startup import resolution finishes.
    from dlboard.serve._pages import _account_page, _simple_admin_page, _simple_homepage, _simple_project_page
    from dlboard.serve._pages import _experiment as _experiment_page

    installed = [InstalledPlugin.describe(p) for p in plugins]
    _log.info("DLBoard creating dash app with plugins:")
    for p in installed:
        _log.info(p.name)
    _app = Dash(
        __name__,
        title="dlboard",
        use_pages=True,
        pages_folder="_pages",
        suppress_callback_exceptions=True,
        plugins=plugins,
        url_base_pathname=f"/{url_prefix.strip('/')}/" if url_prefix else None,
    )
    # So a plain Flask route on this server (every REST route) resolves *this* app, not Dash's
    # process-wide "last app built" -- see `current_dash_app`.
    _app.server.extensions[DASH_APP_EXTENSION] = _app
    # The tab+accordion page layouts (home, project, admin, experiment, account) are opinionated,
    # non-optional dlboard behavior -- always wired into every app(), never part of a deployment's
    # own `plugins` list, and deliberately excluded from the `installed` snapshot above (which
    # should only reflect what a deployment actually chose to install). These page modules don't
    # implement `PluginProtocol`, so `register(app)` is called directly rather than threaded
    # through Dash's own `plugins=` constructor kwarg.
    for page in (_simple_homepage, _simple_project_page, _experiment_page, _simple_admin_page, _account_page):
        page.register(_app)
    # Also core, non-optional: every chart that shows an artifact fetches it from this one route,
    # by id -- never by talking to whatever `ArtifactStore` a deployment happens to have plugged in.
    _artifact_download.register(_app)
    # `/healthz`/`/readyz` for a process manager or orchestrator -- public, like sign-out below.
    _health.register(_app)
    # `/metrics`, only if a deployment asked for a Prometheus scrape -- see `telemetry`.
    telemetry.register(_app)
    # Every route above (and every plugin route) sits behind this one gate -- see `_auth.py`.
    install_request_gate(_app, lambda: get_system_data_store(_app))
    # A request id on every log line, and the request metrics, for every route (auth included) -- after
    # the gate, since it has to run ahead of it: see its own docstring.
    _request_logging.register(_app)
    # Each request continues the trace of whoever sent it, with the SQL it runs as child spans. Last, so that it
    # sees every other request hook (the gate's included) and can put its own ahead of them all.
    telemetry.instrument_app(_app)
    # Opt-in (DLBOARD_TRUSTED_PROXIES): trust a reverse proxy's X-Forwarded-* headers for scheme/
    # host detection, so DLBOARD_SECURE_COOKIES sessions survive behind a TLS-terminating proxy.
    apply_proxy_fix(_app)
    # Only the immutable `InstalledPlugin` snapshots are retained on the app -- not the plugin
    # modules/objects themselves, so introspecting this later (the admin page's About tab) can't
    # reach back into a plugin's own state.
    set_installed_plugins(_app, installed)
    # The navbar drag-to-resize handle, served with the width bounds `navbar_state.js` clamps to as well.
    serve_asset(
        _app,
        AssetKind.SCRIPT,
        _NAVBAR_RESIZE_JS.name,
        f"window.dlNavbarWidth = {json.dumps(_NAVBAR_WIDTH._asdict())};\n".encode()
        + _NAVBAR_RESIZE_JS.read_bytes(),
    )
    for kind, path in _ASSETS:
        serve_asset(_app, kind, path.name, path.read_bytes())
    install_icons(_app)
    install_jump(_app)
    # A page left open across a deploy still sends the old callbacks' shape: say so, once, instead of a 500 per poll.
    install_stale_page_guard(_app)
    # Pure client-side: whether the navbar shows at all only depends on the URL, and its
    # collapsed/width state lives in localStorage -- neither needs the server.
    _app.clientside_callback(
        _NAVBAR_STATE_JS.source,
        Output("appshell", "navbar"),
        Output(constants.NAVBAR_COLLAPSE_TOGGLE_ID, "style"),
        Input(constants.LOCATION_ID, "pathname"),
        Input(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
        Input(constants.NAVBAR_WIDTH_STORE_ID, "data"),
    )
    _app.clientside_callback(
        _NAVBAR_TOGGLE_JS.source,
        Output(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
        Input(constants.NAVBAR_COLLAPSE_TOGGLE_ID, "n_clicks"),
        State(constants.NAVBAR_COLLAPSED_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    # On this app, never through Dash's global `callback`: Dash moves those into whichever app in the process serves
    # first, so a second app (a test's) left the first with no breadcrumbs and an empty navbar (#449).
    _app.callback(
        Output(constants.PAGE_BREADCRUMB_ID, "children"),
        Input(constants.LOCATION_ID, "pathname"),
        Input(constants.STATE_PROJECT_ID, "data", allow_optional=True),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
    )(_breadcrumbs)
    _app.callback(
        Output(constants.NAVBAR_CONTENT_ID, "children"),
        Input(constants.STATE_PROJECT_ID, "data", allow_optional=True),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
    )(_render_navbar)
    # A theme plugin's `plug()` has already run by now (inside `Dash(...)` above), so its theme
    # goes straight into the first response -- no callback, no flash of an unthemed page.
    theme = get_theme(_app)
    _app.index_string = _preload_color_scheme(_app.index_string, theme)
    _app.layout = partial(_layout, theme)
    # Last, so a plugin's check sees the app exactly as it will serve -- every plugin plugged, the gate installed.
    add_startup_check(_app, _check_stored_timestamps)
    run_startup_checks(_app)
    return _app


def _check_stored_timestamps(app: Dash) -> None:
    """The stored timestamps must read as the metric frame takes them (#468), for a store that reads them raw."""
    store = get_system_data_store(app)
    if isinstance(store, SQLStoreBase):
        store.check_stored_timestamps()


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
                                            dmc.Text("dlboard", fw=650, size="md"),
                                        ],
                                        gap=8,
                                        wrap="nowrap",
                                    ),
                                    href=relative_path("/"),
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
                cast("Any", dcc.Loading)(  # pyrefly: ignore [unknown-argument-type]
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
            Store(id=constants.NAVBAR_WIDTH_STORE_ID, data=_NAVBAR_WIDTH.default, storage_type="local"),
        ],
        header={"height": 52},
        padding="md",
        navbar={"width": _NAVBAR_WIDTH.default, "breakpoint": "sm", "collapsed": {"mobile": True}},
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


def _breadcrumbs(_: str, project_id: int | None, experiment_id: int | None) -> list[Component]:
    """Where the current page sits: Projects > project > experiment, the last one emphasized."""
    trail = [("Projects", relative_path("/"))]
    store = get_data_store()
    project = store.get_project(project_id) if project_id is not None else None
    if project is not None:
        trail.append((project.name, relative_path(f"/project/{project.id}")))
        if experiment_id is not None:
            experiment = store.get_experiment(experiment_id)
            name = experiment.name if experiment and experiment.name else f"Experiment {experiment_id}"
            trail.append((name, relative_path(f"/experiment/{experiment_id}")))
    home_href = relative_path("/")
    *parents, (current, _href) = trail
    return [
        *(
            # Home is a full reload; in-project links stay client-side (see each page's layout).
            dmc.Anchor(label, href=href, refresh=href == home_href, c="dimmed", size="sm", underline="hover")
            for label, href in parents
        ),
        dmc.Text(current, c="bright", size="sm", fw=500, truncate="end", maw=320),
    ]


def _user_menu() -> Component:
    """Who the app resolves the caller to be, how, and the account-level links (Account, Admin, sign-out)."""
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
                    dmc.MenuItem(
                        "Account",
                        href=relative_path("/account"),
                        refresh=True,
                        leftSection=icon(Icon.KEY),
                    ),
                    *(
                        [dmc.MenuItem("Sign-in settings", href=provider.manage_url, refresh=True)]
                        if provider.manage_url is not None
                        else []
                    ),
                    dmc.MenuItem(
                        "Admin",
                        href=relative_path("/admin"),
                        refresh=True,
                        leftSection=icon(Icon.ADMIN),
                    ),
                    *(
                        [
                            dmc.MenuItem(
                                "Sign out",
                                href=relative_path(SIGN_OUT_PATH),
                                refresh=True,
                                leftSection=icon(Icon.SIGN_OUT),
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


def _render_navbar(project_id: int | None, experiment_id: int | None) -> Component:
    """The current project's experiments. Empty outside a project, where the navbar is hidden."""
    store = get_data_store()
    project = store.get_project(project_id) if project_id is not None else None
    if project is None:
        return html.Div()

    experiments = list(store.get_experiments(project.id))
    return dmc.Stack(
        [
            dmc.Anchor(
                project.name,
                href=relative_path(f"/project/{project_id}"),
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
                        href=relative_path(f"/experiment/{e.id}"),
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
