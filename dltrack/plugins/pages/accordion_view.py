"""A plugin for a basic metric chart using plotly."""

from __future__ import annotations

import typing
from io import StringIO
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, Dash, Input, NoUpdate, Output, State, ctx, html, no_update
from dash.dcc import Interval, Store  # extend existing `from dash.dcc import Store`
from dash.exceptions import PreventUpdate
from pydantic import ValidationError
from structlog.stdlib import get_logger

from dltrack.models import Artifact, Page, constants
from dltrack.models._metric import LoggedMetrics
from dltrack.models._view import ChartInstance, ChartTypeRegistry, ColumnKind, PanelInstance, ParameterField
from dltrack.plugins.utilities import get_data_store

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import DataStore

_log = get_logger(__name__)

NEW_PANEL_ID = "new-panel-button"
NEW_PANEL_NAME_ID = "panel-name"
ACCORDION_ID = "experiment-accordion"
OPEN_PANEL_KEY: typing.Final = "open_panel"

AUTO_REFRESH_INTERVAL_ID = "auto-refresh-interval"
AUTO_REFRESH_MS = 5000

# --- edit mode / full dataframe cache ---
EDIT_MODE_ID = "edit-mode-switch"
FULL_DF_STORE_ID = "full-dataframe-store"
COLUMN_KINDS_STORE_ID = "column-kinds-store"

# --- add/edit-chart modal ---
ADD_CHART_MODAL_ID = "add-chart-modal"


class _ChartTargetData(TypedDict):
    panel: str
    index: int | None


class _ChartID(_ChartTargetData):
    type: str


ADD_CHART_TARGET_ID = "add-chart-target"  # {"panel": str, "index": int | None}
ADD_CHART_INITIAL_PARAMS_ID = "add-chart-initial-params"
ADD_CHART_TYPE_SELECT_ID = "add-chart-type-select"
ADD_CHART_PARAMS_ID = "add-chart-params"
ADD_CHART_PREVIEW_ID = "add-chart-preview"
ADD_CHART_ERROR_ID = "add-chart-error"
ADD_CHART_SUBMIT_ID = "add-chart-submit"
ADD_CHART_CANCEL_ID = "add-chart-cancel"


def _open_chart_button_id(panel_name: str) -> dict[str, str]:
    return {"type": "open-add-chart", "panel": panel_name}


def _edit_chart_button_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "edit-chart", "panel": panel_name, "index": index}


def _delete_chart_button_id(panel_name: str, index: int) -> _ChartID:
    return {"type": "delete-chart", "panel": panel_name, "index": index}


def _chart_param_id(field_name: str) -> dict[str, str]:
    return {"type": "chart-param", "field": field_name}


def _filter_excluded_runs(df: pd.DataFrame, page_settings: dict[str, Any]) -> pd.DataFrame:
    excluded = set(page_settings.get(constants.EXCLUDED_RUNS_KEY) or [])
    if not excluded or df.empty:
        return df
    return df[~df["run_id"].isin(excluded)]


def _build_artifacts_dataframe(artifacts: typing.Iterable[Artifact]) -> pd.DataFrame:
    """Pivot artifact refs (and tags) into columns per key, indexed by (run_id, step)."""
    df = pd.DataFrame([a.model_dump(mode="json") for a in artifacts])
    if df.empty:
        return df

    ref_pivot = df.pivot_table(index=["run_id", "step"], columns="key", values="ref", aggfunc="first")
    tags_pivot = df.pivot_table(index=["run_id", "step"], columns="key", values="tags", aggfunc="first")
    tags_pivot.columns = [f"{c}__tags" for c in tags_pivot.columns]

    return ref_pivot.join(tags_pivot).reset_index()


def _infer_column_kinds(
    metric_columns: typing.Iterable[str], artifact_keys: typing.Iterable[str]
) -> dict[str, ColumnKind]:
    """
    Tag every known column with its kind.

    run_id is excluded (never a sensible field value);
    step is kept since it's a common x_axis choice.
    """
    kinds = {c: ColumnKind.METRIC for c in metric_columns if c not in ("run_id", "index")}
    kinds.update(dict.fromkeys(artifact_keys, ColumnKind.ARTIFACT))
    return kinds


def _group_columns_by_kind(column_kinds: dict[str, str]) -> dict[ColumnKind, list[str]]:
    """Round-trip Store data (plain strings) back into ColumnKind-keyed groups."""
    grouped: dict[ColumnKind, list[str]] = {}
    for col, kind in column_kinds.items():
        grouped.setdefault(ColumnKind(kind), []).append(col)
    return grouped


class BasicExperimentPage(Page[pd.DataFrame, dmc.Accordion, html.Div], frozen=True, extra="forbid"):
    """Basic experiment page."""

    @typing.override
    def retrieve_dataframes(
        self,
        store: DataStore[...],
        experiment_id: int,
    ) -> list[pd.DataFrame]:
        """Retrieve one dataframe per panel, fetching only what each panel needs."""
        dataframes: list[pd.DataFrame] = []
        for panel in self.panels:
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
                metrics_df = _build_metrics_dataframe(store.fetch_metrics(experiment_id))
            elif metric_cols:
                metrics_df = _build_metrics_dataframe(
                    store.fetch_metrics(experiment_id=experiment_id, metric_name_match=metric_cols)
                )

            artifacts_df = pd.DataFrame()
            if artifact_keys:
                artifacts_df = _build_artifacts_dataframe(
                    store.fetch_artifacts(experiment_id=experiment_id, keys=artifact_keys)
                )

            if not metrics_df.empty and not artifacts_df.empty:
                df = metrics_df.merge(artifacts_df, on=["run_id", "step"], how="outer")
            else:
                df = metrics_df if not metrics_df.empty else artifacts_df

            dataframes.append(_filter_excluded_runs(df, self.page_settings))

        return dataframes

    @typing.override
    def render(
        self,
        data_store: DataStore[...],
        experiment_id: int,
    ) -> dmc.Accordion:
        """Render."""
        open_value = self.page_settings.get(OPEN_PANEL_KEY, [self.panels[0].name] if self.panels else [])
        if isinstance(open_value, str):
            open_value = [open_value]
        return dmc.Accordion(
            id=ACCORDION_ID,
            multiple=True,
            value=open_value,  # pyright: ignore[reportArgumentType]
            children=[
                dmc.AccordionItem(
                    [
                        dmc.AccordionControl(
                            [
                                dmc.Flex(
                                    [
                                        dmc.Text(p.name, size="l"),
                                    ],
                                    justify="space-between",
                                ),
                            ]
                        ),
                        dmc.AccordionPanel(
                            [
                                dmc.Flex(
                                    _render_panel_charts(p, d),
                                    justify="flex-start",
                                    gap="sm",
                                ),
                                dmc.Button(
                                    id=_open_chart_button_id(p.name),
                                    n_clicks=0,
                                    children="+",
                                    disabled=True,  # enabled once edit mode is on
                                ),
                            ]
                        ),
                    ],
                    p.name,
                )
                for p, d in zip(
                    self.panels,
                    self.retrieve_dataframes(data_store, experiment_id),
                    strict=True,
                )
            ],
        )


def _build_metrics_dataframe(metrics: typing.Iterable[LoggedMetrics]) -> pd.DataFrame:
    """Build a metrics dataframe from an iterable of LoggedMetrics."""
    rows = [
        d.metrics | {f: getattr(d, f) for f in LoggedMetrics.model_fields if f != "metrics"} for d in metrics
    ]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_records(rows).reset_index()


def _render_panel_charts(panel: PanelInstance[Any, Any], dataframe: pd.DataFrame) -> list[dmc.Stack]:
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
                                disabled=True,
                                variant="subtle",
                                size="sm",
                            ),
                            dmc.ActionIcon(
                                "🗑",
                                id=_delete_chart_button_id(panel.name, idx),  # pyright: ignore[reportArgumentType]
                                n_clicks=0,
                                disabled=True,
                                variant="subtle",
                                color="red",
                                size="sm",
                            ),
                        ],
                        justify="flex-end",
                        gap="xs",
                    ),
                    rendered,
                ],
                gap="xs",
                w="100%",
            )
        )
    return items


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
        return dmc.NumberInput(
            id=input_id,
            label=label,
            value=value,
            step=1 if field.type == "int" else 0.1,
        )

    options = columns_by_kind.get(field.column_kind, []) if field.column_kind is not None else None
    if options:
        return dmc.Select(id=input_id, label=label, data=sorted(options), value=value, searchable=True)
    return dmc.TextInput(id=input_id, label=label, value=value or "")


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
                    dmc.Select(
                        id=ADD_CHART_TYPE_SELECT_ID,
                        label="Chart type",
                        data=chart_types,
                        value=None,
                    ),
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


def accordion_view(store: DataStore[...], experiment_id: int) -> dmc.Container:
    """Render a metric chart using an accordion with organized prefixes."""
    _log.info("rendering chart for experiment %s", experiment_id)
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    return dmc.Container(
        [
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            dmc.Switch(id=EDIT_MODE_ID, label="Edit", checked=False),
                            dmc.TextInput(id=NEW_PANEL_NAME_ID, placeholder="New Panel Name"),
                            dmc.Button(id=NEW_PANEL_ID, n_clicks=0, children="Create"),
                        ],
                        align="flex-end",
                    ),
                    page.render(store, experiment_id),
                ],
            ),
            Store(id="current-page", data=page.model_dump_json()),
            Store(id=FULL_DF_STORE_ID),
            Store(id=COLUMN_KINDS_STORE_ID),
            Interval(id=AUTO_REFRESH_INTERVAL_ID, interval=AUTO_REFRESH_MS, n_intervals=0),
            _add_chart_modal(),
        ],
    )


def plug(app: Dash) -> None:  # noqa: C901, PLR0915
    """Plugin for viewing a basic chart for all metrics."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children", allow_duplicate=True),
        Input(constants.STATE_EXPERIMENT_ID, component_property="data"),
        prevent_initial_call="initial_update",
    )
    def fun(experiment_id: int) -> dmc.Container:
        store = get_data_store()
        return accordion_view(store, experiment_id=experiment_id)

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children", allow_duplicate=True),
        Input(component_id=NEW_PANEL_ID, component_property="n_clicks"),
        Input(constants.STATE_EXPERIMENT_ID, component_property="data"),
        State(component_id=NEW_PANEL_NAME_ID, component_property="value"),
        State(component_id="current-page", component_property="data"),
        prevent_initial_call=True,
    )
    def create_panel(
        n_clicks: int,
        experiment_id: int,
        panel_name: str,
        page_json: str,
    ) -> dmc.Container:
        if n_clicks > 0:
            if not panel_name:
                msg = "Panel name cannot be empty"
                raise ValueError(msg)
            curr_page = BasicExperimentPage.model_validate_json(page_json)
            curr_page.panels.append(PanelInstance(name=panel_name))
            store = get_data_store()
            store.update_page(curr_page)
            store = get_data_store()
            return accordion_view(store, experiment_id=experiment_id)
        raise PreventUpdate

    # --- edit mode: fetch the full dataframe once, cache it client-side, enable controls ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(FULL_DF_STORE_ID, "data"),
        Output(COLUMN_KINDS_STORE_ID, "data"),
        Output({"type": "open-add-chart", "panel": ALL}, "disabled"),
        Output({"type": "edit-chart", "panel": ALL, "index": ALL}, "disabled"),
        Output({"type": "delete-chart", "panel": ALL, "index": ALL}, "disabled"),
        Input(EDIT_MODE_ID, "checked"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(FULL_DF_STORE_ID, "data"),
        State({"type": "open-add-chart", "panel": ALL}, "disabled"),
        State({"type": "edit-chart", "panel": ALL, "index": ALL}, "disabled"),
        State({"type": "delete-chart", "panel": ALL, "index": ALL}, "disabled"),
        prevent_initial_call=True,
    )
    def toggle_edit_mode(  # noqa: PLR0913
        checked: bool,  # noqa: FBT001
        experiment_id: int,
        existing_df_json: str | None,
        add_disabled: list[bool],
        edit_disabled: list[bool],
        delete_disabled: list[bool],
    ) -> tuple[str | NoUpdate | None, dict[str, ColumnKind] | NoUpdate, list[bool], list[bool], list[bool]]:
        if not checked:
            return (
                no_update,
                no_update,
                [True] * len(add_disabled),
                [True] * len(edit_disabled),
                [True] * len(delete_disabled),
            )

        if existing_df_json is not None:
            # already fetched once this session, don't refetch
            return (
                no_update,
                no_update,
                [False] * len(add_disabled),
                [False] * len(edit_disabled),
                [False] * len(delete_disabled),
            )

        store = get_data_store()
        metrics_df = _build_metrics_dataframe(store.fetch_metrics(experiment_id))
        artifacts = list(store.fetch_artifacts(experiment_id=experiment_id))
        artifacts_df = _build_artifacts_dataframe(artifacts)

        if not metrics_df.empty and not artifacts_df.empty:
            df = metrics_df.merge(artifacts_df, on=["run_id", "step"], how="outer")
        else:
            df = metrics_df if not metrics_df.empty else artifacts_df

        column_kinds = _infer_column_kinds(metrics_df.columns, {a.key for a in artifacts})
        _log.info("column_kinds: %s", column_kinds)
        return (
            df.to_json(orient="split"),
            column_kinds,
            [False] * len(add_disabled),
            [False] * len(edit_disabled),
            [False] * len(delete_disabled),
        )

    # --- open the modal, either to add a new chart or edit an existing one ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_TARGET_ID, "data"),
        Output(ADD_CHART_TYPE_SELECT_ID, "value"),
        Output(ADD_CHART_INITIAL_PARAMS_ID, "data"),
        Input({"type": "open-add-chart", "panel": ALL}, "n_clicks"),
        Input({"type": "edit-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        State("current-page", "data"),
        prevent_initial_call=True,
    )
    def open_chart_modal(
        _add_clicks: list[int],
        _edit_clicks: list[int],
        page_json: str,
    ) -> tuple[bool, _ChartTargetData, str | None, dict[str, Any]]:
        triggered_id = cast("_ChartID", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        if not triggered_id or not ctx.triggered[0]["value"]:
            raise PreventUpdate

        if triggered_id["type"] == "open-add-chart":
            return True, {"panel": str(triggered_id["panel"]), "index": None}, None, {}

        curr_page = BasicExperimentPage.model_validate_json(page_json)
        panel = next(p for p in curr_page.panels if p.name == triggered_id["panel"])
        chart = panel.charts[triggered_id["index"]]
        return (
            True,
            {"panel": str(triggered_id["panel"]), "index": int(triggered_id["index"])},
            chart.chart_type,
            chart.parameters,
        )

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

    # --- build the parameter form for the chosen chart type, pre-filled when editing ---
    @app.callback(
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
        columns_by_kind = _group_columns_by_kind(column_kinds or {})
        fields = ChartTypeRegistry.get_registered_chart_types()[chart_type_name]
        _log.info("chart_type=%s columns_by_kind=%s fields=%s", chart_type_name, columns_by_kind, fields)
        initial_params = initial_params or {}
        return [
            _param_field_input(name, field, columns_by_kind, override=initial_params.get(name))
            for name, field in fields.items()
        ]

    # --- live preview ---
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

        merged = [v if v is not None else c for v, c in zip(values, checked_values, strict=True)]
        parameters = {fid["field"]: val for fid, val in zip(field_ids, merged, strict=True)}

        try:
            chart_instance = ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
            df = pd.read_json(StringIO(df_json), orient="split")
            return chart_instance.render(df), ""
        except (ValidationError, KeyError, ValueError) as exc:
            _log.exception("error rendering preview")
            return None, f"Fill in required fields to see a preview ({exc})"

    # --- add or update the chart and persist ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children", allow_duplicate=True),
        Output(ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(ADD_CHART_ERROR_ID, "children", allow_duplicate=True),
        Input(ADD_CHART_SUBMIT_ID, "n_clicks"),
        State(ADD_CHART_TYPE_SELECT_ID, "value"),
        State({"type": "chart-param", "field": ALL}, "value"),
        State({"type": "chart-param", "field": ALL}, "checked"),
        State({"type": "chart-param", "field": ALL}, "id"),
        State(ADD_CHART_TARGET_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State("current-page", "data"),
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
    ) -> tuple[Any, bool, str]:
        if not n_clicks or not chart_type_name or not target:
            raise PreventUpdate

        merged = [v if v is not None else c for v, c in zip(values, checked_values, strict=True)]
        parameters = {fid["field"]: val for fid, val in zip(field_ids, merged, strict=True)}

        try:
            new_chart = ChartInstance[Any, Any](chart_type=chart_type_name, parameters=parameters)
        except ValidationError as exc:
            return no_update, True, f"Invalid parameters: {exc}"

        panel_name = target["panel"]
        index = target.get("index")

        def update_panel(p: PanelInstance[Any, Any]) -> PanelInstance[Any, Any]:
            if p.name != panel_name:
                return p
            if index is None:
                return p.model_copy(update={"charts": [*p.charts, new_chart]})
            new_charts = list(p.charts)
            new_charts[index] = new_chart
            return p.model_copy(update={"charts": new_charts})

        curr_page = BasicExperimentPage.model_validate_json(page_json)
        curr_page = curr_page.model_copy(update={"panels": [update_panel(p) for p in curr_page.panels]})

        store = get_data_store()
        store.update_page(curr_page)
        return accordion_view(store, experiment_id=experiment_id), False, ""

    # --- delete a chart from a panel ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children", allow_duplicate=True),
        Input({"type": "delete-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        State("current-page", "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def delete_chart(
        _n_clicks_list: list[int],
        page_json: str,
        experiment_id: int,
    ) -> dmc.Container:
        triggered_id = cast("_ChartTargetData", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        if not triggered_id or not ctx.triggered[0]["value"]:
            raise PreventUpdate

        panel_name = triggered_id["panel"]
        index = triggered_id["index"]

        def update_panel(p: PanelInstance[Any, Any]) -> PanelInstance[Any, Any]:
            if p.name != panel_name:
                return p
            return p.model_copy(update={"charts": [c for i, c in enumerate(p.charts) if i != index]})

        curr_page = BasicExperimentPage.model_validate_json(page_json)
        curr_page = curr_page.model_copy(update={"panels": [update_panel(p) for p in curr_page.panels]})

        store = get_data_store()
        store.update_page(curr_page)
        return accordion_view(store, experiment_id=experiment_id)

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output("current-page", "data", allow_duplicate=True),
        Input(ACCORDION_ID, "value"),
        State("current-page", "data"),
        prevent_initial_call=True,
    )
    def persist_open_panel(open_value: str | list[str] | None, page_json: str) -> str:
        curr_page = BasicExperimentPage.model_validate_json(page_json)
        new_settings = {**curr_page.page_settings, OPEN_PANEL_KEY: open_value or []}
        curr_page = curr_page.model_copy(update={"page_settings": new_settings})
        store = get_data_store()
        store.update_page(curr_page)
        return curr_page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children", allow_duplicate=True),
        Input(AUTO_REFRESH_INTERVAL_ID, "n_intervals"),
        State(constants.STATE_EXPERIMENT_ID, component_property="data"),
        State(EDIT_MODE_ID, "checked"),
        prevent_initial_call=True,
    )
    def auto_refresh(
        _n_intervals: int,
        experiment_id: int,
        edit_mode: bool,  # noqa: FBT001
    ) -> dmc.Container:
        if edit_mode:
            # Skip while the user is actively editing — a full re-render would wipe
            # the open modal / in-progress form state.
            raise PreventUpdate
        store = get_data_store()
        return accordion_view(store, experiment_id=experiment_id)
