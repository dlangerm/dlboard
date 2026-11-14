"""
The panel area: tabs, panels and charts, the chart editor docked beside them, and every modal they open.

What an experiment's page and a comparison's both put under their header, and register once for both. The pieces here
go in the page itself, not in what a callback rebuilds (`render_panel_area`, in `_page_edits.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
from dash import dcc, html
from dash.dcc import Store

from dlboard.serve import AssetKind, serve_asset
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _panel_controls as panel_controls
from dlboard.serve._pages._experiment import _tab_controls as tab_controls
from dlboard.serve._pages._experiment._chart_editor import chart_editor_dock, register_chart_editor_callbacks
from dlboard.serve._pages._experiment._chart_suggestions import (
    chart_suggestion_shells,
    register_chart_suggestions_callbacks,
)
from dlboard.serve._pages._experiment._chart_view_state import (
    chart_view_state_stores,
    register_chart_view_state_callbacks,
)
from dlboard.serve._pages._experiment._column_catalog import COMPARISON_CATALOGS, KEPT_CATALOGS
from dlboard.serve._pages._experiment._kept_reads import KeptReads
from dlboard.serve._pages._experiment._panel_area_controls import register_panel_area_callbacks
from dlboard.serve._pages._experiment._views import VIEW_SYNC_REQUEST_ID

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

_ASSETS = [
    (AssetKind.STYLESHEET, Path(__file__).with_name("_experiment_page_hover.css")),
    (AssetKind.SCRIPT, Path(__file__).with_name("_experiment_page_dragdrop.js")),
    (AssetKind.SCRIPT, Path(__file__).with_name("chart_deep_link.js")),
    (AssetKind.SCRIPT, Path(__file__).with_name("chart_view_state.js")),
]

# One dcc.Store per drag-drop gesture a completed drag can report (`_experiment_page_dragdrop.js`,
# via `set_props`) -- listed in the static layout below, not `accordion_view`'s render tree, so the
# drop target always exists regardless of what's currently rendered underneath it.
_DRAG_DROP_STORE_IDS = [
    panel_controls.PANEL_REORDER_STORE_ID,
    panel_controls.CHART_REORDER_STORE_ID,
    tab_controls.TAB_DROP_STORE_ID,
    tab_controls.CHART_TAB_DROP_STORE_ID,
    panel_controls.CHART_PANEL_MOVE_STORE_ID,
]


def panel_area(container: Component) -> list[Component]:
    """The panel area (`container`, from `render_panel_area`), the chart editor docked beside it, and every modal it can open."""
    return [
        # The panel area and the chart editor docked beside it, so the charts stay in view while one is edited
        # (`_experiment_page_hover.css`). The dock is rendered here once, outside `METRIC_CONTENT_ID`: every
        # structural edit rebuilds that.
        #
        # `container` is real content from the very first response, not a `dmc.Loader()` placeholder.
        # `dcc.Loading` wraps it for every *later* full-panel rebuild (add/delete/rename a panel or chart,
        # apply suggestions, ...), and `custom_spinner` matches those rebuilds' spinner to the app's own
        # `dmc.Loader()` rather than `dcc.Loading`'s unstyled default. `target_components` is required now that
        # live updates poll in the background: without it the overlay shows for *any* in-flight callback touching
        # a descendant, which includes `poll_for_updates`'s per-chart patches -- a stop-the-world flash on every
        # tick. Scoped to this div's own `children`, the spinner covers only a genuine full-panel rebuild. Its
        # generated type stub is an empty `TypedDict` (a known Dash codegen gap), so `cast` to `Any`, as
        # `_panel_view.py` does for its `data-*` attributes.
        html.Div(
            [
                html.Div(
                    cast("Any", dcc.Loading)(  # pyrefly: ignore [unknown-argument-type]
                        html.Div(container, id=ids.METRIC_CONTENT_ID),
                        delay_show=250,
                        target_components={str(ids.METRIC_CONTENT_ID): "children"},
                        custom_spinner=dmc.Loader(size="lg"),
                    ),
                    className="dl-experiment-main",
                ),
                chart_editor_dock(),
            ],
            className="dl-experiment-body",
        ),
        # Every modal the panel area can open, each owned by its feature module. Rendered
        # once, outside `METRIC_CONTENT_ID`: every structural edit rebuilds that, and each re-mount
        # of a shell set its confirm button's callback off again -- a burst of round trips that
        # answered nothing but delayed the render they came with. So each confirm closes its own.
        *chart_suggestion_shells(),
        *panel_controls.panel_control_modals(),
        *tab_controls.tab_control_modals(),
    ]


def panel_area_stores() -> list[Component]:
    """
    The stores the panel area writes to, in the page itself rather than in `accordion_view`'s render tree.

    One per drag-drop gesture a completed drag can report (`_experiment_page_dragdrop.js`, via `set_props`), so the
    drop target always exists whatever is rendered underneath it; and what a chart's viewer state is saved with.
    """
    return [
        *(Store(id=store_id) for store_id in _DRAG_DROP_STORE_IDS),
        Store(id=VIEW_SYNC_REQUEST_ID),
        Store(id=ids.RUN_TOGGLE_ID),
        Store(id=ids.RUN_LIST_PAGE_ID),
        Store(id=ids.RUN_LIST_HANDLED_ID),
        *chart_view_state_stores(),
    ]


def register_panel_area(app: Dash) -> None:
    """Serve the panel area's own CSS and JS, and register the callbacks of every feature in it."""
    for kind, path in _ASSETS:
        serve_asset(app, kind, path.name, path.read_bytes())
    COMPARISON_CATALOGS.set(
        app, KeptReads(kept=KEPT_CATALOGS)
    )  # this app's own, like the chart editor's reads
    register_panel_area_callbacks(app)
    panel_controls.register_panel_controls_callbacks(app)
    tab_controls.register_tab_controls_callbacks(app)
    register_chart_editor_callbacks(app)
    register_chart_view_state_callbacks(app)
    register_chart_suggestions_callbacks(app)
