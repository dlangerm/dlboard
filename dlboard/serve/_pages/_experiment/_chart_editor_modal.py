"""
The add/edit-chart modal: its shell, parameter form, live preview, and the submit that saves the chart.

The shell (`add_chart_modal`) is rendered once by the experiment page, beside the panel area; the
"Add chart" and "Edit chart" buttons that open it are part of the panel area's render tree.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, dcc, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, ValidationError
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, PatternId, StoreId, ValueId
from dlboard.serve._grid import KEY_PICKER_EDITOR, SLIDER_RENDERER, themed_grid_kwargs
from dlboard.serve._pages._dash_helpers import require_triggered_id
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dash.development.base_component import Component

    from dlboard.models import DataStore

_log = get_logger(__name__)

_CHART_PARAM_CLEAR_JS = ClientsideScript(Path(__file__).with_name("chart_param_clear.js"))
_TABLE_VALUE_JS = ClientsideScript(Path(__file__).with_name("chart_param_table_value.js"))
_TABLE_ROWS_JS = ClientsideScript(Path(__file__).with_name("chart_param_table_rows.js"))

_SMALL_FIELD: Final = {"flex": "1 1 11rem"}
_MEDIUM_FIELD: Final = {"flex": "1 1 16rem"}
_WIDE_FIELD: Final = {"flex": "1 1 100%"}
"""
How much of a row a setting's input asks for. The form is a wrapping row, so several of the small ones (a switch, a
number, a short list of choices) share a line, a pair of column pickers too, and a list or a table has a line to itself.
"""
_FIELDS_ROW: Final = {"display": "flex", "flexWrap": "wrap", "alignItems": "flex-start", "gap": "0.5rem 1rem"}
"""A wrapping row of settings inputs, each asking for the width of its own `_*_FIELD`."""


SLIDER_STEPS: Final = 100
"""How many notches a slider has across its range."""

ADD_CHART_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("add-chart-modal")
ADD_CHART_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("add-chart-target")
ADD_CHART_INITIAL_PARAMS_ID: StoreId[ids.ExperimentPage] = StoreId("add-chart-initial-params")
ADD_CHART_TYPE_SELECT_ID: ValueId[ids.ExperimentPage] = ValueId("add-chart-type-select")
ADD_CHART_PARAMS_ID: DivId[ids.ExperimentPage] = DivId("add-chart-params")
ADD_CHART_PREVIEW_ID: DivId[ids.ExperimentPage] = DivId("add-chart-preview")
ADD_CHART_ERROR_ID: DivId[ids.ExperimentPage] = DivId("add-chart-error")
ADD_CHART_SUBMIT_ID: ButtonId[ids.ExperimentPage] = ButtonId("add-chart-submit")
ADD_CHART_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("add-chart-cancel")


class _FieldPattern(PatternId[ids.ExperimentPage]):
    """A family of the form's components, with one per field of whichever chart type the form is showing."""

    KEYS: ClassVar[tuple[str, ...]] = ("field",)

    def of(self, field_name: str) -> dict[str, str]:
        return {"type": self.type, "field": field_name}


CHART_PARAM: Final = _FieldPattern("chart-param")
"""A field's own input, which reports through `value` (or a Switch's `checked`)."""
_CHART_PARAM_CLEAR: Final = _FieldPattern("chart-param-clear")
CHART_PARAM_TABLE_DATA: Final = _FieldPattern("chart-param-table-data")
"""A `TABLE` chart parameter's own value: a `Store` holding its rows as the mapping the setting takes. Kept apart
from `CHART_PARAM`, whose `data` is a Select's options."""
_CHART_PARAM_TABLE: Final = _FieldPattern("chart-param-table")
"""The editable grid of a `TABLE` chart parameter."""
_CHART_PARAM_TABLE_ADD: Final = _FieldPattern("chart-param-table-add")
_CHART_PARAM_TABLE_REMOVE: Final = _FieldPattern("chart-param-table-remove")


class EditedChart(BaseModel, frozen=True):
    """The chart the editor was opened on: its type and settings, which only a form of that same type is filled from."""

    chart_type: str
    parameters: dict[str, Any]


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
                        style=_FIELDS_ROW,
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


def merge_chart_param_values(  # noqa: PLR0913
    values: list[Any],
    checked_values: list[Any],
    field_ids: list[dict[str, str]],
    fields: dict[str, models.ParameterField],
    *,
    table_ids: Sequence[Mapping[str, str]] = (),
    table_values: Sequence[Any] = (),
) -> dict[str, Any]:
    """
    Merge a form's reports by field id.

    NumberInput/TextInput/Select report via `value` and Switch via `checked`; a table field is a separate
    `Store` (`table_ids`, `table_values`) holding its rows.

    A cleared NumberInput reports `None`, which isn't a valid `int`/`float` -- for a field with its
    own default (`required=False`), that value is dropped entirely so validation falls back to the
    field's default instead of failing with "not a valid integer".
    """
    merged = [v if v is not None else c for v, c in zip(values, checked_values, strict=True)]
    parameters: dict[str, Any] = {
        fid["field"]: rows for fid, rows in zip(table_ids, table_values, strict=True)
    }
    for fid, val in zip(field_ids, merged, strict=True):
        field_name = fid["field"]
        field = fields.get(field_name)
        if val is None and field is not None and not field.required:
            continue
        parameters[field_name] = val
    return parameters


def build_validated_chart_instance(
    chart_type_name: str, parameters: dict[str, Any], chart_id: str = ""
) -> models.ChartInstance[Any, Any]:
    """
    Build a `models.ChartInstance`, validating `parameters` against the chart type's own settings model.

    `models.ChartInstance.parameters` is an untyped `dict[str, object]` -- constructing one directly never
    raises even for garbage values (e.g. a cleared numeric field), so callers that need to surface a
    validation error to the user (rather than have it surface later, unguarded, from
    `hint_required_columns`/`render`) must validate through here instead.

    `chart_id` is the id of the chart being edited, which an edit keeps (a `?chart=` link to it still opens it); left
    out, the id is derived from the settings.
    """
    chart_type = models.ChartTypeRegistry.get_chart_type(chart_type_name)
    chart_type.parameter_type().model_validate(parameters)
    return models.ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters, id=chart_id)


def _table_input(
    field_name: str,
    field: models.ParameterField,
    label: str,
    entries: dict[str, dict[str, Any]],
    key_options: list[str],
) -> Component:
    """
    A `TABLE` field as an editable grid, a row per entry with a cell for each field of its row model.

    The grid is only what the person edits: `chart_param_table_value.js` writes its rows, as the mapping the
    setting holds, into the field's own `Store`, which is what the form reads back like any other field. A field
    with `key_options` (the names its rows can be) suggests them as a row's name is typed.
    """
    key_column: dict[str, Any] = {
        "field": "key",
        "headerName": "Name",
        "editable": True,
        "flex": 2,
        "checkboxSelection": True,
    }
    if key_options:
        key_column |= {
            "cellEditor": KEY_PICKER_EDITOR,
            "cellEditorPopup": True,
            "cellEditorParams": {"options": key_options},
        }
    columns = [key_column]
    for column in field.columns or ():
        cell: dict[str, Any] = {"editable": True, "cellEditor": "agNumberCellEditor"}
        if column.choices:
            cell = cell | {
                "cellEditor": "agSelectCellEditor",
                "cellEditorParams": {"values": ["", *column.choices]},
            }
        elif column.type == models.ParameterFieldType.BOOL:
            cell = {"editable": True, "cellDataType": "boolean"}  # a checkbox, in the cell and to edit it
        elif (
            column.type == models.ParameterFieldType.FLOAT
            and column.minimum is not None
            and column.maximum is not None
        ):
            cell = {
                "editable": False,
                "cellRenderer": SLIDER_RENDERER,
                "cellRendererParams": {
                    "min": column.minimum,
                    "max": column.maximum,
                    "step": (column.maximum - column.minimum) / SLIDER_STEPS,
                    "fallback": column.default,
                },
            }
        columns.append(
            {
                "field": column.name,
                "headerName": column.name,
                "headerTooltip": column.description,
                "flex": 1,
                **cell,
            }
        )
    return dmc.Stack(
        [
            dmc.Text(label, size="sm", fw=500),
            dmc.Text(field.description, size="xs", c="dimmed"),
            dag.AgGrid(
                id=_CHART_PARAM_TABLE.of(field_name),
                columnDefs=columns,
                rowData=[{"key": key, **cells} for key, cells in entries.items()],
                dashGridOptions={
                    "domLayout": "autoHeight",
                    "singleClickEdit": True,
                    "stopEditingWhenCellsLoseFocus": True,
                    "rowSelection": {"mode": "multiRow", "checkboxes": False, "enableClickSelection": False},
                },
                **themed_grid_kwargs(),
            ),
            dmc.Group(
                [
                    dmc.Button(
                        "Add",
                        id=_CHART_PARAM_TABLE_ADD.of(field_name),
                        size="xs",
                        variant="default",
                    ),
                    dmc.Button(
                        "Remove checked",
                        id=_CHART_PARAM_TABLE_REMOVE.of(field_name),
                        size="xs",
                        variant="default",
                    ),
                ],
                gap="xs",
            ),
            dcc.Store(id=CHART_PARAM_TABLE_DATA.of(field_name), data=entries),
        ],
        gap="xs",
        style=_WIDE_FIELD,
    )


def _pattern_suggestions(names: list[str]) -> list[str]:
    """`names` and the `prefix*` of each folder they sit in (`eval/a`, `eval/b` suggest `eval/*`), and `*` for all."""
    folders = {name[: i + 1] for name in names for i, char in enumerate(name) if char == "/"}
    return ["*", *sorted(f"{folder}*" for folder in folders), *sorted(names)]


def _param_field_input(  # noqa: PLR0911
    field_name: str,
    field: models.ParameterField,
    catalog: ColumnCatalog,
    *,
    override: bool | int | float | str | list[str] | dict[str, Any] | None = None,
) -> Component:
    input_id = CHART_PARAM.of(field_name)
    label = f"{field_name} *" if field.required else field_name
    value = override if override is not None else field.default

    match field.type:
        case models.ParameterFieldType.BOOL:
            return dmc.Switch(
                id=input_id,
                label=label,
                checked=bool(value) if value is not None else False,
                description=field.description,
                style=_SMALL_FIELD,
            )
        case models.ParameterFieldType.INT | models.ParameterFieldType.FLOAT:
            number_value = cast("int | float | None", value)
            return dmc.NumberInput(
                id=input_id,
                label=label,
                value=number_value,
                min=field.minimum,
                max=field.maximum,
                step=1 if field.type == models.ParameterFieldType.INT else 0.1,
                description=field.description,
                # Optional fields (a default, not required) get a visible clear button that resets
                # to that default -- clearing via backspace alone works too, but isn't discoverable.
                rightSection=(
                    dmc.ActionIcon(
                        icon(Icon.CLOSE),
                        id=_CHART_PARAM_CLEAR.of(field_name),
                        n_clicks=0,
                        variant="subtle",
                        color="gray",
                        size="xs",
                        **cast("dict[str, Any]", {"aria-label": "Reset to default"}),
                    )
                    if not field.required
                    else None
                ),
                rightSectionPointerEvents="all",
                style=_SMALL_FIELD,
            )
        case models.ParameterFieldType.TABLE:
            key_options = catalog.options(field.column_kind) if field.column_kind is not None else list[str]()
            return _table_input(
                field_name, field, label, cast("dict[str, dict[str, Any]]", value or {}), key_options
            )
        case models.ParameterFieldType.LIST_PATTERN:
            names = catalog.options(field.column_kind) if field.column_kind is not None else list[str]()
            return dmc.TagsInput(
                id=input_id,
                label=label,
                data=_pattern_suggestions(names),
                value=cast("list[str]", value or []),
                description=field.description,
                placeholder="Pick a name, or type one or a prefix ending in *",
                clearable=True,
                style=_WIDE_FIELD,
            )
        case models.ParameterFieldType.STR | models.ParameterFieldType.LIST_STR:
            if field.choices is not None:
                select_value = cast("str | None", value)
                return dmc.Select(
                    id=input_id,
                    label=label,
                    data=list(field.choices),
                    value=select_value,
                    # An optional choice with no default (`None`: "pick for me") can be put back to blank.
                    allowDeselect=not field.required and field.default is None,
                    description=field.description,
                    style=_SMALL_FIELD,
                )

            options = catalog.options(field.column_kind) if field.column_kind is not None else None
            if field.type == models.ParameterFieldType.LIST_STR:
                multiselect_value = cast("list[str] | None", value)
                return dmc.MultiSelect(
                    id=input_id,
                    label=label,
                    data=sorted(options) if options else [],
                    value=multiselect_value or [],
                    searchable=True,
                    description=field.description,
                    style=_WIDE_FIELD,
                )
            if options:
                return dmc.Select(
                    id=input_id,
                    label=label,
                    data=sorted(options),
                    value=value,  # pyrefly: ignore [bad-argument-type]
                    searchable=True,
                    description=field.description,
                    style=_MEDIUM_FIELD,
                )
            return dmc.TextInput(
                id=input_id,
                label=label,
                value=value or "",  # pyrefly: ignore [bad-argument-type]
                description=field.description,
                style=_MEDIUM_FIELD,
            )


def _register_clear_button(app: Dash) -> None:
    # The ✕ button next to an optional numeric chart-param field (see `_param_field_input`)
    # resets it to empty/default -- purely a client-side convenience for something backspace
    # already does, so no round trip needed.
    app.clientside_callback(
        _CHART_PARAM_CLEAR_JS.source,
        Output(CHART_PARAM.match(), "value"),
        Input(_CHART_PARAM_CLEAR.match(), "n_clicks"),
        prevent_initial_call=True,
    )


def _register_table_inputs(app: Dash) -> None:
    app.clientside_callback(
        _TABLE_VALUE_JS.source,
        Output(CHART_PARAM_TABLE_DATA.match(), "data"),
        Input(_CHART_PARAM_TABLE.match(), "virtualRowData"),
        Input(_CHART_PARAM_TABLE.match(), "cellValueChanged"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        _TABLE_ROWS_JS.source,
        Output(_CHART_PARAM_TABLE.match(), "rowTransaction"),
        Output(_CHART_PARAM_TABLE.match(), "deleteSelectedRows"),
        Input(_CHART_PARAM_TABLE_ADD.match(), "n_clicks"),
        Input(_CHART_PARAM_TABLE_REMOVE.match(), "n_clicks"),
        prevent_initial_call=True,
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


def build_chart_param_form(
    chart_type_name: str | None,
    edited: EditedChart | None,
    *,
    store: DataStore[...],
    experiment_id: int,
) -> list[Component]:
    """
    Build the add/edit-chart form's fields, offering the experiment's real columns as choices.

    The fields start from the `edited` chart's settings only while the form is for its own type: a different
    type's settings are not this one's, even where a name (`height`) happens to match.
    """
    if not chart_type_name:
        return []
    catalog = ColumnCatalog.load(store, experiment_id)
    fields = models.ChartTypeRegistry.get_registered_chart_types()[chart_type_name]
    initial_params = (
        edited.parameters if edited and edited.chart_type == chart_type_name else dict[str, Any]()
    )
    by_section: dict[models.SettingsSection, list[Component]] = {}
    for name, field in fields.items():
        by_section.setdefault(field.section, []).append(
            _param_field_input(name, field, catalog, override=initial_params.get(name))
        )
    if len(by_section) <= 1:
        return [field for section_fields in by_section.values() for field in section_fields]
    return [
        dmc.Accordion(
            [
                dmc.AccordionItem(
                    [
                        dmc.AccordionControl(section.title),
                        dmc.AccordionPanel(html.Div(section_fields, style=_FIELDS_ROW)),
                    ],
                    value=section.title,
                )
                for section, section_fields in by_section.items()
            ],
            multiple=True,
            value=[section.title for section in by_section if section.opened],
            style=_WIDE_FIELD,
        )
    ]


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
    _register_clear_button(app)
    _register_table_inputs(app)
    _register_open_close(app)
    _register_form(app)
    _register_submit(app)
