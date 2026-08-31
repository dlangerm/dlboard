"""
The add/edit-chart modal: builds its parameter form, live-previews the chart, opens/closes it.

The modal's static shell (`dmc.Modal(...)`) is defined in `_experiment_page_state.py` instead of
here, since `accordion_view` needs to include it in its output on every render -- this module only
owns what fills that shell in. The "submit" mutation (`add_chart`) lives in `_panel_controls.py`
alongside the rest of the panel/chart CRUD, since it mutates panels the same way every callback
there does.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, MATCH, Dash, Input, Output, State
from dash.exceptions import PreventUpdate
from pydantic import ValidationError
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.models import constants
from dltrack.plugins.pages.experiment import _experiment_page_state as core
from dltrack.plugins.pages.experiment._dataframe_helpers import group_columns_by_kind
from dltrack.serve import ClientsideScript, get_data_store

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import DataStore

_log = get_logger(__name__)

_CHART_PARAM_CLEAR_JS = ClientsideScript(Path(__file__).with_name("chart_param_clear.js"))


def _param_field_input(
    field_name: str,
    field: models.ParameterField,
    columns_by_kind: dict[models.ColumnKind, list[str]],
    *,
    override: bool | int | float | str | list[str] | None = None,
) -> Component:
    input_id = core.chart_param_id(field_name)
    label = f"{field_name} *" if field.required else field_name
    value = override if override is not None else field.default

    match field.type:
        case models.ParameterFieldType.BOOL:
            return dmc.Switch(
                id=input_id,
                label=label,
                checked=bool(value) if value is not None else False,
                description=field.description,
            )
        case models.ParameterFieldType.INT | models.ParameterFieldType.FLOAT:
            number_value = cast("int | float | None", value)
            return dmc.NumberInput(
                id=input_id,
                label=label,
                value=number_value,
                step=1 if field.type == models.ParameterFieldType.INT else 0.1,
                description=field.description,
                # Optional fields (a default, not required) get a visible clear button that resets
                # to that default -- clearing via backspace alone works too, but isn't discoverable.
                rightSection=(
                    dmc.ActionIcon(
                        "✕",
                        id=core.chart_param_clear_id(field_name),
                        n_clicks=0,
                        variant="subtle",
                        color="gray",
                        size="xs",
                    )
                    if not field.required
                    else None
                ),
                rightSectionPointerEvents="all",
            )
        case models.ParameterFieldType.STR | models.ParameterFieldType.LIST_STR:
            if field.choices is not None:
                select_value = cast("str | None", value)
                return dmc.Select(
                    id=input_id,
                    label=label,
                    data=list(field.choices),
                    value=select_value,
                    allowDeselect=False,
                    description=field.description,
                )

            options = columns_by_kind.get(field.column_kind, []) if field.column_kind is not None else None
            if field.type == models.ParameterFieldType.LIST_STR:
                multiselect_value = cast("list[str] | None", value)
                return dmc.MultiSelect(
                    id=input_id,
                    label=label,
                    data=sorted(options) if options else [],
                    value=multiselect_value or [],
                    searchable=True,
                    description=field.description,
                )
            if options:
                return dmc.Select(
                    id=input_id,
                    label=label,
                    data=sorted(options),
                    value=value,  # pyright: ignore[reportArgumentType]
                    searchable=True,
                    description=field.description,
                )
            return dmc.TextInput(
                id=input_id,
                label=label,
                value=value or "",  # pyright: ignore[reportArgumentType]
                description=field.description,
            )


def _register_clear_button(app: Dash) -> None:
    # The ✕ button next to an optional numeric chart-param field (see `_param_field_input`)
    # resets it to empty/default -- purely a client-side convenience for something backspace
    # already does, so no round trip needed.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _CHART_PARAM_CLEAR_JS.source,
        Output({"type": core.CHART_PARAM_TYPE, "field": MATCH}, "value"),
        Input({"type": "chart-param-clear", "field": MATCH}, "n_clicks"),
        prevent_initial_call=True,
    )


def _register_open_close(app: Dash) -> None:
    # --- open the modal, either to add a new chart or edit an existing one ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.ADD_CHART_TARGET_ID, "data"),
        Output(core.ADD_CHART_TYPE_SELECT_ID, "value"),
        Output(core.ADD_CHART_INITIAL_PARAMS_ID, "data"),
        Input({"type": "open-add-chart", "panel": ALL}, "n_clicks"),
        Input({"type": "edit-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def open_chart_modal(
        _add_clicks: list[int], _edit_clicks: list[int], page_json: str
    ) -> tuple[bool, core.ChartTargetData, str | None, dict[str, Any]]:
        triggered_id = cast("core.ChartID", core.require_triggered_id())

        if triggered_id["type"] == "open-add-chart":
            return True, {"panel": str(triggered_id["panel"]), "index": None}, None, {}

        curr_page = core.BasicExperimentPage.model_validate_json(page_json)
        panel = next(p for p in curr_page.panels if p.name == triggered_id["panel"])
        idx = triggered_id["index"]
        if idx is None:
            msg = f"Malformed edit-chart id: {triggered_id}"
            raise ValueError(msg)
        chart = panel.charts[idx]
        return True, {"panel": str(triggered_id["panel"]), "index": idx}, chart.chart_type, chart.parameters

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.ADD_CHART_SUBMIT_ID, "children"),
        Output(core.ADD_CHART_MODAL_ID, "title"),
        Input(core.ADD_CHART_TARGET_ID, "data"),
        prevent_initial_call=True,
    )
    def set_modal_mode_labels(target: dict[str, Any] | None) -> tuple[str, str]:
        if not target:
            raise PreventUpdate
        if target.get("index") is None:
            return "Add chart", "Add chart"
        return "Save changes", "Edit chart"

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.ADD_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def close_add_chart_modal(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def build_chart_param_form(
    chart_type_name: str | None,
    column_kinds: dict[str, str] | None,
    initial_params: dict[str, Any] | None,
    *,
    store: DataStore[...],
    experiment_id: int,
) -> list[Component]:
    """
    Build the add/edit-chart form's fields, offering real column choices wherever possible.

    `column_kinds` comes from `COLUMN_KINDS_STORE_ID`, which only a prior suggest-charts/auto-
    populate interaction this page load actually populates -- opening the modal itself doesn't, so
    a first-time "Add chart" click (nothing else touched yet) falls back to fetching it fresh here,
    same as `auto_populate_charts`/`refresh_suggestions` already do.
    """
    if not chart_type_name:
        return []
    if not column_kinds:
        _full_df_json, column_kinds = core.compute_full_df_and_column_kinds(store, experiment_id)
    columns_by_kind = group_columns_by_kind(column_kinds or {})
    fields = models.ChartTypeRegistry.get_registered_chart_types()[chart_type_name]
    initial_params = initial_params or {}
    return [
        _param_field_input(name, field, columns_by_kind, override=initial_params.get(name))
        for name, field in fields.items()
    ]


def render_chart_preview(  # noqa: PLR0913
    chart_type_name: str | None,
    values: list[Any],
    checked_values: list[Any],
    field_ids: list[dict[str, str]],
    df_json: str | None,
    *,
    store: DataStore[...],
    experiment_id: int,
) -> tuple[Any, str]:
    """Live-preview the chart -- same `df_json`-missing fallback as `build_chart_param_form`."""
    if not chart_type_name:
        return None, ""
    if df_json is None:
        df_json, _column_kinds = core.compute_full_df_and_column_kinds(store, experiment_id)

    fields = models.ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
    parameters = core.merge_chart_param_values(values, checked_values, field_ids, fields)
    try:
        chart_instance = models.ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
        df = pd.read_json(StringIO(df_json), orient="split")
        return chart_instance.render(df), ""
    except (ValidationError, KeyError, ValueError) as exc:
        _log.exception("error rendering preview")
        return None, f"Fill in required fields to see a preview ({exc})"


def _register_form(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.ADD_CHART_PARAMS_ID, "children"),
        Input(core.ADD_CHART_TYPE_SELECT_ID, "value"),
        State(core.COLUMN_KINDS_STORE_ID, "data"),
        State(core.ADD_CHART_INITIAL_PARAMS_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def build_param_form(
        chart_type_name: str | None,
        column_kinds: dict[str, str] | None,
        initial_params: dict[str, Any] | None,
        experiment_id: int,
    ) -> list[Component]:
        return build_chart_param_form(
            chart_type_name, column_kinds, initial_params, store=get_data_store(), experiment_id=experiment_id
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.ADD_CHART_PREVIEW_ID, "children"),
        Output(core.ADD_CHART_ERROR_ID, "children"),
        Input(core.ADD_CHART_TYPE_SELECT_ID, "value"),
        Input({"type": core.CHART_PARAM_TYPE, "field": ALL}, "value"),
        Input({"type": core.CHART_PARAM_TYPE, "field": ALL}, "checked"),
        State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "id"),
        State(core.FULL_DF_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def render_preview(  # noqa: PLR0913
        chart_type_name: str | None,
        values: list[Any],
        checked_values: list[Any],
        field_ids: list[dict[str, str]],
        df_json: str | None,
        experiment_id: int,
    ) -> tuple[Any, str]:
        return render_chart_preview(
            chart_type_name,
            values,
            checked_values,
            field_ids,
            df_json,
            store=get_data_store(),
            experiment_id=experiment_id,
        )


def register_chart_editor_callbacks(app: Dash) -> None:
    _register_clear_button(app)
    _register_open_close(app)
    _register_form(app)
