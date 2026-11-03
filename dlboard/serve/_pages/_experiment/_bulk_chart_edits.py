"""
Bulk delete and bulk edit of the charts a panel's chart filter matches.

A panel with many charts is found a few at a time with its chart filter; the same filter says which charts to change or
delete at once. Both are menu items of the panel, ask first (saying how many charts they reach), and need a filter: with none
they would reach every chart of the panel, which deleting the panel says better. Both are an edit of the page like any other
(`PageEdit`), so a viewer who does not own the page makes theirs in a copy of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, ValidationError

from dlboard import models
from dlboard.serve import get_data_store
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import busy_while_running, require_triggered_id
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog
from dlboard.serve._pages._experiment._paging import Paging, chart_matches
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

BULK_DELETE_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("bulk-delete-charts-modal")
BULK_DELETE_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("bulk-delete-charts-target")
BULK_DELETE_BODY_ID: DivId[ids.ExperimentPage] = DivId("bulk-delete-charts-body")
BULK_DELETE_CONFIRM_ID: ButtonId[ids.ExperimentPage] = ButtonId("bulk-delete-charts-confirm")
BULK_DELETE_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("bulk-delete-charts-cancel")

BULK_EDIT_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("bulk-edit-charts-modal")
BULK_EDIT_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("bulk-edit-charts-target")
BULK_EDIT_BODY_ID: DivId[ids.ExperimentPage] = DivId("bulk-edit-charts-body")
BULK_EDIT_X_AXIS_ID: ValueId[ids.ExperimentPage] = ValueId("bulk-edit-charts-x-axis")
BULK_EDIT_APPLY_ID: ButtonId[ids.ExperimentPage] = ButtonId("bulk-edit-charts-apply")
BULK_EDIT_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("bulk-edit-charts-cancel")

_X_AXIS: Final = "x_axis"
"""The one setting edited in bulk, named as the chart types that have it name it."""
_STEP = "step"


class BulkTarget(BaseModel, frozen=True, extra="forbid"):
    """The charts of one panel that its chart filter matches: what a bulk action asked about reaches."""

    panel: str
    query: str


class _BulkDeleteCtx(edits.EditCtx):
    target: Annotated[BulkTarget | None, State(BULK_DELETE_TARGET_ID, "data")]


class _BulkEditCtx(edits.EditCtx):
    target: Annotated[BulkTarget | None, State(BULK_EDIT_TARGET_ID, "data")]
    x_axis: Annotated[str | None, State(BULK_EDIT_X_AXIS_ID, "value")]


def bulk_chart_modals() -> list[Any]:
    """The confirmation of a bulk delete and the form of a bulk edit, for the page to render once, beside the panel area."""
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
        dmc.Modal(
            id=BULK_EDIT_MODAL_ID,
            title="Set the x-axis of these charts",
            opened=False,
            children=[
                Store(id=BULK_EDIT_TARGET_ID),
                dmc.Stack(
                    [
                        dmc.Text(id=BULK_EDIT_BODY_ID, size="sm"),
                        dmc.TextInput(
                            id=BULK_EDIT_X_AXIS_ID,
                            label="X-axis",
                            description="A metric, a hyperparameter, or step",
                            placeholder=_STEP,
                        ),
                        dmc.Group(
                            [
                                dmc.Button("Cancel", id=BULK_EDIT_CANCEL_ID, variant="default"),
                                dmc.Button("Apply", id=BULK_EDIT_APPLY_ID),
                            ],
                            justify="flex-end",
                        ),
                    ]
                ),
            ],
        ),
    ]


def _matching(panels: Sequence[models.PanelInstance[Any, Any]], target: BulkTarget) -> list[Any]:
    """The charts of `target`'s panel its query matches."""
    panel = next((p for p in panels if p.name == target.panel), None)
    return [] if panel is None else [c for c in panel.charts if chart_matches(c, target.query)]


def _edit_matching(
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


def _has_x_axis(chart: models.ChartInstance[Any, Any]) -> bool:
    try:
        return (
            _X_AXIS in models.ChartTypeRegistry.get_chart_type(chart.chart_type).parameter_type().model_fields
        )
    except models.UnknownChartTypeError:
        return False


def _with_x_axis(chart: models.ChartInstance[Any, Any], x_axis: str) -> models.ChartInstance[Any, Any]:
    """`chart` on `x_axis`, or as it was if it has none, or its other settings do not allow that one."""
    if not _has_x_axis(chart):
        return chart
    parameters = {**chart.parameters, _X_AXIS: x_axis}
    try:
        models.ChartTypeRegistry.get_chart_type(chart.chart_type).parameter_type().model_validate(parameters)
    except ValidationError:
        return chart
    return chart.model_copy(update={"parameters": parameters})


def _notice(message: str) -> list[dict[str, Any]]:
    return [
        {
            "action": "show",
            "id": f"bulk-charts-{message}",
            "title": "No charts to change",
            "message": message,
            "color": "yellow",
            "autoClose": 5_000,
        }
    ]


def _asked(page_json: str, paging_json: str) -> tuple[BulkTarget, list[Any]]:
    """The panel whose menu item was clicked, its chart filter, and the charts that matches."""
    panel = require_triggered_id(panel_view.PanelID).panel
    target = BulkTarget(panel=panel, query=Paging.model_validate_json(paging_json).chart_paging(panel).q)
    page = page_model.BasicExperimentPage.model_validate_json(page_json)
    return target, _matching(page.panels, target)


def _charts(count: int) -> str:
    return "1 chart" if count == 1 else f"{count} charts"


def _register_delete(app: Dash) -> None:
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
        target, matching = _asked(page_json, paging_json)
        if not target.query.strip():
            return (
                False,
                no_update,
                no_update,
                _notice("Type in the panel's chart filter first: it says which charts."),
            )
        if not matching:
            return (
                False,
                no_update,
                no_update,
                _notice(f"No chart of {target.panel} matches {target.query!r}."),
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
            ctx.page_json, ctx.paging_json, edits.PageEdit(panels=_edit_matching(target, lambda _chart: []))
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


def _register_edit(app: Dash) -> None:
    @app.callback(
        Output(BULK_EDIT_MODAL_ID, "opened", allow_duplicate=True),
        Output(BULK_EDIT_TARGET_ID, "data"),
        Output(BULK_EDIT_BODY_ID, "children"),
        Output(BULK_EDIT_X_AXIS_ID, "error"),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        Input(panel_view.BULK_EDIT_BUTTON.all(), "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def open_bulk_edit(
        _clicks: list[int | None], page_json: str, paging_json: str
    ) -> tuple[bool | NoUpdate, Any, Any, Any, Any]:
        target, matching = _asked(page_json, paging_json)
        if not target.query.strip():
            return (
                False,
                no_update,
                no_update,
                no_update,
                _notice("Type in the panel's chart filter first: it says which charts."),
            )
        with_x_axis = [chart for chart in matching if _has_x_axis(chart)]
        if not with_x_axis:
            return (
                False,
                no_update,
                no_update,
                no_update,
                _notice(f"No chart of {target.panel} that matches {target.query!r} has an x-axis."),
            )
        body = (
            f"Sets the x-axis of the {_charts(len(with_x_axis))} of {target.panel} that match {target.query!r}"
            + (
                f" (the other {len(matching) - len(with_x_axis)} have none)."
                if len(matching) > len(with_x_axis)
                else "."
            )
        )
        return True, target.model_dump(), body, "", no_update

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(BULK_EDIT_MODAL_ID, "opened", allow_duplicate=True),
        Output(BULK_EDIT_X_AXIS_ID, "error", allow_duplicate=True),
        Input(BULK_EDIT_APPLY_ID, "n_clicks"),
        _BulkEditCtx.states(),
        prevent_initial_call=True,
        running=busy_while_running(BULK_EDIT_APPLY_ID),
    )
    def bulk_edit(
        n_clicks: int, states: dict[str, Any]
    ) -> tuple[html.Div | NoUpdate, str | NoUpdate, bool | NoUpdate, str]:
        ctx = _BulkEditCtx.model_validate(states)
        target, x_axis = ctx.target, (ctx.x_axis or "").strip()
        if not n_clicks or target is None:
            raise PreventUpdate
        page = page_model.BasicExperimentPage.model_validate_json(ctx.page_json)
        catalog = ColumnCatalog.of_page(get_data_store(), page)
        if x_axis not in {_STEP, *catalog.metrics, *catalog.hparams}:
            return no_update, no_update, no_update, "Not a metric or hyperparameter of this page's runs"

        saved, container = edits.commit(
            ctx.page_json,
            ctx.paging_json,
            edits.PageEdit(panels=_edit_matching(target, lambda chart: [_with_x_axis(chart, x_axis)])),
        )
        return container, saved.model_dump_json(), False, ""

    @app.callback(
        Output(BULK_EDIT_MODAL_ID, "opened", allow_duplicate=True),
        Input(BULK_EDIT_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_bulk_edit(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def register_bulk_chart_edits(app: Dash) -> None:
    _register_delete(app)
    _register_edit(app)
