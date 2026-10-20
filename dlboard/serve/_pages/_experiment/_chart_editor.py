"""
The chart editor: a pane docked beside the panel area, where a chart is added or edited with the page still in view.

`chart_editor_dock` is rendered once by the experiment page, outside the panel area (every structural edit rebuilds
that), and the "Add chart" and "Edit chart" buttons that open it are part of the panel area's render tree.

Editing a chart draws it as it would be saved in its own card (`CHART_DRAFT`), in place of the saved chart, which
is left alone under it: a live update still has a chart to replace, and cancelling just empties the draft. A chart
being added has no card yet, so its preview is in the dock. What is being edited is an `EditorTarget`, which names its
charts by id: a delete or a drag elsewhere on the page while the dock is open changes their places, not their ids.
The settings form is `_chart_settings_form.py`'s.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, cast

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.serve import ClientsideScript, get_data_store
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, PaneId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import busy_while_running, require_triggered_id
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
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
from dlboard.serve._pages._experiment._paging import Paging

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore

_log = get_logger(__name__)

_CANCEL_JS = ClientsideScript(Path(__file__).with_name("chart_editor_cancel.js"))
_REMOUNT_JS = ClientsideScript(Path(__file__).with_name("chart_editor_remount.js"))
_MARK_JS = ClientsideScript(Path(__file__).with_name("chart_editor_mark.js"))

CHART_EDITOR_DOCK_ID: PaneId[ids.ExperimentPage] = PaneId("chart-editor-dock")
CHART_EDITOR_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("chart-editor-target")
CHART_EDITOR_FORM_FOR_ID: StoreId[ids.ExperimentPage] = StoreId("chart-editor-form-for")
"""What the form on screen was built for (`_form_key`): the person's choice of type rebuilds the form, but opening the editor already has."""
CHART_EDITOR_REMOUNT_ID: StoreId[ids.ExperimentPage] = StoreId("chart-editor-remount")
"""Changes when the cards were drawn again while the editor is open, so it draws its draft again."""
CHART_EDITOR_TITLE_ID: DivId[ids.ExperimentPage] = DivId("chart-editor-title")
CHART_EDITOR_TYPE_ID: ValueId[ids.ExperimentPage] = ValueId("chart-editor-type")
CHART_EDITOR_PARAMS_ID: DivId[ids.ExperimentPage] = DivId("chart-editor-params")
CHART_EDITOR_PREVIEW_ID: DivId[ids.ExperimentPage] = DivId("chart-editor-preview")
CHART_EDITOR_ERROR_ID: DivId[ids.ExperimentPage] = DivId("chart-editor-error")
CHART_EDITOR_SAVE_ID: ButtonId[ids.ExperimentPage] = ButtonId("chart-editor-save")
CHART_EDITOR_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("chart-editor-cancel")

NEW_CHART_ID_BYTES: Final = 5
"""How many random bytes a new chart's id is made of (twice as many hex characters), enough that two never collide."""


class EditChart(BaseModel, frozen=True):
    """The editor is changing the chart with this id, which is on the page."""

    kind: Literal["edit"] = "edit"
    chart_id: str


class AddChart(BaseModel, frozen=True):
    """The editor is making a new chart, to go at the end of the panel `panel`."""

    kind: Literal["add"] = "add"
    panel: str


EditorTarget = Annotated[EditChart | AddChart, Field(discriminator="kind")]
"""What the editor is open on, as it is kept in `CHART_EDITOR_TARGET_ID`."""

_TARGET: Final[TypeAdapter[EditChart | AddChart]] = TypeAdapter(EditorTarget)

_NO_LONGER_ON_THE_PAGE: Final = "This chart is no longer on the page."


class _FormValues(edits.EditCtx):
    chart_type_name: str | None
    values: list[Any]
    checked_values: list[Any]
    field_ids: list[dict[str, str]]
    table_ids: list[dict[str, str]]
    table_values: list[Any]
    target: dict[str, Any] | None
    experiment_id: int


def chart_editor_dock() -> html.Aside:
    """The chart editor's pane, for the experiment page to render once, beside the panel area. Hidden until opened."""
    chart_types = sorted(models.ChartTypeRegistry.get_registered_chart_types().keys())
    aside = cast("Any", html.Aside)
    return aside(
        [
            Store(id=CHART_EDITOR_TARGET_ID),
            Store(id=CHART_EDITOR_FORM_FOR_ID),
            Store(id=CHART_EDITOR_REMOUNT_ID),
            dmc.Stack(
                [
                    dmc.Text("Edit chart", id=CHART_EDITOR_TITLE_ID, fw=700, size="lg"),
                    dmc.Select(id=CHART_EDITOR_TYPE_ID, label="Chart type", data=chart_types, value=None),
                    html.Div(id=CHART_EDITOR_PARAMS_ID, style=FIELDS_ROW),
                    html.Div(id=CHART_EDITOR_PREVIEW_ID, className="dl-chart-editor-preview"),
                    dmc.Text(id=CHART_EDITOR_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=CHART_EDITOR_CANCEL_ID, variant="default"),
                            dmc.Button("Save changes", id=CHART_EDITOR_SAVE_ID),
                        ],
                        justify="flex-end",
                        className="dl-chart-editor-actions",
                    ),
                ],
                gap="sm",
            ),
        ],
        id=CHART_EDITOR_DOCK_ID,
        hidden=True,
        className="dl-chart-editor",
        **{"aria-label": "Chart settings"},
    )


def _target_of(data: dict[str, Any] | None) -> EditChart | AddChart | None:
    return _TARGET.validate_python(data) if data else None


def _form_key(target: dict[str, Any] | None, chart_type_name: str | None) -> str:
    """What a settings form is for: what the editor is open on, and the chart type the form is of. The same is the same form."""
    return json.dumps([target, chart_type_name], sort_keys=True)


def _settings_form(
    target_data: dict[str, Any] | None, chart_type_name: str | None, page_json: str, experiment_id: int
) -> list[Component]:
    """The form for `chart_type_name`, starting from the chart being edited if it is of that type."""
    edited: EditedChart | None = None
    if isinstance(target := _target_of(target_data), EditChart):
        panels = page_model.BasicExperimentPage.model_validate_json(page_json).panels
        if location := page_model.locate_chart(panels, target.chart_id):
            edited = EditedChart(
                chart_type=location.chart.chart_type,
                parameters=models.ChartTypeRegistry.current_parameters(location.chart),
            )
    return build_chart_param_form(
        chart_type_name, edited, store=get_data_store(), experiment_id=experiment_id
    )


def render_chart_preview(  # noqa: PLR0913
    chart_type_name: str | None,
    values: list[Any],
    checked_values: list[Any],
    field_ids: list[dict[str, str]],
    table_ids: list[dict[str, str]],
    table_values: list[Any],
    *,
    store: DataStore[...],
    experiment_id: int,
    page_json: str,
) -> tuple[Any, str]:
    """
    Live-preview a chart that is being added exactly as it'd render in a panel of its own -- same fetch, same runs.

    Goes through `fetch_panel_dataframe` rather than any whole-experiment dataframe, so the preview
    only ever fetches what this one chart needs and honors the page's excluded runs (`page_json`, the page the
    browser has), like the real panel will.
    """
    if not chart_type_name:
        return None, ""
    fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
    parameters = merge_chart_param_values(
        values, checked_values, field_ids, fields, table_ids=table_ids, table_values=table_values
    )
    try:
        chart = build_validated_chart_instance(chart_type_name, parameters)
        page = page_model.BasicExperimentPage.model_validate_json(page_json)
        panel = models.PanelInstance[Any, Any](name="preview", charts=[chart])
        return chart.render(
            panel_data.fetch_panel_dataframe(store, experiment_id, panel, page.document.excluded_runs)
        ), ""
    except (ValidationError, KeyError, ValueError) as exc:
        _log.exception("error rendering preview")
        return None, f"Fill in required fields to see a preview ({exc})"


def render_card_drafts(
    values: _FormValues,
    location: page_model.ChartLocation,
    page_size: int,
    draft_ids: list[chart_view.ChartID],
    *,
    store: DataStore[...],
) -> tuple[list[Any], str]:
    """
    The `CHART_DRAFT` of each card, with the edited chart drawn as the form has it in its own and the rest empty.

    A form that does not (yet) make a valid chart leaves the draft as it was and says why.

    It is drawn even while the form says what the saved chart says. The first draw of a card's draft is a heavy one, and
    is better done as the editor opens than later, when the person is typing in the form: it can reorder their keystrokes
    in a table cell that is open (the chart editor's tag display test caught exactly that).
    """
    chart_type_name = values["chart_type_name"]
    if not chart_type_name:
        return [None] * len(draft_ids), ""
    fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
    parameters = merge_chart_param_values(
        values["values"],
        values["checked_values"],
        values["field_ids"],
        fields,
        table_ids=values["table_ids"],
        table_values=values["table_values"],
    )
    try:
        draft = build_validated_chart_instance(chart_type_name, parameters, location.chart.id)
        excluded = page_model.BasicExperimentPage.model_validate_json(
            values["page_json"]
        ).document.excluded_runs
        dataframe = panel_data.fetch_panel_dataframe(
            store,
            values["experiment_id"],
            models.PanelInstance[Any, Any](name=location.panel.name, charts=[draft]),
            excluded,
        )
        drawn = chart_view.draft_view(location.panel, draft, dataframe, page_size)
    except (ValidationError, KeyError, ValueError) as exc:
        _log.exception("error rendering the chart being edited")
        return [no_update] * len(draft_ids), f"Fill in required fields to see a preview ({exc})"
    here = (location.panel.name, location.index)
    return [drawn if (i["panel"], i["index"]) == here else None for i in draft_ids], ""


def _register_open_and_cancel(app: Dash) -> None:
    @app.callback(
        Output(CHART_EDITOR_DOCK_ID, "hidden", allow_duplicate=True),
        Output(CHART_EDITOR_TARGET_ID, "data", allow_duplicate=True),
        Output(CHART_EDITOR_TYPE_ID, "value"),
        Output(CHART_EDITOR_TITLE_ID, "children"),
        Output(CHART_EDITOR_SAVE_ID, "children"),
        Output(CHART_EDITOR_PARAMS_ID, "children", allow_duplicate=True),
        Output(CHART_EDITOR_FORM_FOR_ID, "data", allow_duplicate=True),
        Input(panel_view.OPEN_CHART_BUTTON.all(), "n_clicks"),
        Input(chart_view.EDIT_CHART_BUTTON.all(), "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def open_chart_editor(
        _add_clicks: list[int], _edit_clicks: list[int], page_json: str, experiment_id: int
    ) -> tuple[bool, dict[str, Any], str | None, str, str, list[Component], str]:
        """Opens the editor, with its form built in this same round trip: it is what the person is waiting for."""
        triggered_id = cast("chart_view.ChartID", require_triggered_id())

        if triggered_id["type"] == panel_view.OPEN_CHART_BUTTON.type:
            adding = AddChart(panel=triggered_id["panel"]).model_dump()
            return (
                False,
                adding,
                None,
                "Add chart",
                "Add chart",
                [],
                _form_key(adding, None),
            )

        panel = next(
            p
            for p in page_model.BasicExperimentPage.model_validate_json(page_json).panels
            if p.name == triggered_id["panel"]
        )
        if triggered_id["index"] is None:
            msg = f"Malformed edit-chart id: {triggered_id}"
            raise ValueError(msg)
        chart = panel.charts[triggered_id["index"]]
        editing = EditChart(chart_id=chart.id).model_dump()
        return (
            False,
            editing,
            chart.chart_type,
            "Edit chart",
            "Save changes",
            _settings_form(editing, chart.chart_type, page_json, experiment_id),
            _form_key(editing, chart.chart_type),
        )

    app.clientside_callback(
        _CANCEL_JS.source,
        Output(CHART_EDITOR_DOCK_ID, "hidden", allow_duplicate=True),
        Output(CHART_EDITOR_TARGET_ID, "data", allow_duplicate=True),
        Output(chart_view.CHART_DRAFT.all(), "children", allow_duplicate=True),
        Output(CHART_EDITOR_PREVIEW_ID, "children", allow_duplicate=True),
        Output(CHART_EDITOR_ERROR_ID, "children", allow_duplicate=True),
        Input(CHART_EDITOR_CANCEL_ID, "n_clicks"),
        State(chart_view.CHART_DRAFT.all(), "id"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        _MARK_JS.source,
        Output(CHART_EDITOR_DOCK_ID, "data-chart-id"),
        Input(CHART_EDITOR_TARGET_ID, "data"),
        Input(chart_view.CHART_DRAFT.all(), "id"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        _REMOUNT_JS.source,
        Output(CHART_EDITOR_REMOUNT_ID, "data"),
        Input(chart_view.CHART_DRAFT.all(), "id"),
        State(CHART_EDITOR_TARGET_ID, "data"),
        prevent_initial_call=True,
    )


def _register_form_and_draft(app: Dash) -> None:
    @app.callback(
        Output(CHART_EDITOR_PARAMS_ID, "children"),
        Output(CHART_EDITOR_FORM_FOR_ID, "data"),
        Input(CHART_EDITOR_TYPE_ID, "value"),
        State(CHART_EDITOR_TARGET_ID, "data"),
        State(CHART_EDITOR_FORM_FOR_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def build_settings_form(
        chart_type_name: str | None,
        target_data: dict[str, Any] | None,
        built_for: str | None,
        page_json: str,
        experiment_id: int,
    ) -> tuple[list[Component], str]:
        """Rebuilds the form for the type the person chose; opening the editor has built it already."""
        key = _form_key(target_data, chart_type_name)
        if key == built_for:
            raise PreventUpdate
        return _settings_form(target_data, chart_type_name, page_json, experiment_id), key

    @app.callback(
        Output(chart_view.CHART_DRAFT.all(), "children"),
        Output(CHART_EDITOR_PREVIEW_ID, "children"),
        Output(CHART_EDITOR_ERROR_ID, "children"),
        Input(CHART_PARAM.all(), "value"),
        Input(CHART_PARAM.all(), "checked"),
        Input(CHART_PARAM_TABLE_DATA.all(), "data"),
        Input(CHART_EDITOR_REMOUNT_ID, "data"),
        {
            "chart_type_name": State(CHART_EDITOR_TYPE_ID, "value"),
            "target": State(CHART_EDITOR_TARGET_ID, "data"),
            "field_ids": State(CHART_PARAM.all(), "id"),
            "table_ids": State(CHART_PARAM_TABLE_DATA.all(), "id"),
            "draft_ids": State(chart_view.CHART_DRAFT.all(), "id"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(ids.PANEL_PAGING_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "view_id": State(ids.STATE_VIEW_ID, "data"),
        },
        prevent_initial_call=True,
    )
    def render_draft(
        values: list[Any],
        checked_values: list[Any],
        table_values: list[Any],
        _remounted: int | None,
        state: dict[str, Any],
    ) -> tuple[list[Any], Any, str]:
        # Not listening to the chart type: a type chosen rebuilds the form, and the form's own fields then say so. Listening
        # to both drew the chart once with the previous form's values (the wrong chart) before the right one.
        chart_type_name = state["chart_type_name"]
        target = _target_of(state["target"])
        no_drafts: list[Any] = [None] * len(state["draft_ids"])
        form = _FormValues(
            chart_type_name=chart_type_name,
            values=values,
            checked_values=checked_values,
            field_ids=state["field_ids"],
            table_ids=state["table_ids"],
            table_values=table_values,
            target=state["target"],
            experiment_id=state["experiment_id"],
            page_json=state["page_json"],
            paging_json=state["paging_json"],
        )
        match target:
            case None:
                raise PreventUpdate
            case AddChart():
                preview, error = render_chart_preview(
                    chart_type_name,
                    values,
                    checked_values,
                    state["field_ids"],
                    state["table_ids"],
                    table_values,
                    store=get_data_store(),
                    experiment_id=state["experiment_id"],
                    page_json=state["page_json"],
                )
                shown = dmc.Stack([dmc.Text("Preview", fw=600), preview]) if preview is not None else None
                return no_drafts, shown, error
            case EditChart():
                panels = page_model.BasicExperimentPage.model_validate_json(state["page_json"]).panels
                location = page_model.locate_chart(panels, target.chart_id)
                if location is None:
                    return no_drafts, None, _NO_LONGER_ON_THE_PAGE
                page_size = Paging.model_validate_json(state["paging_json"]).sizes.charts
                drafts, error = render_card_drafts(
                    form, location, page_size, state["draft_ids"], store=get_data_store()
                )
                return drafts, None, error


def _register_save(app: Dash) -> None:
    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(CHART_EDITOR_DOCK_ID, "hidden", allow_duplicate=True),
        Output(CHART_EDITOR_TARGET_ID, "data", allow_duplicate=True),
        Output(CHART_EDITOR_ERROR_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(CHART_EDITOR_SAVE_ID, "n_clicks"),
        {
            "chart_type_name": State(CHART_EDITOR_TYPE_ID, "value"),
            "values": State(CHART_PARAM.all(), "value"),
            "checked_values": State(CHART_PARAM.all(), "checked"),
            "table_ids": State(CHART_PARAM_TABLE_DATA.all(), "id"),
            "table_values": State(CHART_PARAM_TABLE_DATA.all(), "data"),
            "field_ids": State(CHART_PARAM.all(), "id"),
            "target": State(CHART_EDITOR_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(ids.PANEL_PAGING_ID, "data"),
        },
        running=busy_while_running(CHART_EDITOR_SAVE_ID),
        prevent_initial_call=True,
    )
    def save_chart(
        n_clicks: int, form: _FormValues
    ) -> tuple[Any, bool | NoUpdate, dict[str, Any] | NoUpdate | None, str, str | NoUpdate]:
        chart_type_name, target = form["chart_type_name"], _target_of(form["target"])
        if not n_clicks or not chart_type_name or target is None:
            raise PreventUpdate

        fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
        parameters = merge_chart_param_values(
            form["values"],
            form["checked_values"],
            form["field_ids"],
            fields,
            table_ids=form["table_ids"],
            table_values=form["table_values"],
        )
        panels = page_model.BasicExperimentPage.model_validate_json(form["page_json"]).panels
        # An edit keeps each chart's id, so a link to it still opens it; a new chart gets one of its own, so two charts
        # of one config are not one chart to a link or to anything keyed by id.
        match target:
            case EditChart():
                location = page_model.locate_chart(panels, target.chart_id)
                if location is None:
                    return no_update, no_update, no_update, _NO_LONGER_ON_THE_PAGE, no_update
                panel_name, index, chart_id = location.panel.name, location.index, location.chart.id
            case AddChart():
                panel_name, index, chart_id = target.panel, None, secrets.token_hex(NEW_CHART_ID_BYTES)
        try:
            chart = build_validated_chart_instance(chart_type_name, parameters, chart_id)
        except (ValidationError, KeyError) as exc:
            return no_update, no_update, no_update, f"Invalid parameters: {exc}", no_update

        def apply_chart(panels: list[Any]) -> list[Any]:
            return [page_model.upsert_chart(p, panel_name, index, chart) for p in panels]

        page, container = edits.commit(
            form["page_json"],
            form["paging_json"],
            edits.PageEdit(panels=apply_chart, reveal=edits.RevealChart(chart.id)),
        )
        return container, True, None, "", page.model_dump_json()


def register_chart_editor_callbacks(app: Dash) -> None:
    register_settings_form_callbacks(app)
    _register_open_and_cancel(app)
    _register_form_and_draft(app)
    _register_save(app)
