"""
The chart editor's settings form: a field per setting of a chart type, and the values read back from it.

Built from `ChartTypeRegistry.get_registered_chart_types()`, so any chart plugin's settings are editable with no
code of its own. Settings are grouped into their `SettingsSection`s. Pure of any editing session: the form is handed
the chart it is for and the experiment's columns, and `merge_chart_param_values` reads its values back.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Dash, Input, Output, dcc, html
from pydantic import BaseModel, Field

from dlboard import models
from dlboard.models import family_template
from dlboard.serve import ClientsideScript, Icon, icon
from dlboard.serve._component_ids import PatternId
from dlboard.serve._grid import CHOICE_RENDERER, KEY_PICKER_EDITOR, SLIDER_RENDERER, themed_grid_kwargs
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment._column_catalog import KEY_PICKER_LIMIT

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dash.development.base_component import Component

    from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog


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
FIELDS_ROW: Final = {"display": "flex", "flexWrap": "wrap", "alignItems": "flex-start", "gap": "0.5rem 1rem"}
"""A wrapping row of settings inputs, each asking for the width of its own `_*_FIELD`."""


MAX_CHOICES_SIDE_BY_SIDE: Final = 4
"""How many choices a table cell shows side by side (`CHOICE_RENDERER`); more are picked from a list."""
CHOICE_WIDTH_PX: Final = 58
"""How wide each of a cell's side-by-side choices is."""

SLIDER_STEPS: Final = 100
"""How many notches a slider has across its range."""
INPUT_DEBOUNCE_MS: Final = 400
"""How long a number or text input waits after the last keystroke before the chart is redrawn from it."""

_IS_FIXED: Final = "!!(params.data && params.data.fixed)"
"""A grid cell's JS: true on a row the chart supplies (`fixed` in its data), false on one the person made."""
_NOT_FIXED: Final = f"!{_IS_FIXED}"
FIXED_ROW_CLASS: Final = "dl-fixed-row"
"""The class of a row the chart supplies in a settings table."""


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
    fixed_rows: models.FixedRows = Field(default_factory=dict)
    """The rows the chart draws whether or not they are saved (`ChartType.fixed_table_rows`), listed in its tables."""


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
    chart_type_name: str, parameters: dict[str, Any], chart_id: str | None = None
) -> models.ChartInstance[Any, Any]:
    """
    Build a `models.ChartInstance`, validating `parameters` against the chart type's own settings model.

    `models.ChartInstance.parameters` is an untyped `dict[str, object]` -- constructing one directly never
    raises even for garbage values (e.g. a cleared numeric field), so callers that need to surface a
    validation error to the user (rather than have it surface later, unguarded, from
    `hint_required_columns`/`render`) must validate through here instead.

    `chart_id` is the id of the chart being edited, which an edit keeps (a `?chart=` link to it still opens it); `None`
    for a new chart, whose id is derived from its settings.
    """
    chart_type = models.ChartTypeRegistry.get_chart_type(chart_type_name)
    chart_type.parameter_type().model_validate(parameters)
    if chart_id is None:
        return models.ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
    return models.ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters, id=chart_id)


def _table_input(  # noqa: PLR0913
    field_name: str,
    field: models.ParameterField,
    label: str,
    entries: dict[str, dict[str, Any]],
    key_options: list[str],
    fixed: dict[str, str],
) -> Component:
    """
    A `TABLE` field as an editable grid, a row per entry with a cell for each field of its row model.

    The grid is only what the person edits: `chart_param_table_value.js` writes its rows, as the mapping the
    setting holds, into the field's own `Store`, which is what the form reads back like any other field. A field
    with `key_options` (the names its rows can be) suggests them as a row's name is typed.

    `fixed` are rows the chart draws whether or not they are saved (key to the label it is shown by): they come
    first, as a blank row if the entries have none, and can be restyled but not renamed or removed.
    """
    entries = {**{key: {} for key in fixed}, **entries}
    key_column: dict[str, Any] = {
        "field": "key",
        "headerName": "Name",
        "editable": {"function": _NOT_FIXED},
        "valueFormatter": {"function": "params.data.label || params.value"},
        "flex": 2,
        "minWidth": 120,
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
        if column.choices and len(column.choices) <= MAX_CHOICES_SIDE_BY_SIDE:
            cell = {
                "editable": False,
                "width": CHOICE_WIDTH_PX * len(column.choices),
                "flex": 0,  # as wide as its choices need, so a name keeps the rest
                "cellRenderer": CHOICE_RENDERER,
                "cellRendererParams": {"choices": list(column.choices), "fallback": column.default},
            }
        elif column.choices:
            cell = cell | {
                "cellEditor": "agSelectCellEditor",
                "cellEditorParams": {"values": ["", *column.choices]},
            }
        elif (
            column.type == models.ParameterFieldType.FLOAT
            and column.minimum is not None
            and column.maximum is not None
        ):
            cell = {
                "editable": False,
                "minWidth": 110,  # room for the slider as well as its value
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
                "headerName": column.label,
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
                rowData=[
                    {"key": key, **({"label": fixed[key], "fixed": True} if key in fixed else {}), **cells}
                    for key, cells in entries.items()
                ],
                # The code strings are written here, never taken from a request.
                dangerously_allow_code=True,
                dashGridOptions={
                    "domLayout": "autoHeight",
                    "singleClickEdit": True,
                    "stopEditingWhenCellsLoseFocus": True,
                    "rowSelection": {
                        "mode": "multiRow",
                        "checkboxes": False,
                        "enableClickSelection": False,
                    },
                    # A row the chart supplies has no checkbox to tick (`grid_cell_components.css`).
                    "rowClassRules": {FIXED_ROW_CLASS: _IS_FIXED},
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
    """
    The patterns a picker offers for `names`, and the names.

    A `prefix*` for each folder they sit in (`eval/a`, `eval/b` suggest `eval/*`), a `#` pattern for each family of
    them that differ only in their numbers (`layer_1/x`, `layer_2/x` suggest `layer_#/x`), and `*` for all.
    """
    folders = {name[: i + 1] for name in names for i, char in enumerate(name) if char == "/"}
    families = Counter(family_template(name) for name in names)
    shared = {template for template, size in families.items() if size > 1}
    return ["*", *sorted(f"{folder}*" for folder in folders), *sorted(shared), *sorted(names)]


def _param_field_input(  # noqa: PLR0911
    field_name: str,
    field: models.ParameterField,
    catalog: ColumnCatalog,
    *,
    override: bool | int | float | str | list[str] | dict[str, Any] | None = None,
    fixed_rows: dict[str, str] | None = None,
) -> Component:
    input_id = CHART_PARAM.of(field_name)
    label = f"{field.label} *" if field.required else field.label
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
                # Typing redraws the chart only once it pauses, not on every digit (each redraw is a fetch).
                debounce=INPUT_DEBOUNCE_MS,
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
                field_name,
                field,
                label,
                cast("dict[str, dict[str, Any]]", value or {}),
                key_options,
                fixed_rows or {},
            )
        case models.ParameterFieldType.LIST_PATTERN:
            names = catalog.options(field.column_kind) if field.column_kind is not None else list[str]()
            return dmc.TagsInput(
                id=input_id,
                label=label,
                data=_pattern_suggestions(names),
                limit=KEY_PICKER_LIMIT,
                value=cast("list[str]", value or []),
                description=field.description,
                placeholder="Pick a name, or type a pattern: * is any text, # is digits",
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
                    limit=KEY_PICKER_LIMIT,
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
                    limit=KEY_PICKER_LIMIT,
                    value=value,  # pyrefly: ignore [bad-argument-type]
                    searchable=True,
                    description=field.description,
                    style=_MEDIUM_FIELD,
                )
            return dmc.TextInput(
                id=input_id,
                label=label,
                # Typing a name redraws the chart only once it pauses, as a number does.
                debounce=INPUT_DEBOUNCE_MS,
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


def build_chart_param_form(
    chart_type_name: str | None,
    edited: EditedChart | None,
    *,
    catalog: ColumnCatalog,
) -> list[Component]:
    """
    Build the add/edit-chart form's fields, offering the experiment's real columns (`catalog`) as choices.

    The fields start from the `edited` chart's settings only while the form is for its own type: a different
    type's settings are not this one's, even where a name (`height`) happens to match.
    """
    if not chart_type_name:
        return []
    fields = models.ChartTypeRegistry.get_registered_chart_types()[chart_type_name]
    of_edited = edited if edited and edited.chart_type == chart_type_name else None
    initial_params = of_edited.parameters if of_edited else dict[str, Any]()
    fixed_rows = of_edited.fixed_rows if of_edited else models.FixedRows()
    by_section: dict[models.SettingsSection, list[Component]] = {}
    for name, field in fields.items():
        by_section.setdefault(field.section, []).append(
            _param_field_input(
                name, field, catalog, override=initial_params.get(name), fixed_rows=fixed_rows.get(name)
            )
        )
    if len(by_section) <= 1:
        return [field for section_fields in by_section.values() for field in section_fields]
    return [
        dmc.Accordion(
            [
                dmc.AccordionItem(
                    [
                        dmc.AccordionControl(section.title),
                        dmc.AccordionPanel(html.Div(section_fields, style=FIELDS_ROW)),
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


def register_settings_form_callbacks(app: Dash) -> None:
    """The clientside callbacks that make the form's reset buttons and table fields work."""
    _register_clear_button(app)
    _register_table_inputs(app)
