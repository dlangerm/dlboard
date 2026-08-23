"""The basic experiment page: hyperparameter table + accordion of metric/artifact charts."""

from __future__ import annotations

import itertools
import typing
from io import StringIO
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, Dash, Input, NoUpdate, Output, State, ctx, dash_table, html, no_update
from dash.dash_table.Format import Format
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import ValidationError
from structlog.stdlib import get_logger

from dltrack.models import HyperParams, Page, constants
from dltrack.models._view import ChartInstance, ChartTypeRegistry, ColumnKind, PanelInstance, ParameterField
from dltrack.plugins.pages._dataframe_helpers import (
    build_artifacts_dataframe,
    build_metrics_dataframe,
    filter_excluded_runs,
    group_columns_by_kind,
    infer_column_kinds,
    merge_metrics_and_artifacts,
)
from dltrack.plugins.pages._description_editor import (
    DescriptionEditorIds,
    register_edit_callbacks,
    render_header,
)
from dltrack.serve import get_data_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component

    from dltrack.models import DataStore, Experiment

_log = get_logger(__name__)


NEW_PANEL_ID = "new-panel-button"
NEW_PANEL_CONTROLS_ID = "new-panel-controls"
NEW_PANEL_NAME_ID = "panel-name"
ACCORDION_ID = "experiment-accordion"
OPEN_PANEL_KEY: typing.Final = "open_panel"

EDIT_MODE_ID = "edit-mode-switch"
FULL_DF_STORE_ID = "full-dataframe-store"
COLUMN_KINDS_STORE_ID = "column-kinds-store"

ADD_CHART_MODAL_ID = "add-chart-modal"
LOADED_PANELS_STORE_ID = "loaded-panels-store"


class _ChartTargetData(TypedDict):
    panel: str
    index: int | None


class _ChartID(_ChartTargetData):
    type: str


ADD_CHART_TARGET_ID = "add-chart-target"
ADD_CHART_INITIAL_PARAMS_ID = "add-chart-initial-params"
ADD_CHART_TYPE_SELECT_ID = "add-chart-type-select"
ADD_CHART_PARAMS_ID = "add-chart-params"
ADD_CHART_PREVIEW_ID = "add-chart-preview"
ADD_CHART_ERROR_ID = "add-chart-error"
ADD_CHART_SUBMIT_ID = "add-chart-submit"
ADD_CHART_CANCEL_ID = "add-chart-cancel"

EXPERIMENT_DESC_IDS = DescriptionEditorIds(
    header=constants.EXPERIMENT_HEADER_ID,
    edit_button="experiment-edit-desc-button",
    modal="experiment-edit-desc-modal",
    textarea="experiment-edit-desc-textarea",
    save="experiment-edit-desc-save",
    cancel="experiment-edit-desc-cancel",
)

_METRIC_BOOKKEEPING_COLS = {"run_id", "step", "index", "timestamp_utc"}


def _experiment_display_name(experiment: Experiment) -> str:
    return experiment.name or f"Experiment {experiment.id}"


# ============================================================
# id helpers
# ============================================================


def _open_chart_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "open-add-chart", "panel": panel_name}


def _edit_chart_button_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "edit-chart", "panel": panel_name, "index": index}


def _delete_chart_button_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "delete-chart", "panel": panel_name, "index": index}


def _chart_controls_group_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "chart-controls", "panel": panel_name, "index": index}


def _edit_controls_style(*, edit_mode: bool) -> dict[str, str]:
    """Hide edit affordances entirely (rather than just disabling them) so they don't waste layout space."""
    return {} if edit_mode else {"display": "none"}


def _chart_param_id(field_name: str) -> dict[str, str]:
    return {"type": "chart-param", "field": field_name}


def _panel_content_id(panel_name: str) -> dict[str, str]:
    return {"type": "panel-content", "panel": panel_name}


def _fetch_panel_dataframe(
    store: DataStore[...], experiment_id: int, panel: PanelInstance[Any, Any], page_settings: dict[str, Any]
) -> pd.DataFrame:
    """Fetch just the data one panel needs — not the whole experiment."""
    metric_cols = panel.hint_required_columns()
    artifact_keys = panel.hint_required_artifact_keys()

    if (metric_cols is None or metric_cols) and artifact_keys:
        _log.warning(
            "Panel %r mixes metric and artifact charts, fetching both is "
            "less efficient than a panel of one kind; consider splitting it.",
            panel.name,
        )

    metrics_df = pd.DataFrame()
    if metric_cols is None:
        _log.warning("A panel had a missing hint, fetching every metric, this can be expensive")
        metrics_df = build_metrics_dataframe(store.fetch_metrics(experiment_id))
    elif metric_cols:
        metrics_df = build_metrics_dataframe(
            store.fetch_metrics(experiment_id=experiment_id, metric_name_match=metric_cols)
        )

    artifacts_df = pd.DataFrame()
    if artifact_keys:
        artifacts_df = build_artifacts_dataframe(
            store.fetch_artifacts(
                experiment_id=experiment_id, keys={k for k in artifact_keys if k is not None}
            )
        )

    df = merge_metrics_and_artifacts(metrics_df, artifacts_df)
    return filter_excluded_runs(df, page_settings)


def _render_panel_content(
    store: DataStore[...],
    experiment_id: int,
    panel: PanelInstance[Any, Any],
    page_settings: dict[str, Any],
    *,
    edit_mode: bool = False,
) -> list[Component]:
    """Fetch + render one panel's charts. Only called for panels that are actually open."""
    df = _fetch_panel_dataframe(store, experiment_id, panel, page_settings)
    return [
        dmc.Flex(_render_panel_charts(panel, df, edit_mode=edit_mode), justify="flex-start", gap="xs"),
        dmc.Button(
            id=_open_chart_button_id(panel.name),
            n_clicks=0,
            children="+",
            disabled=not edit_mode,
            style=_edit_controls_style(edit_mode=edit_mode),
        ),
    ]


def _panel_placeholder() -> dmc.Skeleton:
    return dmc.Skeleton(height=60, radius="sm")


# ============================================================
# page model
# ============================================================


class BasicExperimentPage(Page[pd.DataFrame, dmc.Accordion, html.Div], frozen=True, extra="forbid"):
    """Basic experiment page: per-panel charts rendered in an accordion."""

    @typing.override
    def retrieve_dataframes(self, store: DataStore[...], experiment_id: int) -> list[pd.DataFrame]:
        return [_fetch_panel_dataframe(store, experiment_id, p, self.page_settings) for p in self.panels]

    @typing.override
    def render(
        self, data_store: DataStore[...], experiment_id: int, *, edit_mode: bool = False
    ) -> dmc.Accordion:
        """Render the accordion. Only open panels get real content; closed ones get a placeholder."""
        open_value = self.page_settings.get(OPEN_PANEL_KEY, [self.panels[0].name] if self.panels else [])
        if isinstance(open_value, str):
            open_value = [open_value]
        open_set = set(open_value)  # pyright: ignore[reportUnknownVariableType, reportArgumentType]

        return dmc.Accordion(
            id=ACCORDION_ID,
            multiple=True,
            value=open_value,  # pyright: ignore[reportArgumentType]
            variant="contained",
            chevronPosition="left",
            children=[
                dmc.AccordionItem(
                    [
                        dmc.AccordionControl(
                            [dmc.Text(p.name, size="xs", fw=600)],
                            py="xs",
                            px="xs",
                        ),
                        dmc.AccordionPanel(
                            html.Div(
                                id=_panel_content_id(p.name),
                                children=(
                                    _render_panel_content(
                                        data_store, experiment_id, p, self.page_settings, edit_mode=edit_mode
                                    )
                                    if p.name in open_set
                                    else _panel_placeholder()
                                ),
                            ),
                            px="xs",
                            py="xs",
                        ),
                    ],
                    p.name,
                )
                for p in self.panels
            ],
        )


def _render_panel_charts(
    panel: PanelInstance[Any, Any], dataframe: pd.DataFrame, *, edit_mode: bool = False
) -> list[dmc.Stack]:
    """Render each chart in a panel with edit/delete controls above it."""
    items: list[dmc.Stack] = []
    for idx, chart in enumerate(panel.charts):
        rendered = chart.render(dataframe)
        items.append(
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            dmc.ActionIcon(
                                "✎",
                                id=_edit_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                disabled=not edit_mode,
                                variant="subtle",
                                size="xs",
                            ),
                            dmc.ActionIcon(
                                "🗑",
                                id=_delete_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                disabled=not edit_mode,
                                variant="subtle",
                                color="red",
                                size="xs",
                            ),
                        ],
                        id=_chart_controls_group_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                        justify="flex-end",
                        gap="xs",
                        style=_edit_controls_style(edit_mode=edit_mode),
                    ),
                    rendered,
                ],
                gap="xs",
                w="100%",
            )
        )
    return items


def accordion_view(
    store: DataStore[...],
    experiment_id: int,
    *,
    edit_mode: bool = False,
    full_df_json: str | None = None,
    column_kinds: dict[str, ColumnKind] | None = None,
) -> html.Div:
    """
    Accordion view for experiments.

    `edit_mode`/`full_df_json`/`column_kinds` let callers that re-render the accordion mid-edit
    (adding a panel/chart, changing run selection, etc.) carry the current edit state and cached
    dataframe forward instead of silently resetting them.
    """
    _log.debug("rendering chart for experiment %s", experiment_id)
    page = cast(
        "BasicExperimentPage", store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    )
    open_value = page.page_settings.get(OPEN_PANEL_KEY, [page.panels[0].name] if page.panels else [])
    if isinstance(open_value, str):
        open_value = [open_value]

    return html.Div(
        [
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            dmc.Switch(id=EDIT_MODE_ID, label="Edit", checked=edit_mode),
                            dmc.Collapse(
                                dmc.Group(
                                    [
                                        dmc.TextInput(id=NEW_PANEL_NAME_ID, placeholder="New Panel Name"),
                                        dmc.Button(id=NEW_PANEL_ID, n_clicks=0, children="Create"),
                                    ]
                                ),
                                id=NEW_PANEL_CONTROLS_ID,
                                opened=edit_mode,
                            ),
                        ],
                        align="flex-end",
                    ),
                    page.render(store, experiment_id, edit_mode=edit_mode),
                ],
                gap="xs",
            ),
            Store(id=LOADED_PANELS_STORE_ID, data=list(open_value)),  # pyright: ignore[reportArgumentType]
            Store(id=FULL_DF_STORE_ID, data=full_df_json),
            Store(id=COLUMN_KINDS_STORE_ID, data=column_kinds),
            _add_chart_modal(),
        ],
    )


# ============================================================
# page-mutation helpers (shared by multiple callbacks below)
# ============================================================


def _persist_settings_and_rerender(  # noqa: PLR0913
    store: DataStore[...],
    experiment_id: int,
    updates: dict[str, Any],
    *,
    edit_mode: bool = False,
    full_df_json: str | None = None,
    column_kinds: dict[str, ColumnKind] | None = None,
) -> tuple[BasicExperimentPage, dmc.Container]:
    """Merge `updates` into page_settings (server-authoritative), persist, and re-render the accordion."""
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    new_settings = {**page.page_settings, **updates}
    page = page.model_copy(update={"page_settings": new_settings})
    page = store.update_page(page)
    container = accordion_view(
        store,
        experiment_id=experiment_id,
        edit_mode=edit_mode,
        full_df_json=full_df_json,
        column_kinds=column_kinds,
    )
    return page, container  # pyright: ignore[reportReturnType]


def _mutate_panels_and_rerender(  # noqa: PLR0913
    page_json: str,
    experiment_id: int,
    mutate: Callable[[list[PanelInstance[Any, Any]]], list[PanelInstance[Any, Any]]],
    *,
    edit_mode: bool = False,
    full_df_json: str | None = None,
    column_kinds: dict[str, ColumnKind] | None = None,
) -> tuple[BasicExperimentPage, html.Div]:
    """Load page from client-cached state, apply `mutate` to its panels, persist, and re-render."""
    curr_page = BasicExperimentPage.model_validate_json(page_json)
    curr_page = curr_page.model_copy(update={"panels": mutate(curr_page.panels)})
    store = get_data_store()
    curr_page = store.update_page(curr_page)
    container = accordion_view(
        store,
        experiment_id=experiment_id,
        edit_mode=edit_mode,
        full_df_json=full_df_json,
        column_kinds=column_kinds,
    )
    return curr_page, container  # pyright: ignore[reportReturnType]


def _upsert_chart(
    panel: PanelInstance[Any, Any], panel_name: str, index: int | None, chart: ChartInstance[Any, Any]
) -> PanelInstance[Any, Any]:
    """Add `chart` to `panel` (if index is None) or replace the chart at `index`."""
    if panel.name != panel_name:
        return panel
    if index is None:
        return panel.model_copy(update={"charts": [*panel.charts, chart]})
    new_charts = list(panel.charts)
    new_charts[index] = chart
    return panel.model_copy(update={"charts": new_charts})


def _merge_chart_param_values(
    values: list[Any], checked_values: list[Any], field_ids: list[dict[str, str]]
) -> dict[str, Any]:
    """NumberInput/TextInput/Select report via `value`; Switch reports via `checked`. Merge them by field id."""
    merged = [v if v is not None else c for v, c in zip(values, checked_values, strict=True)]
    return {fid["field"]: val for fid, val in zip(field_ids, merged, strict=True)}


# ============================================================
# add/edit-chart modal
# ============================================================


def _param_field_input(
    field_name: str,
    field: ParameterField,
    columns_by_kind: dict[ColumnKind, list[str]],
    *,
    override: bool | int | float | str | None = None,
) -> Component:
    input_id = _chart_param_id(field_name)
    label = f"{field_name} *" if field.required else field_name
    value = override if override is not None else field.default

    if field.type == "bool":
        return dmc.Switch(id=input_id, label=label, checked=bool(value) if value is not None else False)
    if field.type in ("int", "float"):
        return dmc.NumberInput(id=input_id, label=label, value=value, step=1 if field.type == "int" else 0.1)

    options = columns_by_kind.get(field.column_kind, []) if field.column_kind is not None else None
    if options:
        return dmc.Select(id=input_id, label=label, data=sorted(options), value=value, searchable=True)  # pyright: ignore[reportArgumentType]
    return dmc.TextInput(id=input_id, label=label, value=value or "")  # pyright: ignore[reportArgumentType]


def _add_chart_modal() -> dmc.Modal:
    chart_types = sorted(ChartTypeRegistry.get_registered_chart_types().keys())
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
                    html.Div(id=ADD_CHART_PARAMS_ID),
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


# ============================================================
# hparam / metrics table
# ============================================================


def _fetch_last_step_metrics(store: DataStore[...], experiment_id: int) -> dict[int, dict[str, Any]]:
    """One row per run: metric values at that run's highest logged step."""
    df = build_metrics_dataframe(store.fetch_metrics(experiment_id))
    if df.empty:
        return {}
    last_rows = df.loc[df.groupby("run_id")["step"].idxmax()]
    metric_cols = [c for c in df.columns if c not in _METRIC_BOOKKEEPING_COLS]
    return {
        int(row["run_id"]): {c: row[c] for c in metric_cols if pd.notna(row[c])}
        for _, row in last_rows.iterrows()
    }


def _build_hparam_rows(
    hydrated: list[HyperParams], last_step_metrics: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in hydrated:
        row: dict[str, Any] = {"run_id": run.id}
        row.update(run.hparams_dict)
        row.update(last_step_metrics.get(run.id, {}))
        rows.append(row)
    return rows


def _load_hparam_view_data(
    store: DataStore[...], experiment_id: int, hparams: list[str]
) -> tuple[list[HyperParams], list[str], list[str], list[dict[str, Any]]]:
    """Hydrate hparams, compute last-step metrics, and build the combined rows the datatable needs."""
    hydrated = [HyperParams.model_validate_json(run) for run in hparams]
    hparam_keys = sorted(set(itertools.chain(*[list(k.hparams_dict.keys()) for k in hydrated])))
    last_step_metrics = _fetch_last_step_metrics(store, experiment_id)
    metric_keys = sorted({k for m in last_step_metrics.values() for k in m})
    rows = _build_hparam_rows(hydrated, last_step_metrics)
    return hydrated, hparam_keys, metric_keys, rows


def _infer_dtype[T](rows: list[dict[T, Any]], key: T) -> str:
    for row in rows:
        val = row.get(key)
        if val is not None:
            return "numeric" if isinstance(val, (int, float)) and not isinstance(val, bool) else "text"
    return "text"


def _build_hparam_datatable(
    rows: list[dict[str, Any]], selected: list[str] | list[int], excluded: list[int] | list[str]
) -> dash_table.DataTable:
    columns: list[dict[str, Any]] = [{"name": "Run", "id": "run_id", "type": "numeric"}]
    for key in selected:
        col: dict[str, Any] = {"name": key, "id": key}
        if _infer_dtype(rows, str(key)) == "numeric":
            col["type"] = "numeric"
            col["format"] = Format(precision=3, scheme="s")
        columns.append(col)

    selected_rows = [i for i, row in enumerate(rows) if row["run_id"] not in excluded]

    return dash_table.DataTable(
        id=constants.HPARAM_DATATABLE_ID,
        columns=columns,  # pyright: ignore[reportArgumentType]
        data=rows,  # pyright: ignore[reportArgumentType]
        row_selectable="multi",
        selected_rows=selected_rows,
        filter_action="native",
        sort_action="native",
        page_action="native",
        page_size=20,
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": "var(--mantine-color-default-hover)",
            "color": "var(--mantine-color-text)",
            "fontFamily": "var(--mantine-font-family)",
            "fontWeight": 600,
            "border": "none",
            "borderBottom": "1px solid var(--mantine-color-default-border)",
        },
        style_cell={
            "backgroundColor": "var(--mantine-color-body)",
            "color": "var(--mantine-color-text)",
            "fontFamily": "var(--mantine-font-family)",
            "border": "none",
            "borderBottom": "1px solid var(--mantine-color-default-border)",
            "padding": "6px 10px",
        },
        style_data_conditional=[
            {
                "if": {"state": "selected"},
                "backgroundColor": "var(--mantine-primary-color-light)",
                "border": "1px solid var(--mantine-primary-color-filled)",
            },
            {
                "if": {"row_index": "odd"},
                "backgroundColor": "var(--mantine-color-default-hover)",
            },
        ],
        css=[
            {
                "selector": ".dash-filter input",
                "rule": (
                    "background-color: var(--mantine-color-body);"
                    "color: var(--mantine-color-text);"
                    "border: 1px solid var(--mantine-color-default-border);"
                    "border-radius: 4px;"
                ),
            },
            {
                "selector": ".dash-spreadsheet-pagination",
                "rule": "color: var(--mantine-color-text);",
            },
            {
                "selector": ".dash-spreadsheet-pagination button",
                "rule": (
                    "background-color: var(--mantine-color-default-hover);"
                    "color: var(--mantine-color-text);"
                    "border: 1px solid var(--mantine-color-default-border);"
                ),
            },
        ],
    )


# ============================================================
# callbacks
# ============================================================


def plug(app: Dash) -> None:  # noqa: C901, PLR0915
    """Plugin for the basic experiment page: hparam table + chart accordion + editor."""

    # --- initial render: fills METRIC_CONTENT_ID/EXPERIMENT_HEADER_ID and seeds STATE_PAGE_STORAGE ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.EXPERIMENT_HEADER_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call="initial_update",
    )
    def render_initial(experiment_id: int) -> tuple[html.Div, Component, str]:
        store = get_data_store()
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        exp = store.get_experiment(experiment_id)
        name, description = (_experiment_display_name(exp), exp.description) if exp else ("", "")
        header = render_header(EXPERIMENT_DESC_IDS, title=name, description=description)
        return accordion_view(store, experiment_id=experiment_id), header, page.model_dump_json()

    def _fetch_experiment_header(experiment_id: int) -> tuple[str, str]:
        store = get_data_store()
        exp = store.get_experiment(experiment_id)
        if exp is None:
            msg = f"Experiment {experiment_id} not found"
            raise ValueError(msg)
        return _experiment_display_name(exp), exp.description

    def _save_experiment_description(experiment_id: int, description: str) -> tuple[str, str]:
        store = get_data_store()
        exp = store.get_experiment(experiment_id)
        if exp is None:
            msg = f"Experiment {experiment_id} not found"
            raise ValueError(msg)
        updated = store.update_experiment(exp.model_copy(update={"description": description}))
        return _experiment_display_name(updated), updated.description

    register_edit_callbacks(
        app,
        EXPERIMENT_DESC_IDS,
        State(constants.STATE_EXPERIMENT_ID, "data"),
        fetch=_fetch_experiment_header,
        save=_save_experiment_description,
    )

    # --- hparam table ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.HPARAM_TABLE_ID, "children"),
        Input(constants.STATE_HPARAMS, "data"),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
        prevent_initial_callback=True,
    )
    def render_hparams(hparams: list[str], experiment_id: int | None) -> html.Div:
        if experiment_id is None:
            raise PreventUpdate

        store = get_data_store()
        _hydrated, hparam_keys, metric_keys, rows = _load_hparam_view_data(store, experiment_id, hparams)
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        excluded = page.page_settings.get(constants.EXCLUDED_RUNS_KEY, [])
        assert isinstance(excluded, list)
        selected = page.page_settings.get(constants.SELECTED_HPARAM_COLS_KEY, [])
        assert isinstance(selected, list)

        summary_bits = [dmc.Text(f"{len(rows)} runs", size="sm", fw=500)]
        if excluded:
            summary_bits.append(dmc.Text(f"{len(excluded)} excluded", size="sm", c="dimmed"))

        return html.Div(
            [
                dmc.Group(
                    [
                        dmc.Group(summary_bits, gap="xs"),
                        dmc.Button("Runs", id=constants.HPARAM_DRAWER_TOGGLE_ID, size="xs", variant="light"),
                    ],
                    justify="space-between",
                ),
                dmc.Drawer(
                    id=constants.HPARAM_DRAWER_ID,
                    title="Run Metrics and Parameters",
                    position="right",
                    size="xl",
                    opened=False,
                    children=[
                        dmc.MultiSelect(
                            id=constants.HPARAM_COL_SELECT_ID,
                            label="Columns",
                            data=[
                                {"group": "Hyperparameters", "items": hparam_keys},
                                {"group": "Metrics (last step)", "items": metric_keys},
                            ],
                            value=[k for k in selected if k in hparam_keys or k in metric_keys],
                            searchable=True,
                            clearable=True,
                        ),
                        html.Div(
                            id=constants.HPARAM_TABLE_BODY_ID,
                            children=_build_hparam_datatable(rows, selected, excluded),
                        ),
                    ],
                ),
            ]
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.HPARAM_TABLE_BODY_ID, "children"),
        Input(constants.HPARAM_COL_SELECT_ID, "value"),
        State(constants.STATE_HPARAMS, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def render_hparam_table_body(
        selected: list[str], hparams: list[str], experiment_id: int
    ) -> dash_table.DataTable:
        store = get_data_store()
        _hydrated, _hparam_keys, _metric_keys, rows = _load_hparam_view_data(store, experiment_id, hparams)
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        excluded = page.page_settings.get(constants.EXCLUDED_RUNS_KEY, [])
        assert isinstance(excluded, list)
        return _build_hparam_datatable(rows, selected, excluded)

    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        "function(n, opened) { return n ? !opened : window.dash_clientside.no_update; }",
        Output(constants.HPARAM_DRAWER_ID, "opened"),
        Input(constants.HPARAM_DRAWER_TOGGLE_ID, "n_clicks"),
        State(constants.HPARAM_DRAWER_ID, "opened"),
        prevent_initial_call=True,
    )

    # --- page_settings-only mutations, via _persist_settings_and_rerender ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(constants.HPARAM_COL_SELECT_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def persist_selected_hparam_cols(selected: list[str], experiment_id: int) -> str:
        store = get_data_store()
        page, _container = _persist_settings_and_rerender(
            store, experiment_id, {constants.SELECTED_HPARAM_COLS_KEY: selected}
        )
        return page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Input(constants.HPARAM_DATATABLE_ID, "selected_rows"),
        State(constants.HPARAM_DATATABLE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(EDIT_MODE_ID, "checked", allow_optional=True),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def sync_run_selection(  # noqa: PLR0913
        selected_rows: list[int] | None,
        table_data: list[dict[str, Any]] | None,
        experiment_id: int,
        edit_mode: bool | None,  # noqa: FBT001
        full_df_json: str | None,
        column_kinds: dict[str, ColumnKind] | None,
    ) -> tuple[str, dmc.Container]:
        if selected_rows is None or table_data is None:
            raise PreventUpdate
        selected_ids = {table_data[i]["run_id"] for i in selected_rows}
        all_ids = {row["run_id"] for row in table_data}
        excluded = sorted(all_ids - selected_ids)

        store = get_data_store()
        page, container = _persist_settings_and_rerender(
            store,
            experiment_id,
            {constants.EXCLUDED_RUNS_KEY: excluded},
            edit_mode=bool(edit_mode),
            full_df_json=full_df_json,
            column_kinds=column_kinds,
        )
        return page.model_dump_json(), container

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(ACCORDION_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def persist_open_panel(open_value: str | list[str] | None, experiment_id: int) -> str:
        store = get_data_store()
        page, _container = _persist_settings_and_rerender(
            store, experiment_id, {OPEN_PANEL_KEY: open_value or []}
        )
        return page.model_dump_json()

    # --- panel/chart mutations, via _mutate_panels_and_rerender ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(NEW_PANEL_ID, "n_clicks"),
        Input(constants.STATE_EXPERIMENT_ID, "data"),
        State(NEW_PANEL_NAME_ID, "value"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(EDIT_MODE_ID, "checked", allow_optional=True),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def create_panel(  # noqa: PLR0913
        n_clicks: int,
        experiment_id: int,
        panel_name: str,
        page_json: str,
        edit_mode: bool | None,  # noqa: FBT001
        full_df_json: str | None,
        column_kinds: dict[str, ColumnKind] | None,
    ) -> tuple[html.Div, str]:
        if not n_clicks:
            raise PreventUpdate
        if not panel_name:
            msg = "Panel name cannot be empty"
            raise ValueError(msg)

        def add_panel(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [*panels, PanelInstance(name=panel_name)]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            add_panel,
            edit_mode=bool(edit_mode),
            full_df_json=full_df_json,
            column_kinds=column_kinds,
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_ERROR_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(ADD_CHART_SUBMIT_ID, "n_clicks"),
        State(ADD_CHART_TYPE_SELECT_ID, "value"),
        State({"type": "chart-param", "field": ALL}, "value"),
        State({"type": "chart-param", "field": ALL}, "checked"),
        State({"type": "chart-param", "field": ALL}, "id"),
        State(ADD_CHART_TARGET_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(EDIT_MODE_ID, "checked", allow_optional=True),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def add_chart(  # noqa: PLR0913
        n_clicks: int,
        chart_type_name: str | None,
        values: list[Any],
        checked_values: list[Any],
        field_ids: list[dict[str, str]],
        target: _ChartTargetData | None,
        experiment_id: int,
        page_json: str,
        edit_mode: bool | None,  # noqa: FBT001
        full_df_json: str | None,
        column_kinds: dict[str, ColumnKind] | None,
    ) -> tuple[Any, bool, str, str | NoUpdate]:
        if not n_clicks or not chart_type_name or not target:
            raise PreventUpdate

        parameters = _merge_chart_param_values(values, checked_values, field_ids)
        try:
            new_chart = ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
        except ValidationError as exc:
            return no_update, True, f"Invalid parameters: {exc}", no_update

        panel_name, index = target["panel"], target.get("index")

        def apply_chart(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [_upsert_chart(p, panel_name, index, new_chart) for p in panels]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            apply_chart,
            edit_mode=bool(edit_mode),
            full_df_json=full_df_json,
            column_kinds=column_kinds,
        )
        return container, False, "", page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(constants.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "delete-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(EDIT_MODE_ID, "checked", allow_optional=True),
        State(FULL_DF_STORE_ID, "data", allow_optional=True),
        State(COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def delete_chart(
        _n_clicks_list: list[int],
        page_json: str,
        experiment_id: int,
        edit_mode: bool | None,  # noqa: FBT001
        full_df_json: str | None,
        column_kinds: dict[str, ColumnKind] | None,
    ) -> tuple[html.Div, str]:
        triggered_id = cast("_ChartTargetData", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        if not triggered_id or not ctx.triggered[0]["value"]:
            raise PreventUpdate
        panel_name, index = triggered_id["panel"], triggered_id["index"]

        def remove_chart(panels: list[PanelInstance[Any, Any]]) -> list[PanelInstance[Any, Any]]:
            return [
                p
                if p.name != panel_name
                else p.model_copy(update={"charts": [c for i, c in enumerate(p.charts) if i != index]})
                for p in panels
            ]

        page, container = _mutate_panels_and_rerender(
            page_json,
            experiment_id,
            remove_chart,
            edit_mode=bool(edit_mode),
            full_df_json=full_df_json,
            column_kinds=column_kinds,
        )
        return container, page.model_dump_json()

    # --- edit mode: fetch the full dataframe once, cache it client-side, enable controls ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(FULL_DF_STORE_ID, "data"),
        Output(COLUMN_KINDS_STORE_ID, "data"),
        Output({"type": "open-add-chart", "panel": ALL}, "disabled"),
        Output({"type": "open-add-chart", "panel": ALL}, "style"),
        Output({"type": "edit-chart", "panel": ALL, "index": ALL}, "disabled"),
        Output({"type": "delete-chart", "panel": ALL, "index": ALL}, "disabled"),
        Output({"type": "chart-controls", "panel": ALL, "index": ALL}, "style"),
        Output(NEW_PANEL_CONTROLS_ID, "opened"),
        Input(EDIT_MODE_ID, "checked"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data"),
        State({"type": "open-add-chart", "panel": ALL}, "disabled"),
        State({"type": "edit-chart", "panel": ALL, "index": ALL}, "disabled"),
        State({"type": "delete-chart", "panel": ALL, "index": ALL}, "disabled"),
        State({"type": "chart-controls", "panel": ALL, "index": ALL}, "id"),
        prevent_initial_call=True,
    )
    def toggle_edit_mode(  # noqa: PLR0913
        checked: bool,  # noqa: FBT001
        experiment_id: int,
        existing_df_json: str | None,
        add_disabled: list[bool],
        edit_disabled: list[bool],
        delete_disabled: list[bool],
        controls_ids: list[dict[str, Any]],
    ) -> tuple[
        str | NoUpdate | None,
        dict[str, ColumnKind] | NoUpdate,
        list[bool],
        list[dict[str, str]],
        list[bool],
        list[bool],
        list[dict[str, str]],
        bool,
    ]:
        add_style = [_edit_controls_style(edit_mode=checked)] * len(add_disabled)
        controls_style = [_edit_controls_style(edit_mode=checked)] * len(controls_ids)
        if not checked:
            return (
                no_update,
                no_update,
                [True] * len(add_disabled),
                add_style,
                [True] * len(edit_disabled),
                [True] * len(delete_disabled),
                controls_style,
                False,
            )
        if existing_df_json is not None:
            return (
                no_update,
                no_update,
                [False] * len(add_disabled),
                add_style,
                [False] * len(edit_disabled),
                [False] * len(delete_disabled),
                controls_style,
                True,
            )

        store = get_data_store()
        metrics_df = build_metrics_dataframe(store.fetch_metrics(experiment_id))
        artifacts = list(store.fetch_artifacts(experiment_id=experiment_id))
        artifacts_df = build_artifacts_dataframe(artifacts)
        df = merge_metrics_and_artifacts(metrics_df, artifacts_df)
        column_kinds = infer_column_kinds(metrics_df.columns, {a.key for a in artifacts})
        return (
            df.to_json(orient="split"),
            column_kinds,
            [False] * len(add_disabled),
            add_style,
            [False] * len(edit_disabled),
            [False] * len(delete_disabled),
            controls_style,
            True,
        )

    # --- open the modal, either to add a new chart or edit an existing one ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_TARGET_ID, "data"),
        Output(ADD_CHART_TYPE_SELECT_ID, "value"),
        Output(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        Input({"type": "open-add-chart", "panel": ALL}, "n_clicks"),
        Input({"type": "edit-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        State(constants.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def open_chart_modal(
        _add_clicks: list[int], _edit_clicks: list[int], page_json: str
    ) -> tuple[bool, _ChartTargetData, str | None, dict[str, Any]]:
        triggered_id = cast("_ChartID", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        if not triggered_id or not ctx.triggered[0]["value"]:
            raise PreventUpdate

        if triggered_id["type"] == "open-add-chart":
            return True, {"panel": str(triggered_id["panel"]), "index": None}, None, {}

        curr_page = BasicExperimentPage.model_validate_json(page_json)
        panel = next(p for p in curr_page.panels if p.name == triggered_id["panel"])
        idx = triggered_id["index"]
        if idx is None:
            msg = f"Malformed edit-chart id: {triggered_id}"
            raise ValueError(msg)
        chart = panel.charts[idx]
        return True, {"panel": str(triggered_id["panel"]), "index": idx}, chart.chart_type, chart.parameters

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
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

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(ADD_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def close_add_chart_modal(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_PARAMS_ID, "children"),
        Input(ADD_CHART_TYPE_SELECT_ID, "value"),
        State(COLUMN_KINDS_STORE_ID, "data"),
        State(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        prevent_initial_call=True,
    )
    def build_param_form(
        chart_type_name: str | None,
        column_kinds: dict[str, str] | None,
        initial_params: dict[str, Any] | None,
    ) -> list[Component]:
        if not chart_type_name:
            return []
        columns_by_kind = group_columns_by_kind(column_kinds or {})
        fields = ChartTypeRegistry.get_registered_chart_types()[chart_type_name]
        initial_params = initial_params or {}
        return [
            _param_field_input(name, field, columns_by_kind, override=initial_params.get(name))
            for name, field in fields.items()
        ]

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_PREVIEW_ID, "children"),
        Output(ADD_CHART_ERROR_ID, "children"),
        Input(ADD_CHART_TYPE_SELECT_ID, "value"),
        Input({"type": "chart-param", "field": ALL}, "value"),
        Input({"type": "chart-param", "field": ALL}, "checked"),
        State({"type": "chart-param", "field": ALL}, "id"),
        State(FULL_DF_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def render_preview(
        chart_type_name: str | None,
        values: list[Any],
        checked_values: list[Any],
        field_ids: list[dict[str, str]],
        df_json: str | None,
    ) -> tuple[Any, str]:
        if not chart_type_name or df_json is None:
            return None, ""

        parameters = _merge_chart_param_values(values, checked_values, field_ids)
        try:
            chart_instance = ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
            df = pd.read_json(StringIO(df_json), orient="split")
            return chart_instance.render(df), ""
        except (ValidationError, KeyError, ValueError) as exc:
            _log.exception("error rendering preview")
            return None, f"Fill in required fields to see a preview ({exc})"

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output({"type": "panel-content", "panel": ALL}, "children"),
        Output(LOADED_PANELS_STORE_ID, "data"),
        Input(ACCORDION_ID, "value"),
        State({"type": "panel-content", "panel": ALL}, "id"),
        State(LOADED_PANELS_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(EDIT_MODE_ID, "checked", allow_optional=True),
        prevent_initial_call=True,
    )
    def render_opened_panels(
        open_value: list[str] | None,
        panel_ids: list[dict[str, str]],
        loaded: list[str] | None,
        experiment_id: int,
        edit_mode: bool | None,  # noqa: FBT001
    ) -> tuple[list[Any], list[str]]:
        open_set = set(open_value or [])
        loaded_set = set(loaded or [])
        newly_opened = open_set - loaded_set
        if not newly_opened:
            raise PreventUpdate

        store = get_data_store()
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        panel_by_name = {p.name: p for p in page.panels}

        outputs: list[Any] = []
        for pid in panel_ids:
            name = pid["panel"]
            if name not in newly_opened or name not in panel_by_name:
                outputs.append(no_update)
                continue
            outputs.append(
                _render_panel_content(
                    store, experiment_id, panel_by_name[name], page.page_settings, edit_mode=bool(edit_mode)
                )
            )
        return outputs, sorted(loaded_set | newly_opened)
