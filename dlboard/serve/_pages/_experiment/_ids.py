"""
The experiment page's own page tag, and the ids every part of it shares: the page bus.

An id belongs here only if more than one feature reads or writes it. One that only a single feature's
markup or callbacks use lives with that feature (`_panel_view.py`, `_chart_view.py`, `_panel_controls.py`, ...).
"""

from __future__ import annotations

from dlboard.serve._component_ids import DivId, StoreId


class ExperimentPage:
    """Page tag: marks a component id as belonging to the experiment page."""


# --- route-skeleton ids: a two-file contract with `serve/_pages/experiment.py`, which imports
# these from `_experiment/__init__.py` (this package's public façade) rather than from here. ---
PAGE_EXPERIMENT_ID: DivId[ExperimentPage] = DivId("experiment-container")
EXPERIMENT_HEADER_ID: DivId[ExperimentPage] = DivId("experiment-header")
METRIC_CONTENT_ID: DivId[ExperimentPage] = DivId("metrics-view")
STATE_HPARAMS: StoreId[ExperimentPage] = StoreId("hparams-state")
STATE_PAGE_STORAGE: StoreId[ExperimentPage] = StoreId("current-page")
STATE_VIEW_ID: StoreId[ExperimentPage] = StoreId("view-id-state")
"""The named view (`Page.id`) this page is showing, from its `?view=` URL, or `None` for the shared page."""

PANEL_PAGING_ID: StoreId[ExperimentPage] = StoreId("panel-paging")
"""
The `Paging` (as JSON) of what the panel area is showing.

Part of `accordion_view`'s output, so it is always the paging of the tree it sits in. Every callback that
rebuilds that tree takes it as a `State` and hands it back to `accordion_view`, so a rebuild stays on the
page the viewer was looking at.
"""
