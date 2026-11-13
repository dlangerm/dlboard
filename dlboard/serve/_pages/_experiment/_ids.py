"""
The experiment page's own page tag, and the ids every part of it shares: the page bus.

An id belongs here only if more than one feature reads or writes it. One that only a single feature's
markup or callbacks use lives with that feature (`_panel_view.py`, `_chart_view.py`, `_panel_controls.py`, ...).
"""

from __future__ import annotations

from dlboard.serve._component_ids import DivId, StoreId


class ExperimentPage:
    """Page tag: marks a component id as belonging to the experiment page."""


# --- route-skeleton ids: the page `serve/_pages/experiment.py` (and `comparison.py`) lays out. ---
PAGE_EXPERIMENT_ID: DivId[ExperimentPage] = DivId("experiment-container")
METRIC_CONTENT_ID: DivId[ExperimentPage] = DivId("metrics-view")
STATE_PAGE_STORAGE: StoreId[ExperimentPage] = StoreId("current-page")
STATE_VIEW_ID: StoreId[ExperimentPage] = StoreId("view-id-state")
"""The named view (`Page.id`) this page is showing, from its `?view=` URL, or `None` for the shared page."""
RUN_LIST_PAGE_ID: StoreId[ExperimentPage] = StoreId("run-list-page")
"""
`STATE_PAGE_STORAGE`, passed on to the run list only when it may change it (`run_list_gate.js`): not for a page an action
already brought the list up to date with (`RUN_LIST_HANDLED_ID`), nor on a comparison's page, which has no such list.
"""
RUN_LIST_HANDLED_ID: StoreId[ExperimentPage] = StoreId("run-list-handled")
"""The page (as JSON) an action wrote along with the run list's rows and head itself, which the run list need not read again."""

PANEL_PAGING_ID: StoreId[ExperimentPage] = StoreId("panel-paging")
"""
The `Paging` (as JSON) of what the panel area is showing.

Part of `accordion_view`'s output, so it is always the paging of the tree it sits in. Every callback that
rebuilds that tree takes it as a `State` and hands it back to `accordion_view`, so a rebuild stays on the
page the viewer was looking at.
"""

CHART_VIEW_SAVE_REQUEST_ID: StoreId[ExperimentPage] = StoreId("chart-view-save-request")
"""
What a card's "Save as default" asks the server to save: the chart's id and the viewer state (`chart_view_state.js`).

Written by the browser, with no callback of its own -- the card's bar names this store, which is why the card (`_chart_view.py`)
and the feature that saves it (`_chart_view_state.py`) share it.
"""
