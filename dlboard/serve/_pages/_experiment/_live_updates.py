"""
Live updates: a cheap poll that redraws only the open charts whose own data changed, and their status.

The badge, switch and "fetched Xs ago" label say how fresh the page is.

Owns its ids, its markup (`live_controls`, `live_timers`), the poll callback, and the two clientside
callbacks that keep switching it off and the label's clock off the server entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
import pendulum
from dash import Dash, Input, Output, State, html, no_update
from dash.dcc import Interval, Store
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard.serve import ClientsideScript, get_data_store
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import DivId, IntervalId, StoreId, ValueId
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment._notes import STATE_NOTES_REVISION
from dlboard.serve._pages._experiment._paging import Paging
from dlboard.serve._pages._experiment._shown_runs import shown_run_ids

if TYPE_CHECKING:
    import pandas as pd
    from dash.development.base_component import Component

    from dlboard import models
    from dlboard.models import DataStore

_log = get_logger(__name__)

_LIVE_SWITCH_STATE_JS = ClientsideScript(Path(__file__).with_name("live_switch_state.js"))
_LIVE_LAST_FETCH_LABEL_JS = ClientsideScript(Path(__file__).with_name("live_last_fetch_label.js"))

# --- live updates: a `dcc.Interval`-driven poll of `Experiment.revision` re-renders whichever open
# charts have actually changed since the last poll; a small badge reports whether the poll itself
# is healthy. See `poll_for_updates`. ---
LIVE_POLL_INTERVAL_ID: IntervalId[ids.ExperimentPage] = IntervalId("live-poll-interval")
STATE_LAST_KNOWN_REVISION: StoreId[ids.ExperimentPage] = StoreId("last-known-revision")
LIVE_STATUS_ID: DivId[ids.ExperimentPage] = DivId("live-status")

# --- enable/disable + "fetched Xs ago": both pure client-side concerns, deliberately kept off the
# server. `LIVE_UPDATES_ENABLED_ID`'s `checked` persists to the browser's own `localStorage` (Dash's
# built-in `persistence`/`persistence_type="local"`, set where it's rendered in
# `serve/_pages/experiment.py`) -- a per-browser viewing preference, not something to write into
# `Experiment`/`Page` and have every other viewer inherit. `LIVE_TICK_INTERVAL_ID` ticks
# `LIVE_LAST_FETCH_LABEL_ID`'s text once a second purely from `STATE_LAST_FETCH_AT` (a clientside
# callback, see `live_last_fetch_label.js`) -- no server round trip just to update a clock. ---
LIVE_UPDATES_ENABLED_ID: ValueId[ids.ExperimentPage] = ValueId("live-updates-enabled")
LIVE_TICK_INTERVAL_ID: IntervalId[ids.ExperimentPage] = IntervalId("live-tick-interval")
LIVE_LAST_FETCH_LABEL_ID: DivId[ids.ExperimentPage] = DivId("live-last-fetch-label")
LIVE_PAUSED_BADGE_ID: DivId[ids.ExperimentPage] = DivId("live-paused-badge")
"""The "Paused" badge shown instead of `LIVE_STATUS_ID` while live updates are switched off -- a
sibling element toggled by CSS `display` (see `live_switch_state.js`), not a second callback
writing `LIVE_STATUS_ID.children` -- `poll_for_updates` stays that output's only owner."""
STATE_LAST_FETCH_AT: StoreId[ids.ExperimentPage] = StoreId("last-fetch-at")
"""ISO 8601 UTC timestamp of the last poll tick that actually reached the store successfully --
set on *every* such tick, whether or not anything had changed, since it answers "how stale could
what I'm looking at be" rather than "when did it last change"."""


class LiveUpdateSettings(BaseSettings):
    """`DLBOARD_*` environment variables controlling the experiment page's live-update poll."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    poll_interval_ms: int = 30000
    """
    How often the browser checks whether an open experiment has changed. Each check is one indexed
    primary-key read, cheap even with many concurrent viewers -- turn this down (or up) for a
    heavily-loaded multi-user deployment without a code change.
    """

    tick_interval_ms: int = 10000
    """How often the "fetched Xs ago" label re-renders. Purely client-side (see
    `LIVE_TICK_INTERVAL_ID`) -- this never touches the server, so it's fine to leave far more
    frequent than `poll_interval_ms`."""


def live_status_badge(*, ok: bool) -> Component:
    """
    Small colored chip reporting whether the live-update poll (`poll_for_updates`) is healthy.

    Stateless by design: a single failed poll is enough to flip it red, and it self-clears green on
    the very next successful one -- no failure-count/session state to track or leak, matching this
    feature's "don't trust clients, don't hold server-side connection state" approach.
    """
    return (
        dmc.Badge("Live", color="green", variant="dot", size="sm", tt="none")
        if ok
        else dmc.Badge("Reconnecting", color="red", variant="dot", size="sm", tt="none")
    )


def _fetch_open_panel_dataframes(
    store: DataStore[...],
    experiment_id: int,
    page: page_model.BasicExperimentPage,
    chart_ids: list[chart_view.ChartID],
) -> tuple[dict[str, pd.DataFrame], bool]:
    """
    Fetch the dataframe of each panel with charts on screen, for just those charts -- once per panel.

    Not once per chart: "Auto-generate charts" routinely groups several charts sharing a panel, so
    fetching per panel (rather than per chart) keeps a poll tick's cost proportional to how many
    panels are open. And not for the panel's other charts: only one page of them is on screen (see
    `Paging`), so a tick costs what that page does, however many charts the panel holds.
    """
    run_ids = shown_run_ids(store, experiment_id, page.document)
    panels_by_name = {p.name: p for p in page.panels}
    mounted: dict[str, set[int]] = {}
    for chart_id in chart_ids:
        if chart_id["index"] is not None:
            mounted.setdefault(chart_id["panel"], set()).add(chart_id["index"])
    dataframes: dict[str, pd.DataFrame] = {}
    ok = True
    for panel_name, indexes in mounted.items():
        panel = panels_by_name.get(panel_name)
        if panel is None:
            continue
        shown = panel.model_copy(update={"charts": [c for i, c in enumerate(panel.charts) if i in indexes]})
        try:
            dataframes[panel_name] = panel_data.fetch_panel_dataframe(store, experiment_id, shown, run_ids)
        except Exception:  # noqa: BLE001 -- one bad panel must not break the whole poll tick
            _log.exception("Live-update poll failed to fetch panel %r", panel_name)
            ok = False
    return dataframes, ok


def _refresh_chart_bodies(
    chart_ids: list[chart_view.ChartID],
    panels_by_name: dict[str, models.PanelInstance[Any, Any]],
    dataframes: dict[str, pd.DataFrame],
    shown: dict[tuple[str, int | None], str],
    page_size: int,
) -> tuple[list[Any], bool]:
    """
    The new body of each chart whose data is no longer what its on-screen body was drawn from.

    `shown` is the `chart_data_fingerprint` each chart's body carries (`chart_body`); one that still
    matches what the data says now is left alone (`no_update`).
    """
    contents: list[Any] = []
    ok = True
    for chart_id in chart_ids:
        panel_name, index = chart_id["panel"], chart_id["index"]
        panel = panels_by_name.get(panel_name)
        df = dataframes.get(panel_name)
        if panel is None or df is None or index is None or index >= len(panel.charts):
            contents.append(no_update)
            continue
        try:
            if panel_data.chart_data_fingerprint(panel.charts[index], df) == shown.get((panel_name, index)):
                contents.append(no_update)
            else:
                contents.append(chart_view.chart_body(panel, index, df, page_size))
        except Exception:  # noqa: BLE001 -- one bad chart must not break the whole poll tick
            _log.exception("Live-update poll failed to refresh chart %r", f"{panel_name}:{index}")
            contents.append(no_update)
            ok = False
    return contents, ok


def register_live_update_callbacks(app: Dash) -> None:
    """The live-update poll, and the clientside switch and clock beside it."""

    # --- live updates: a cheap poll of `Experiment.revision` re-renders only the individual open
    # charts whose own data actually changed (never touching the accordion/tabs shell, closed
    # panels, or sibling charts sharing a panel), so new data appears without any page/panel-level
    # flash a coarser rebuild would cause. Also refreshes `STATE_HPARAMS`, since the navbar
    # run-comparison table (`_run_comparison_table.py`) reads run/hparam data from it, not from a
    # live store query of its own.
    #
    # Each open *panel* is still fetched only once per tick (`fetch_panel_dataframe`, the same call
    # the initial render makes) -- "Auto-generate charts" routinely groups several charts sharing a
    # panel, and fetching once per panel rather than once per chart keeps this poll's per-tick cost
    # proportional to how many panels are open, not how many charts they hold. Each chart's own
    # `chart_data_fingerprint` (scoped to just its own columns of that shared dataframe) is what
    # decides whether *that* chart's body actually needs to be replaced -- compared with the one the body
    # on screen carries, so a chart that was just mounted is not redrawn for nothing. ---
    @app.callback(
        Output(chart_view.CHART_BODY.all(), "children"),
        Output(ids.STATE_HPARAMS, "data", allow_duplicate=True),
        Output(STATE_LAST_KNOWN_REVISION, "data", allow_duplicate=True),
        Output(STATE_LAST_FETCH_AT, "data", allow_duplicate=True),
        Output(LIVE_STATUS_ID, "children"),
        Output(STATE_NOTES_REVISION, "data"),
        Input(LIVE_POLL_INTERVAL_ID, "n_intervals"),
        State(chart_view.CHART_BODY.all(), "id"),
        State(chart_view.CHART_FINGERPRINT.all(), "id"),
        State(chart_view.CHART_FINGERPRINT.all(), "children"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(ids.STATE_VIEW_ID, "data"),
        State(STATE_LAST_KNOWN_REVISION, "data"),
        State(STATE_NOTES_REVISION, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def poll_for_updates(  # noqa: PLR0913
        _n_intervals: int,
        chart_ids: list[chart_view.ChartID],
        fingerprint_ids: list[chart_view.ChartID],
        fingerprints: list[str],
        experiment_id: int,
        view_id: int | None,
        last_known_revision: int | None,
        last_notes_revision: int | None,
        paging_json: str,
    ) -> tuple[list[Any], Any, Any, Any, Component, Any]:
        store = get_data_store()
        ok = True
        try:
            experiment = store.get_experiment(experiment_id)
        except Exception:  # noqa: BLE001 -- a poll failure is reported via the status badge, not raised
            _log.exception("Live-update poll failed for experiment %s", experiment_id)
            experiment, ok = None, False

        # Set on every tick that actually reached the store, whether or not anything had changed --
        # "how stale could this be" (`LIVE_LAST_FETCH_LABEL_ID`, ticked purely client-side) needs the
        # time of the last real check, not the time of the last real change.
        fetched_at = pendulum.now("UTC").isoformat() if ok else no_update
        # Notes changed (posted/deleted) since the last tick: tells an open notes thread to refresh,
        # from the same one-row read -- and independently of whether any chart data changed.
        notes_revision = (
            experiment.notes_revision
            if experiment is not None and experiment.notes_revision != last_notes_revision
            else no_update
        )

        if not ok or experiment is None or experiment.revision == last_known_revision:
            return (
                [no_update] * len(chart_ids),
                no_update,
                no_update,
                fetched_at,
                live_status_badge(ok=ok),
                notes_revision,
            )

        page = repo.load_page(store, repo.PageRef(experiment_id, view_id))
        panels_by_name = {p.name: p for p in page.panels}
        dataframes, fetch_ok = _fetch_open_panel_dataframes(store, experiment_id, page, chart_ids)
        shown = {
            (i["panel"], i["index"]): fingerprint
            for i, fingerprint in zip(fingerprint_ids, fingerprints, strict=True)
        }
        contents, render_ok = _refresh_chart_bodies(
            chart_ids, panels_by_name, dataframes, shown, Paging.model_validate_json(paging_json).sizes.charts
        )
        poll_ok = fetch_ok and render_ok

        try:
            hparams_json: Any = [
                h.model_dump_json() for h in store.fetch_hyperparams(experiment_id, run_ids=None)
            ]
        except Exception:  # noqa: BLE001 -- charts refreshed above still ship even if this fails
            _log.exception(
                "Live-update poll failed to refresh hyperparameters for experiment %s", experiment_id
            )
            hparams_json, poll_ok = no_update, False

        return (
            contents,
            hparams_json,
            experiment.revision,
            fetched_at,
            live_status_badge(ok=poll_ok),
            notes_revision,
        )

    # Both pure client-side, deliberately never round-tripping to the server -- see
    # `live_switch_state.js`/`live_last_fetch_label.js` and the ids' own docstrings above.
    app.clientside_callback(
        _LIVE_SWITCH_STATE_JS.source,
        Output(LIVE_POLL_INTERVAL_ID, "disabled"),
        Output(LIVE_STATUS_ID, "style"),
        Output(LIVE_PAUSED_BADGE_ID, "style"),
        Input(LIVE_UPDATES_ENABLED_ID, "checked"),
    )
    app.clientside_callback(
        _LIVE_LAST_FETCH_LABEL_JS.source,
        Output(LIVE_LAST_FETCH_LABEL_ID, "children"),
        Input(LIVE_TICK_INTERVAL_ID, "n_intervals"),
        State(STATE_LAST_FETCH_AT, "data"),
    )


def live_controls() -> list[Component]:
    """The status badge (or "Paused" in its place), the "fetched Xs ago" label, and the on/off switch."""
    return [
        html.Div(live_status_badge(ok=True), id=LIVE_STATUS_ID),
        # Hidden until the switch below is unchecked -- a plain CSS visibility swap with the div
        # above (see `live_switch_state.js`), not a second callback writing that div's `children`.
        html.Div(
            dmc.Badge("Paused", color="gray", variant="dot", size="sm", tt="none"),
            id=LIVE_PAUSED_BADGE_ID,
            style={"display": "none"},
        ),
        dmc.Text("Fetched just now", id=LIVE_LAST_FETCH_LABEL_ID, size="xs", c="dimmed"),
        dmc.Tooltip(
            # `persistence`/`persistence_type="local"` is the *entire* mechanism here -- Dash writes
            # `checked` straight to the browser's own `localStorage` keyed by this component's id, a
            # per-browser viewing preference. No `dcc.Store`, no server round trip, and nothing shared
            # with other viewers of the same experiment.
            dmc.Switch(
                id=LIVE_UPDATES_ENABLED_ID,
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
    ]


def live_timers(settings: LiveUpdateSettings, revision: int) -> list[Interval | Store]:
    """The two intervals that drive the poll and the label's clock, and the stores they read and write."""
    return [
        # Drives `poll_for_updates`: each tick does one indexed PK read of `Experiment.revision`, only
        # paying for a real refresh when it's actually changed since the last poll. `disabled` is
        # flipped client-side by `LIVE_UPDATES_ENABLED_ID` (`live_switch_state.js`) -- unchecking it
        # genuinely stops this from firing at all, not just from being acted on.
        Interval(id=LIVE_POLL_INTERVAL_ID, interval=settings.poll_interval_ms),
        # Drives the "Fetched Xs ago" label (`live_last_fetch_label.js`) -- purely client-side, so this
        # stays enabled (and ticking, showing growing staleness) even while the poll above is off.
        Interval(id=LIVE_TICK_INTERVAL_ID, interval=settings.tick_interval_ms),
        Store(id=STATE_LAST_KNOWN_REVISION, data=revision),
        Store(id=STATE_LAST_FETCH_AT, data=pendulum.now("UTC").isoformat()),
    ]
