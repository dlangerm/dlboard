"""The experiment page for dlboard."""

from typing import Any, cast

import dash
import dash_mantine_components as dmc
import pendulum
from dash import dcc, html
from dash.dcc import Store
from dash.development.base_component import Component
from flask import request

from dlboard.serve import _constants as constants
from dlboard.serve import get_current_user, get_data_store
from dlboard.serve._pages._experiment import _experiment_page_state as core
from dlboard.serve._pages._experiment import render_panel
from dlboard.serve._pages._experiment._notes import STATE_NOTES_REVISION, notes_button, notes_drawer
from dlboard.serve._pages._experiment._paging import SIZES_COOKIE, Paging
from dlboard.serve._pages._experiment._run_compare import CompareQuery, compare_components
from dlboard.serve._pages._experiment._views import resolve_view_id, view_controls
from dlboard.serve._pages._onboarding import first_run_snippet, onboarding_card

_LIVE_UPDATE_SETTINGS = core.LiveUpdateSettings()

# One dcc.Store per drag-drop gesture a completed drag can report (`_experiment_page_dragdrop.js`,
# via `set_props`) -- listed in the static layout below, not `accordion_view`'s render tree, so the
# drop target always exists regardless of what's currently rendered underneath it.
_DRAG_DROP_STORE_IDS = [
    core.PANEL_REORDER_STORE_ID,
    core.CHART_REORDER_STORE_ID,
    core.TAB_DROP_STORE_ID,
    core.CHART_TAB_DROP_STORE_ID,
    core.CHART_PANEL_MOVE_STORE_ID,
]


def _live_update_header(header: Component, notes: Component, views: Component) -> dmc.Group:
    """The experiment header, its view picker, and the live-update status badge/switch/"fetched Xs ago" label."""
    return dmc.Group(
        [
            html.Div(header, id=core.EXPERIMENT_HEADER_ID),
            dmc.Group(
                [
                    notes,
                    views,
                    dmc.Divider(orientation="vertical"),
                    html.Div(core.live_status_badge(ok=True), id=core.LIVE_STATUS_ID),
                    # Hidden until the switch below is unchecked -- a plain CSS visibility swap
                    # with the div above (see `live_switch_state.js`), not a second callback
                    # writing that div's `children`.
                    html.Div(
                        dmc.Badge("Paused", color="gray", variant="dot", size="sm", tt="none"),
                        id=core.LIVE_PAUSED_BADGE_ID,
                        style={"display": "none"},
                    ),
                    dmc.Text("Fetched just now", id=core.LIVE_LAST_FETCH_LABEL_ID, size="xs", c="dimmed"),
                    dmc.Tooltip(
                        # `persistence`/`persistence_type="local"` is the *entire* mechanism here --
                        # Dash writes `checked` straight to the browser's own `localStorage` keyed
                        # by this component's id, a per-browser viewing preference. No `dcc.Store`,
                        # no server round trip, and nothing shared with other viewers of the same
                        # experiment.
                        dmc.Switch(
                            id=core.LIVE_UPDATES_ENABLED_ID,
                            checked=True,
                            size="xs",
                            persistence=True,
                            persistence_type="local",
                            persisted_props=["checked"],
                        ),
                        label="Live updates",
                        position="top",
                        withArrow=True,
                    ),
                ],
                gap="xs",
                wrap="nowrap",
            ),
        ],
        justify="space-between",
        wrap="nowrap",
    )


def layout(
    experiment_id: str, chart: str | None = None, view: str | None = None, **query: str
) -> list[html.Div | dcc.Store | dcc.Interval]:
    """
    The experiment page, with its URL's query parameters passed in as keyword arguments by Dash.

    `?view=` (a `Page.id`) shows one of the named views instead of the shared page (see `_views.py`).
    `?chart=` (a `ChartInstance.id`, from a copied chart link) opens that chart's panel and tab, and
    `chart_deep_link.js` scrolls to it. `?compare=`, `?compare_mode=` and `?compare_q=` open the
    run-compare modal on that state (see `CompareQuery`). `?panels_q=`, `?panels_page=` and
    `?charts=` say which page of panels and charts is showing (see `Paging`). Any other query parameter is ignored.
    """
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    hparams = store.fetch_hyperparams(int(experiment_id))
    # Rendered synchronously, here, rather than by a `render_initial`-style callback fired after
    # this static shell mounts -- see `render_panel`'s own docstring for why that's the one thing
    # that actually eliminates the page's extra round trip, not just hides it behind a spinner.
    view_id = resolve_view_id(store, exp.id, view)
    container, header, page_json = render_panel(
        store,
        core.PageRef(exp.id, view_id),
        Paging.from_request(query, request.cookies.get(SIZES_COOKIE)),
        focus_chart=chart,
    )
    current_page = core.BasicExperimentPage.model_validate_json(page_json)
    is_owner = view_id is not None and current_page.owner_id == get_current_user().id

    return [
        html.Div(
            id=core.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    _live_update_header(
                        header,
                        notes_button(len(store.list_comments(exp.id))),
                        view_controls(
                            store,
                            exp.id,
                            view_id,
                            is_owner=is_owner,
                            view_name=current_page.name,
                            is_shared=current_page.shared,
                        ),
                    ),
                    notes_drawer(),
                    *compare_components(store, exp.id, CompareQuery.from_query(query)),
                    # Nothing logged yet: show how to log the first run, right where its charts will go.
                    onboarding_card(
                        title="No runs yet — log your first one",
                        snippet=first_run_snippet(exp.project_id, exp.id),
                    )
                    if next(store.get_runs(exp.id, limit=1), None) is None
                    else None,
                    # `container` (from `render_panel`, above) is real content from the very first
                    # response now, not a `dmc.Loader()` placeholder -- `dcc.Loading` still wraps it
                    # for every *later* full-panel rebuild (add/delete/rename a panel or chart,
                    # apply suggestions, ...), which still goes through a callback writing this
                    # same `children`. `custom_spinner` matches those rebuilds' spinner to the
                    # app's own `dmc.Loader()` rather than `dcc.Loading`'s unstyled default, so a
                    # rebuild doesn't visibly swap from one loading indicator to an
                    # unrelated-looking second one partway through.
                    #
                    # `target_components` is required now that live updates poll in the
                    # background: without it, `dcc.Loading` shows its overlay for *any* in-flight
                    # callback touching a descendant, which includes `poll_for_updates`'s per-chart
                    # patches (they live inside this subtree, at `chart_content_id(...)`) -- a
                    # stop-the-world flash on every poll tick, exactly what per-chart patching
                    # exists to avoid. Scoping it to just this div's own `children` means the
                    # spinner only ever covers a genuine full-panel rebuild; the poll's targeted
                    # patches update their charts silently. Its generated type stub is an empty
                    # `TypedDict` (a known Dash codegen gap for this genuinely dynamic dict) --
                    # `cast` to `Any` rather than fight that, same as `_experiment_page_state.py`'s
                    # `data-*`-attr casts.
                    cast("Any", dcc.Loading)(
                        html.Div(container, id=core.METRIC_CONTENT_ID),
                        delay_show=250,
                        target_components={str(core.METRIC_CONTENT_ID): "children"},
                        custom_spinner=dmc.Loader(size="lg"),
                    ),
                ],
                gap="xs",
            ),
        ),
        # Drives `poll_for_updates` (`_experiment/__init__.py`): each tick does one indexed PK read
        # of `Experiment.revision`, only paying for a real refresh when it's actually changed since
        # the last poll -- see `LiveUpdateSettings`. `disabled` is flipped client-side by
        # `LIVE_UPDATES_ENABLED_ID` (`live_switch_state.js`) -- unchecking it genuinely stops this
        # from firing at all, not just from being acted on.
        dcc.Interval(id=core.LIVE_POLL_INTERVAL_ID, interval=_LIVE_UPDATE_SETTINGS.poll_interval_ms),
        # Drives the "Fetched Xs ago" label (`live_last_fetch_label.js`) -- purely client-side, so
        # this stays enabled (and ticking, showing growing staleness) even while the poll above is
        # switched off.
        dcc.Interval(id=core.LIVE_TICK_INTERVAL_ID, interval=_LIVE_UPDATE_SETTINGS.tick_interval_ms),
        Store(id=core.STATE_LAST_KNOWN_REVISION, data=exp.revision),
        Store(id=core.STATE_CHART_CONTENT_HASHES, data={}),
        Store(id=core.STATE_LAST_FETCH_AT, data=pendulum.now("UTC").isoformat()),
        Store(id=core.STATE_PAGE_STORAGE, data=page_json),
        Store(id=core.STATE_VIEW_ID, data=view_id),
        Store(id=STATE_NOTES_REVISION, data=exp.notes_revision),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=core.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
        *(Store(id=store_id) for store_id in _DRAG_DROP_STORE_IDS),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")
