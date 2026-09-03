"""The experiment page for dltrack."""

from typing import Any, cast

import dash
import dash_mantine_components as dmc
import pendulum
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants
from dltrack.serve import get_data_store
from dltrack.serve._pages._experiment import _experiment_page_state as core
from dltrack.serve._pages._experiment import render_panel

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


def layout(experiment_id: str) -> list[html.Div | dcc.Store | dcc.Interval]:
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    hparams = store.fetch_hyperparams(int(experiment_id))
    # Rendered synchronously, here, rather than by a `render_initial`-style callback fired after
    # this static shell mounts -- see `render_panel`'s own docstring for why that's the one thing
    # that actually eliminates the mount-cascade, not just guards around it.
    container, header, page_json = render_panel(store, int(experiment_id))

    return [
        html.Div(
            id=core.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    dmc.Group(
                        [
                            html.Div(header, id=core.EXPERIMENT_HEADER_ID),
                            dmc.Group(
                                [
                                    html.Div(core.live_status_badge(ok=True), id=core.LIVE_STATUS_ID),
                                    # Hidden until the switch below is unchecked -- a plain CSS
                                    # visibility swap with the div above (see
                                    # `live_updates_toggle_shows_paused_badge.js`), not a second
                                    # callback writing that div's `children`.
                                    html.Div(
                                        dmc.Badge("Paused", color="gray", variant="light", size="xs"),
                                        id=core.LIVE_PAUSED_BADGE_ID,
                                        style={"display": "none"},
                                    ),
                                    dmc.Text(
                                        "Fetched just now",
                                        id=core.LIVE_LAST_FETCH_LABEL_ID,
                                        size="xs",
                                        c="dimmed",
                                    ),
                                    dmc.Tooltip(
                                        # `persistence`/`persistence_type="local"` is the *entire*
                                        # mechanism here -- Dash writes `checked` straight to the
                                        # browser's own `localStorage` keyed by this component's id,
                                        # a per-browser viewing preference. No `dcc.Store`, no
                                        # server round trip, and nothing shared with other viewers of
                                        # the same experiment.
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
                    ),
                    # `container` (from `render_panel`, above) is real content from the very first
                    # response now, not a `dmc.Loader()` placeholder -- `dcc.Loading` still wraps it
                    # for every *later* full-panel rebuild (add/delete/rename a panel or chart,
                    # apply suggestions, ...): every one of those still goes through a callback
                    # writing this same `children`, so the spinner still earns its keep there.
                    #
                    # `custom_spinner` is the same `dmc.Loader()` those rebuilds would otherwise show
                    # nothing-then-something with, rather than left at `dcc.Loading`'s own default --
                    # Dash's built-in spinner is a completely different (unstyled, purple) component,
                    # so without this a rebuild visibly swaps from one loading indicator to an
                    # unrelated-looking second one partway through, which reads as broken rather than
                    # as one continuous wait.
                    #
                    # `target_components` is required here: without it, `dcc.Loading` shows its
                    # overlay for *any* in-flight callback touching a descendant component, which
                    # includes `poll_for_updates`'s per-chart patches (they live inside this
                    # subtree, at `chart_content_id(...)`) -- a stop-the-world flash on every live
                    # update tick, exactly the whole-page-refresh feel this feature exists to avoid.
                    # Scoping it to just this Div's own "children" means the spinner only ever
                    # covers a genuine full-panel rebuild; the poll's targeted patches update their
                    # charts silently.
                    # `target_components`'s generated type stub is an empty `TypedDict` (a known
                    # Dash codegen gap for this genuinely dynamic dict) -- `cast` to `Any` rather
                    # than fight that, same as `_experiment_page_state.py`'s `data-*`-attr casts.
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
        # Drives `poll_for_updates` (`__init__.py`): each tick does one indexed PK read of
        # `Experiment.revision`, only paying for a full re-render when it's actually changed since
        # the last poll -- see `LiveUpdateSettings`. `disabled` is flipped client-side by
        # `LIVE_UPDATES_ENABLED_ID` (`live_updates_toggle_disables_interval.js`) -- unchecking it
        # genuinely stops this from firing at all, not just from being acted on.
        dcc.Interval(id=core.LIVE_POLL_INTERVAL_ID, interval=_LIVE_UPDATE_SETTINGS.poll_interval_ms),
        # Drives the "Fetched Xs ago" label (`live_last_fetch_label.js`) -- purely client-side, so
        # this stays enabled (and ticking, showing growing staleness) even while the poll above is
        # switched off.
        dcc.Interval(id=core.LIVE_TICK_INTERVAL_ID, interval=_LIVE_UPDATE_SETTINGS.tick_interval_ms),
        Store(id=core.STATE_LAST_KNOWN_REVISION, data=exp.revision),
        Store(id=core.STATE_CHART_CONTENT_HASHES, data={}),
        Store(id=core.STATE_LAST_FETCH_AT, data=pendulum.now("UTC").isoformat()),
        Store(id=core.STATE_PAGE_STORAGE, data=page_json),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=core.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
        *(Store(id=store_id) for store_id in _DRAG_DROP_STORE_IDS),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
