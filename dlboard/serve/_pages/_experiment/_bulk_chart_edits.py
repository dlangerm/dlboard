"""
Bulk delete of the charts a panel's chart filter matches.

A panel with many charts is found a few at a time with its chart filter; the same filter says which charts to delete at
once. It is a menu item of the panel, asks first (saying how many charts it reaches), and needs a filter: with none it
would reach every chart of the panel, which deleting the panel says better. It is an edit of the page like any other
(`PageEdit`), so a viewer who does not own the page makes theirs in a copy of it. Changing the settings of the charts a
filter matches is the panel's "Chart settings…" (`_chart_settings_layers.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel

from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId
from dlboard.serve._pages._dash_helpers import busy_while_running, notice, require_triggered_id
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._paging import Paging, chart_matches
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from dlboard import models

BULK_DELETE_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("bulk-delete-charts-modal")
BULK_DELETE_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("bulk-delete-charts-target")
BULK_DELETE_BODY_ID: DivId[ids.ExperimentPage] = DivId("bulk-delete-charts-body")
BULK_DELETE_CONFIRM_ID: ButtonId[ids.ExperimentPage] = ButtonId("bulk-delete-charts-confirm")
BULK_DELETE_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("bulk-delete-charts-cancel")


class BulkTarget(BaseModel, frozen=True, extra="forbid"):
    """The charts of one panel that its chart filter matches: what a bulk action asked about reaches."""

    panel: str
    query: str


class _BulkDeleteCtx(edits.EditCtx):
    target: Annotated[BulkTarget | None, State(BULK_DELETE_TARGET_ID, "data")]


def bulk_chart_modals() -> list[Any]:
    """The confirmation of a bulk delete, for the page to render once, beside the panel area."""
    return [
        dmc.Modal(
            id=BULK_DELETE_MODAL_ID,
            title="Delete these charts?",
            opened=False,
            children=[
                Store(id=BULK_DELETE_TARGET_ID),
                dmc.Text(id=BULK_DELETE_BODY_ID),
                dmc.Group(
                    [
                        dmc.Button("Cancel", id=BULK_DELETE_CANCEL_ID, variant="default"),
                        dmc.Button("Delete", id=BULK_DELETE_CONFIRM_ID, color="red"),
                    ],
                    justify="flex-end",
                    mt="sm",
                ),
            ],
        ),
    ]


def _matching(panels: Sequence[models.PanelInstance[Any, Any]], target: BulkTarget) -> list[Any]:
    """The charts of `target`'s panel its query matches."""
    panel = next((p for p in panels if p.name == target.panel), None)
    return [] if panel is None else [c for c in panel.charts if chart_matches(c, target.query)]


def edit_matching(
    target: BulkTarget,
    change: Callable[[models.ChartInstance[Any, Any]], list[models.ChartInstance[Any, Any]]],
) -> edits.PanelsEdit:
    """An edit of the panels: each chart of `target`'s panel its query matches is replaced by what `change` gives (none, to delete it)."""

    def edit(panels: list[models.PanelInstance[Any, Any]]) -> list[models.PanelInstance[Any, Any]]:
        return [
            p
            if p.name != target.panel
            else p.model_copy(
                update={
                    "charts": [
                        new
                        for chart in p.charts
                        for new in (change(chart) if chart_matches(chart, target.query) else [chart])
                    ]
                }
            )
            for p in panels
        ]

    return edit


def _charts(count: int) -> str:
    return "1 chart" if count == 1 else f"{count} charts"


def register_bulk_chart_edits(app: Dash) -> None:
    @app.callback(
        Output(BULK_DELETE_MODAL_ID, "opened", allow_duplicate=True),
        Output(BULK_DELETE_TARGET_ID, "data"),
        Output(BULK_DELETE_BODY_ID, "children"),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        Input(panel_view.BULK_DELETE_BUTTON.all(), "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def open_bulk_delete(
        _clicks: list[int | None], page_json: str, paging_json: str
    ) -> tuple[bool | NoUpdate, Any, Any, Any]:
        panel = require_triggered_id(panel_view.PanelID).panel
        target = BulkTarget(panel=panel, query=Paging.model_validate_json(paging_json).chart_paging(panel).q)
        matching = _matching(page_model.BasicExperimentPage.model_validate_json(page_json).panels, target)
        if not target.query.strip():
            return (
                False,
                no_update,
                no_update,
                notice(
                    "No charts to change", "Type in the panel's chart filter first: it says which charts."
                ),
            )
        if not matching:
            return (
                False,
                no_update,
                no_update,
                notice("No charts to change", f"No chart of {target.panel} matches {target.query!r}."),
            )
        body = (
            f"This deletes the {_charts(len(matching))} of {target.panel} that match {target.query!r}. "
            "This can't be undone."
        )
        return True, target.model_dump(), body, no_update

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(BULK_DELETE_MODAL_ID, "opened", allow_duplicate=True),
        Input(BULK_DELETE_CONFIRM_ID, "n_clicks"),
        _BulkDeleteCtx.states(),
        prevent_initial_call=True,
        running=busy_while_running(BULK_DELETE_CONFIRM_ID),
    )
    def bulk_delete(
        n_clicks: int, states: dict[str, Any]
    ) -> tuple[html.Div | NoUpdate, str | NoUpdate, bool]:
        ctx = _BulkDeleteCtx.model_validate(states)
        target = ctx.target
        if not n_clicks or target is None:
            raise PreventUpdate

        page, container = edits.commit(
            ctx.page_json, ctx.paging_json, edits.PageEdit(panels=edit_matching(target, lambda _chart: []))
        )
        return container, page.model_dump_json(), False

    @app.callback(
        Output(BULK_DELETE_MODAL_ID, "opened", allow_duplicate=True),
        Input(BULK_DELETE_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_bulk_delete(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False
