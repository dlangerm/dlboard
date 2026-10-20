"""
The add/edit-chart modal: its shell, live preview, and the submit that saves the chart.

The shell (`add_chart_modal`) is rendered once by the experiment page, beside the panel area; the
"Add chart" and "Edit chart" buttons that open it are part of the panel area's render tree. Its form is
`_chart_settings_form.py`'s.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any, Final, cast

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import ValidationError
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.serve import _constants as constants
from dlboard.serve import get_data_store
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import require_triggered_id
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._chart_settings_form import (
    CHART_PARAM,
    CHART_PARAM_TABLE_DATA,
    FIELDS_ROW,
    EditedChart,
    build_chart_param_form,
    build_validated_chart_instance,
    merge_chart_param_values,
    register_settings_form_callbacks,
)

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore

_log = get_logger(__name__)

ADD_CHART_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("add-chart-modal")
ADD_CHART_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("add-chart-target")
ADD_CHART_INITIAL_PARAMS_ID: StoreId[ids.ExperimentPage] = StoreId("add-chart-initial-params")
ADD_CHART_TYPE_SELECT_ID: ValueId[ids.ExperimentPage] = ValueId("add-chart-type-select")
ADD_CHART_PARAMS_ID: DivId[ids.ExperimentPage] = DivId("add-chart-params")
ADD_CHART_PREVIEW_ID: DivId[ids.ExperimentPage] = DivId("add-chart-preview")
ADD_CHART_ERROR_ID: DivId[ids.ExperimentPage] = DivId("add-chart-error")
ADD_CHART_SUBMIT_ID: ButtonId[ids.ExperimentPage] = ButtonId("add-chart-submit")
ADD_CHART_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("add-chart-cancel")


NEW_CHART_ID_BYTES: Final = 5
"""How many random bytes a new chart's id is made of (twice as many hex characters), enough that two never collide."""


class _AddChartCtx(edits.EditCtx):
    chart_type_name: str | None
    values: list[Any]
    checked_values: list[Any]
    field_ids: list[dict[str, str]]
    table_ids: list[dict[str, str]]
    table_values: list[Any]
    target: chart_view.ChartTargetData | None
    experiment_id: int


def add_chart_modal() -> dmc.Modal:
    """The add/edit-chart modal, for the experiment page to render once, beside the panel area."""
    chart_types = sorted(models.ChartTypeRegistry.get_registered_chart_types().keys())
    return dmc.Modal(
        id=ADD_CHART_MODAL_ID,
        title="Add chart",
        opened=False,
        size="lg",
        children=[
            Store(id=ADD_CHART_TARGET_ID),
            Store(id=ADD_CHART_INITIAL_PARAMS_ID),
            dmc.Stack(
                [
                    dmc.Select(id=ADD_CHART_TYPE_SELECT_ID, label="Chart type", data=chart_types, value=None),
                    html.Div(
                        id=ADD_CHART_PARAMS_ID,
                        style=FIELDS_ROW,
                    ),
                    dmc.Text("Preview", fw=600, mt="sm"),
                    html.Div(id=ADD_CHART_PREVIEW_ID),
                    dmc.Text(id=ADD_CHART_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=ADD_CHART_CANCEL_ID, variant="default"),
                            dmc.Button("Add chart", id=ADD_CHART_SUBMIT_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _register_open_close(app: Dash) -> None:
    # --- open the modal, either to add a new chart or edit an existing one ---
    @app.callback(
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_TARGET_ID, "data"),
        Output(ADD_CHART_TYPE_SELECT_ID, "value"),
        Output(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        Output(ADD_CHART_SUBMIT_ID, "loading", allow_duplicate=True),
        Input(panel_view.OPEN_CHART_BUTTON.all(), "n_clicks"),
        Input(chart_view.EDIT_CHART_BUTTON.all(), "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def open_chart_modal(
        _add_clicks: list[int], _edit_clicks: list[int], page_json: str
    ) -> tuple[bool, chart_view.ChartTargetData, str | None, dict[str, Any] | None, bool]:
        triggered_id = cast("chart_view.ChartID", require_triggered_id())

        if triggered_id["type"] == panel_view.OPEN_CHART_BUTTON.type:
            return True, {"panel": triggered_id["panel"], "index": None}, None, None, False

        curr_page = page_model.BasicExperimentPage.model_validate_json(page_json)
        panel = next(p for p in curr_page.panels if p.name == triggered_id["panel"])
        idx = triggered_id["index"]
        if idx is None:
            msg = f"Malformed edit-chart id: {triggered_id}"
            raise ValueError(msg)
        chart = panel.charts[idx]
        return (
            True,
            {"panel": triggered_id["panel"], "index": idx},
            chart.chart_type,
            EditedChart(
                chart_type=chart.chart_type, parameters=models.ChartTypeRegistry.current_parameters(chart)
            ).model_dump(),
            False,
        )

    @app.callback(
        Output(ADD_CHART_SUBMIT_ID, "children"),
        Output(ADD_CHART_MODAL_ID, "title"),
        Input(ADD_CHART_TARGET_ID, "data"),
        prevent_initial_call=True,
    )
    def set_modal_mode_labels(target: dict[str, Any] | None) -> tuple[str, str]:
        if not target:
            raise PreventUpdate
        if target.get("index") is None:
            return "Add chart", "Add chart"
        return "Save changes", "Edit chart"

    @app.callback(
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_SUBMIT_ID, "loading", allow_duplicate=True),
        Input(ADD_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def close_add_chart_modal(n_clicks: int) -> tuple[bool, bool]:
        if not n_clicks:
            raise PreventUpdate
        return False, False


def render_chart_preview(  # noqa: PLR0913
    chart_type_name: str | None,
    values: list[Any],
    checked_values: list[Any],
    field_ids: list[dict[str, str]],
    table_ids: list[dict[str, str]],
    table_values: list[Any],
    *,
    store: DataStore[...],
    ref: repo.PageRef,
) -> tuple[Any, str]:
    """
    Live-preview the chart exactly as it'd render in a panel of its own -- same fetch, same runs.

    Goes through `fetch_panel_dataframe` rather than any whole-experiment dataframe, so the preview
    only ever fetches what this one chart needs and honors the page's excluded runs, like the real
    panel will.
    """
    if not chart_type_name:
        return None, ""
    fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
    parameters = merge_chart_param_values(
        values, checked_values, field_ids, fields, table_ids=table_ids, table_values=table_values
    )
    try:
        chart = build_validated_chart_instance(chart_type_name, parameters)
        page = repo.load_page(store, ref)
        panel = models.PanelInstance[Any, Any](name="preview", charts=[chart])
        return chart.render(
            panel_data.fetch_panel_dataframe(store, ref.experiment_id, panel, page.document.excluded_runs)
        ), ""
    except (ValidationError, KeyError, ValueError) as exc:
        _log.exception("error rendering preview")
        return None, f"Fill in required fields to see a preview ({exc})"


def _register_form(app: Dash) -> None:
    @app.callback(
        Output(ADD_CHART_PARAMS_ID, "children"),
        Input(ADD_CHART_TYPE_SELECT_ID, "value"),
        State(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def build_param_form(
        chart_type_name: str | None, edited: dict[str, Any] | None, experiment_id: int
    ) -> list[Component]:
        return build_chart_param_form(
            chart_type_name,
            EditedChart.model_validate(edited) if edited else None,
            store=get_data_store(),
            experiment_id=experiment_id,
        )

    @app.callback(
        Output(ADD_CHART_PREVIEW_ID, "children"),
        Output(ADD_CHART_ERROR_ID, "children"),
        Input(ADD_CHART_TYPE_SELECT_ID, "value"),
        Input(CHART_PARAM.all(), "value"),
        Input(CHART_PARAM.all(), "checked"),
        Input(CHART_PARAM_TABLE_DATA.all(), "data"),
        State(CHART_PARAM.all(), "id"),
        State(CHART_PARAM_TABLE_DATA.all(), "id"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(ids.STATE_VIEW_ID, "data"),
        prevent_initial_call=True,
    )
    def render_preview(  # noqa: PLR0913
        chart_type_name: str | None,
        values: list[Any],
        checked_values: list[Any],
        table_values: list[Any],
        field_ids: list[dict[str, str]],
        table_ids: list[dict[str, str]],
        experiment_id: int,
        view_id: int | None,
    ) -> tuple[Any, str]:
        return render_chart_preview(
            chart_type_name,
            values,
            checked_values,
            field_ids,
            table_ids,
            table_values,
            store=get_data_store(),
            ref=repo.PageRef(experiment_id, view_id),
        )


def _register_submit(app: Dash) -> None:
    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_ERROR_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ADD_CHART_SUBMIT_ID, "loading", allow_duplicate=True),
        Input(ADD_CHART_SUBMIT_ID, "n_clicks"),
        {
            "chart_type_name": State(ADD_CHART_TYPE_SELECT_ID, "value"),
            "values": State(CHART_PARAM.all(), "value"),
            "checked_values": State(CHART_PARAM.all(), "checked"),
            "table_ids": State(CHART_PARAM_TABLE_DATA.all(), "id"),
            "table_values": State(CHART_PARAM_TABLE_DATA.all(), "data"),
            "field_ids": State(CHART_PARAM.all(), "id"),
            "target": State(ADD_CHART_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(ids.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
    )
    def add_chart(n_clicks: int, add_chart_ctx: _AddChartCtx) -> tuple[Any, bool, str, str | NoUpdate, bool]:
        chart_type_name, target = add_chart_ctx["chart_type_name"], add_chart_ctx["target"]
        if not n_clicks or not chart_type_name or not target:
            raise PreventUpdate

        fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
        parameters = merge_chart_param_values(
            add_chart_ctx["values"],
            add_chart_ctx["checked_values"],
            add_chart_ctx["field_ids"],
            fields,
            table_ids=add_chart_ctx["table_ids"],
            table_values=add_chart_ctx["table_values"],
        )
        panel_name, index = target["panel"], target.get("index")
        # An edit keeps the chart's id, so a link to it still opens it; a new chart gets one of its own, so two charts
        # of one config are not one chart to a link or to anything keyed by id.
        panels = page_model.BasicExperimentPage.model_validate_json(add_chart_ctx["page_json"]).panels
        chart_id = (
            next(p for p in panels if p.name == panel_name).charts[index].id
            if index is not None
            else secrets.token_hex(NEW_CHART_ID_BYTES)
        )
        try:
            new_chart = build_validated_chart_instance(chart_type_name, parameters, chart_id)
        except (ValidationError, KeyError) as exc:
            return no_update, True, f"Invalid parameters: {exc}", no_update, False

        def apply_chart(panels: list[Any]) -> list[Any]:
            return [page_model.upsert_chart(p, panel_name, index, new_chart) for p in panels]

        page, container = edits.commit(
            add_chart_ctx["page_json"],
            add_chart_ctx["paging_json"],
            edits.PageEdit(panels=apply_chart, reveal=edits.RevealChart(new_chart.id)),
        )
        return container, False, "", page.model_dump_json(), False


def register_chart_editor_callbacks(app: Dash) -> None:
    register_settings_form_callbacks(app)
    _register_open_close(app)
    _register_form(app)
    _register_submit(app)
