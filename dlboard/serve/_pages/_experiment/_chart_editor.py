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

import secrets
from dataclasses import dataclass
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
from dlboard.serve._component_ids import ButtonId, DivId, PaneId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import TriggeredId, busy_while_running, require_triggered_id
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
    FieldID,
    build_chart_param_form,
    build_validated_chart_instance,
    key_picker_options,
    merge_chart_param_values,
    register_settings_form_callbacks,
)
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog
from dlboard.serve._pages._experiment._kept_reads import KeptReads
from dlboard.serve._pages._experiment._paging import Paging
from dlboard.serve._pages._experiment._shown_runs import page_scope

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd
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


class _OpenedFrom(TriggeredId):
    """The button that opened the editor: a panel's "Add chart" (no `index`), or a chart's own edit button."""

    panel: str
    index: int | None = None


_TARGET: Final[TypeAdapter[EditChart | AddChart]] = TypeAdapter(EditorTarget)

_NO_LONGER_ON_THE_PAGE: Final = "This chart is no longer on the page."


class _FormStates(edits.EditCtx):
    """What the editor's form is of: the chart type, what the editor is open on, and the ids of the form's fields."""

    chart_type_name: Annotated[str | None, State(CHART_EDITOR_TYPE_ID, "value")]
    target: Annotated[EditorTarget | None, State(CHART_EDITOR_TARGET_ID, "data")]
    field_ids: Annotated[list[dict[str, str]], State(CHART_PARAM.all(), "id")]
    table_ids: Annotated[list[dict[str, str]], State(CHART_PARAM_TABLE_DATA.all(), "id")]


class _FormValues(_FormStates):
    """A whole form: what it is of, and what is in its fields."""

    values: Annotated[list[Any], State(CHART_PARAM.all(), "value")]
    checked_values: Annotated[list[Any], State(CHART_PARAM.all(), "checked")]
    table_values: Annotated[list[Any], State(CHART_PARAM_TABLE_DATA.all(), "data")]


class _DraftStates(_FormStates):
    """What drawing the form's chart in its card reads beside the fields it listens to: the cards' drafts."""

    draft_ids: Annotated[list[chart_view.ChartID], State(chart_view.CHART_DRAFT.all(), "id")]


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


class FormFor(BaseModel, frozen=True):
    """What a settings form is for: what the editor is open on, and the chart type the form is of. The same is the same form."""

    target: EditorTarget | None
    chart_type: str | None


def _form_for(target_data: dict[str, Any] | None, chart_type_name: str | None) -> dict[str, Any]:
    """`FormFor` as `CHART_EDITOR_FORM_FOR_ID` keeps it."""
    return FormFor(target=_target_of(target_data), chart_type=chart_type_name).model_dump(mode="json")


def _settings_form(
    target_data: dict[str, Any] | None, chart_type_name: str | None, page_json: str
) -> list[Component]:
    """
    The form for `chart_type_name`, starting from the chart being edited if it is of that type.

    Its tables also list the rows the chart type draws without saving (`ChartType.fixed_table_rows`): an image chart
    draws every overlay logged over its picture, and its Layers table lists each, though only some may be saved.
    """
    page = page_model.BasicExperimentPage.model_validate_json(page_json)
    catalog = ColumnCatalog.of_page(get_data_store(), page)
    edited: EditedChart | None = None
    if isinstance(target := _target_of(target_data), EditChart) and (
        location := page_model.locate_chart(page.panels, target.chart_id)
    ):
        edited = EditedChart(
            chart_type=location.chart.chart_type,
            parameters=models.ChartTypeRegistry.current_parameters(location.chart),
            fixed_rows=models.ChartTypeRegistry.fixed_table_rows(location.chart, catalog),
        )
    return build_chart_param_form(chart_type_name, edited, catalog=catalog)


_KEPT_READS: Final = 8
"""How many panel reads the chart editor keeps on a worker: about the charts open in an editor on it at once."""


@dataclass(frozen=True)
class _ReadKey:
    """What a read for one chart depends on: the data it asks for, of which runs, as of which write to each experiment."""

    experiments: tuple[tuple[int, int, frozenset[int]], ...]
    """Each experiment drawn: its id, its revision, and the runs of it that are."""
    request: models.DataRequest


EditorReads = KeptReads[_ReadKey, panel_data.PanelData]
"""
The last few panels the chart editor read, so an edit that asks for the same data is drawn without reading it again.

A display-only edit (a chart's height, its colors) asks for what it asked before, and so does opening the editor on
a chart the page has just drawn.
"""


def _draw_form(  # noqa: PLR0913
    values: _FormValues,
    panel_name: str,
    chart_id: str | None,
    draw: Callable[[models.ChartInstance[Any, Any], pd.DataFrame], Component],
    *,
    store: DataStore[...],
    reads: EditorReads,
) -> tuple[Component | None, str]:
    """
    The chart the form makes, drawn by `draw` from what a panel named `panel_name` would fetch for it.

    That is the drawing and "", or nothing and why not.

    Goes through `fetch_panel_data` rather than any whole-experiment dataframe, so only what this one chart needs is
    fetched, and the page's excluded runs (`page_json`, the page the browser has) are left out, like the real panel will.

    A form that does not (yet) make a valid chart is an expected state while someone types, not an error: it says what
    is missing, and is logged at debug level with no traceback. Anything else that goes wrong is logged in full.
    """
    chart_type_name = values.chart_type_name
    if not chart_type_name:
        return None, ""
    fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
    parameters = merge_chart_param_values(
        values.values,
        values.checked_values,
        values.field_ids,
        fields,
        table_ids=values.table_ids,
        table_values=values.table_values,
    )
    try:
        chart = build_validated_chart_instance(chart_type_name, parameters, chart_id)
        scope = page_scope(store, page_model.BasicExperimentPage.model_validate_json(values.page_json))
        panel = models.PanelInstance[Any, Any](name=panel_name, charts=[chart])

        def fetch() -> panel_data.PanelData:
            return panel_data.scope_panel_data(store, scope, panel)

        # Read through the caller's own store: `None` for an experiment they may not see, which is never served kept.
        experiments = [store.get_experiment(experiment_id) for experiment_id in scope]
        data = (
            fetch()
            if any(experiment is None for experiment in experiments)
            else reads.read(
                _ReadKey(
                    experiments=tuple(
                        sorted((e.id, e.revision, scope[e.id]) for e in experiments if e is not None)
                    ),
                    request=chart.data_request(),
                ),
                fetch,
            )
        )
        if (wide := data.wide_history(chart)) is not None and wide.refused:
            return (
                None,
                f"This chart would draw every step of {wide.metrics:,} metrics, more than one chart can show",
            )
        return draw(chart, data.for_chart(chart)), ""
    except ValidationError as exc:
        _log.debug("the chart editor's form makes no chart yet", error=str(exc))
        return None, f"Fill in required fields to see a preview ({exc})"
    except (KeyError, ValueError) as exc:
        _log.exception("error drawing the chart being edited")
        return None, f"The chart could not be drawn ({exc})"


def render_chart_preview(
    values: _FormValues, *, store: DataStore[...], reads: EditorReads
) -> tuple[Component | None, str]:
    """Live-preview a chart that is being added exactly as it'd render in a panel of its own -- same fetch, same runs."""

    def draw(chart: models.ChartInstance[Any, Any], dataframe: pd.DataFrame) -> Component:
        return chart.render(dataframe)

    return _draw_form(values, "preview", None, draw, store=store, reads=reads)


def render_card_drafts(  # noqa: PLR0913
    values: _FormValues,
    location: page_model.ChartLocation,
    page_size: int,
    draft_ids: list[chart_view.ChartID],
    *,
    store: DataStore[...],
    reads: EditorReads,
) -> tuple[list[Any], str]:
    """
    The `CHART_DRAFT` of each card, with the edited chart drawn as the form has it in its own and the rest empty.

    A form that does not (yet) make a valid chart leaves the draft as it was and says why.

    It is drawn even while the form says what the saved chart says. The first draw of a card's draft reads the chart's
    data, and is better done as the editor opens than later, when the person is typing in the form: it can reorder their
    keystrokes in a table cell that is open (the chart editor's tag display test caught exactly that). Every later draw
    that asks for the same data (a display-only edit) is drawn from that read (`EditorReads`).
    """

    def draw(chart: models.ChartInstance[Any, Any], dataframe: pd.DataFrame) -> Component:
        return chart_view.draft_view(location.panel, chart, dataframe, page_size)

    drawn, error = _draw_form(values, location.panel.name, location.chart.id, draw, store=store, reads=reads)
    if drawn is None:
        return [no_update if error else None] * len(draft_ids), error
    here = (location.panel.name, location.index)
    return [drawn if (i.panel, i.index) == here else None for i in draft_ids], ""


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
        prevent_initial_call=True,
    )
    def open_chart_editor(
        _add_clicks: list[int], _edit_clicks: list[int], page_json: str
    ) -> tuple[bool, dict[str, Any], str | None, str, str, list[Component], dict[str, Any]]:
        """Opens the editor, with its form built in this same round trip: it is what the person is waiting for."""
        opened = require_triggered_id(_OpenedFrom)

        if opened.type == panel_view.OPEN_CHART_BUTTON.type:
            adding = AddChart(panel=opened.panel).model_dump()
            return (
                False,
                adding,
                None,
                "Add chart",
                "Add chart",
                [],
                _form_for(adding, None),
            )

        if opened.index is None:
            msg = f"Malformed edit-chart id: {opened}"
            raise ValueError(msg)
        panel = next(
            (
                p
                for p in page_model.BasicExperimentPage.model_validate_json(page_json).panels
                if p.name == opened.panel
            ),
            None,
        )
        if panel is None or opened.index >= len(panel.charts):
            raise PreventUpdate  # the chart went from another tab since its button was drawn: there is nothing to open on
        chart = panel.charts[opened.index]
        editing = EditChart(chart_id=chart.id).model_dump()
        return (
            False,
            editing,
            chart.chart_type,
            "Edit chart",
            "Save changes",
            _settings_form(editing, chart.chart_type, page_json),
            _form_for(editing, chart.chart_type),
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
    reads = EditorReads(kept=_KEPT_READS)  # this app's own: what one app read is no other's to draw

    @app.callback(
        Output(CHART_EDITOR_PARAMS_ID, "children"),
        Output(CHART_EDITOR_FORM_FOR_ID, "data"),
        Input(CHART_EDITOR_TYPE_ID, "value"),
        State(CHART_EDITOR_TARGET_ID, "data"),
        State(CHART_EDITOR_FORM_FOR_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def build_settings_form(
        chart_type_name: str | None,
        target_data: dict[str, Any] | None,
        built_for: dict[str, Any] | None,
        page_json: str,
    ) -> tuple[list[Component], dict[str, Any]]:
        """Rebuilds the form for the type the person chose; opening the editor has built it already."""
        form_for = _form_for(target_data, chart_type_name)
        if form_for == built_for:
            raise PreventUpdate
        return _settings_form(target_data, chart_type_name, page_json), form_for

    @app.callback(
        Output(chart_view.CHART_DRAFT.all(), "children"),
        Output(CHART_EDITOR_PREVIEW_ID, "children"),
        Output(CHART_EDITOR_ERROR_ID, "children"),
        Input(CHART_PARAM.all(), "value"),
        Input(CHART_PARAM.all(), "checked"),
        Input(CHART_PARAM_TABLE_DATA.all(), "data"),
        Input(CHART_EDITOR_REMOUNT_ID, "data"),
        _DraftStates.states(),
        prevent_initial_call=True,
    )
    def render_draft(
        values: list[Any],
        checked_values: list[Any],
        table_values: list[Any],
        _remounted: int | None,
        states: dict[str, Any],
    ) -> tuple[list[Any], Any, str]:
        # Not listening to the chart type: a type chosen rebuilds the form, and the form's own fields then say so. Listening
        # to both drew the chart once with the previous form's values (the wrong chart) before the right one.
        draft = _DraftStates.model_validate(states)
        no_drafts: list[Any] = [None] * len(draft.draft_ids)
        form = _FormValues.model_validate(
            draft.model_dump(exclude={"draft_ids"})
            | {"values": values, "checked_values": checked_values, "table_values": table_values}
        )
        match target := draft.target:
            case None:
                raise PreventUpdate
            case AddChart():
                preview, error = render_chart_preview(form, store=get_data_store(), reads=reads)
                shown = dmc.Stack([dmc.Text("Preview", fw=600), preview]) if preview is not None else None
                return no_drafts, shown, error
            case EditChart():
                panels = page_model.BasicExperimentPage.model_validate_json(draft.page_json).panels
                location = page_model.locate_chart(panels, target.chart_id)
                if location is None:
                    return no_drafts, None, _NO_LONGER_ON_THE_PAGE
                page_size = Paging.model_validate_json(draft.paging_json).sizes.charts
                drafts, error = render_card_drafts(
                    form, location, page_size, draft.draft_ids, store=get_data_store(), reads=reads
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
        _FormValues.states(),
        running=busy_while_running(CHART_EDITOR_SAVE_ID),
        prevent_initial_call=True,
    )
    def save_chart(
        n_clicks: int, states: dict[str, Any]
    ) -> tuple[Any, bool | NoUpdate, dict[str, Any] | NoUpdate | None, str, str | NoUpdate]:
        form = _FormValues.model_validate(states)
        chart_type_name, target = form.chart_type_name, form.target
        if not n_clicks or not chart_type_name or target is None:
            raise PreventUpdate

        fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
        parameters = merge_chart_param_values(
            form.values,
            form.checked_values,
            form.field_ids,
            fields,
            table_ids=form.table_ids,
            table_values=form.table_values,
        )
        panels = page_model.BasicExperimentPage.model_validate_json(form.page_json).panels
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
            form.page_json,
            form.paging_json,
            edits.PageEdit(panels=apply_chart, reveal=edits.RevealChart(chart.id)),
        )
        return container, True, None, "", page.model_dump_json()


def _register_key_search(app: Dash) -> None:
    @app.callback(
        Output(CHART_PARAM.match(), "data"),
        Input(CHART_PARAM.match(), "searchValue"),
        State(CHART_PARAM.match(), "value"),
        State(CHART_EDITOR_TYPE_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def search_keys(
        search: str | None, value: str | list[str] | None, chart_type_name: str | None, page_json: str
    ) -> list[str]:
        """A key picker's options for what is typed in it, searched on the server: it lists a page of them (#416)."""
        field_name = require_triggered_id(FieldID).field
        field = (
            models.ChartTypeRegistry.get_registered_chart_types()
            .get(chart_type_name or "", {})
            .get(field_name)
        )
        if field is None or field.column_kind is None:
            raise PreventUpdate
        kept = value if isinstance(value, list) else [value] if value else list[str]()
        catalog = ColumnCatalog.of_page(
            get_data_store(), page_model.BasicExperimentPage.model_validate_json(page_json)
        )
        return key_picker_options(field, catalog, search=search or "", keep=kept)


def register_chart_editor_callbacks(app: Dash) -> None:
    register_settings_form_callbacks(app)
    _register_open_and_cancel(app)
    _register_form_and_draft(app)
    _register_save(app)
    _register_key_search(app)
