"""
"Save as default" for what a viewer changed in a chart without editing it: the server side of a card's view bar.

What is changed, and the bar that offers to save it, is the browser's (`chart_view_state.js`, drawn by
`_chart_view.py`); this saves it as the chart's own settings through the chart type (`ChartType.with_viewer_state`),
which forks the page into the viewer's own view if it is not theirs, like any other edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from dash import Dash, Input, NoUpdate, Output, State, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, ValidationError
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.serve import ClientsideScript
from dlboard.serve._component_ids import StoreId
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model

if TYPE_CHECKING:
    from dash.development.base_component import Component

_log = get_logger(__name__)

_SAVED_JS = ClientsideScript(Path(__file__).with_name("chart_view_state_saved.js"))

CHART_VIEW_SAVED_ID: StoreId[ids.ExperimentPage] = StoreId("chart-view-saved")
"""The id of the chart whose viewer state was just saved as its own settings: the browser forgets that state."""
CHART_VIEW_FORGOTTEN_ID: StoreId[ids.ExperimentPage] = StoreId("chart-view-forgotten")
"""The id of the chart whose browser-kept state was just forgotten. Nothing reads it: a clientside callback needs an output."""


class SaveViewRequest(BaseModel, frozen=True):
    """
    What a card's "Save as default" sends: the chart, and the state the browser kept for it.

    It also sends the time, which this ignores: it makes asking twice for the same thing two different requests.
    """

    chart_id: str
    state: dict[str, Any]


def chart_view_state_stores() -> list[Component]:
    """The stores the browser and the server pass a save through, for the experiment page to render once."""
    return [
        Store(id=ids.CHART_VIEW_SAVE_REQUEST_ID),
        Store(id=CHART_VIEW_SAVED_ID),
        Store(id=CHART_VIEW_FORGOTTEN_ID),
    ]


def register_chart_view_state_callbacks(app: Dash) -> None:
    """Save a card's viewer state as the chart's own, and tell the browser it is no longer a change."""

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(CHART_VIEW_SAVED_ID, "data"),
        Input(ids.CHART_VIEW_SAVE_REQUEST_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def save_chart_view(
        request: dict[str, Any] | None, page_json: str, paging_json: str
    ) -> tuple[Any, Any, str | NoUpdate]:
        if not request:
            raise PreventUpdate
        saving = SaveViewRequest.model_validate(request)

        def apply(panels: list[Any]) -> list[Any]:
            location = page_model.locate_chart(panels, saving.chart_id)
            if location is None:
                return panels
            saved = models.ChartTypeRegistry.saved_with_viewer_state(location.chart, saving.state)
            return [page_model.upsert_chart(p, location.panel.name, location.index, saved) for p in panels]

        try:
            page, container = edits.commit(page_json, paging_json, edits.PageEdit(panels=apply))
        except (ValidationError, ValueError):
            _log.exception("a viewer's change to a chart could not be saved", chart_id=saving.chart_id)
            return no_update, no_update, no_update
        return container, page.model_dump_json(), saving.chart_id

    app.clientside_callback(
        _SAVED_JS.source,
        Output(CHART_VIEW_FORGOTTEN_ID, "data"),
        Input(CHART_VIEW_SAVED_ID, "data"),
        prevent_initial_call=True,
    )
