"""
Auto-populate charts for a brand-new (empty) view; suggest charts for un-charted keys otherwise.

The empty-view controls and the suggest-charts drawer's static shell live in
`_experiment_page_state.py` (needed by `accordion_view`'s static render tree); this module owns
the dynamic suggestion list content and the callbacks that drive both flows.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, Dash, Input, NoUpdate, Output, State, ctx, no_update
from dash.exceptions import PreventUpdate

from dltrack.models import ChartInstance, ColumnKind, constants
from dltrack.plugins.pages.experiment import _dataframe_helpers as dfh
from dltrack.plugins.pages.experiment import _experiment_page_state as core
from dltrack.plugins.pages.experiment._chart_autogen import (
    Suggestion,
    build_auto_panels,
    build_suggestions,
    chartable_metric_columns,
    find_uncharted_keys,
)
from dltrack.serve import get_data_store

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.plugins.pages.experiment._chart_autogen import SplitMode


def _single_value_columns(full_df_json: str | None, column_kinds: dict[str, str]) -> frozenset[str]:
    """Metric columns worth a bar chart instead of a line chart -- see `default_chart_for_metric`."""
    if not full_df_json:
        return frozenset()
    df = pd.read_json(StringIO(full_df_json), orient="split")
    return frozenset(dfh.single_value_metric_columns(df, chartable_metric_columns(column_kinds)))


class _AutoPopulateCtx(core.EditCtx):
    delimiter: str | None
    mode: str | None
    experiment_id: int


def _render_suggestions(suggestions: list[Suggestion]) -> Component:
    if not suggestions:
        return dmc.Text("Every metric and artifact already has a chart.", c="dimmed", size="sm")

    def _row(s: Suggestion) -> Component:
        return dmc.Group(
            [
                dmc.Stack(
                    [
                        dmc.Text(s.key, size="sm", ff="monospace"),
                        dmc.Text(f"→ {s.panel_name}", size="xs", c="dimmed"),
                    ],
                    gap=0,
                ),
                dmc.ActionIcon(
                    "+",
                    id=core.add_suggestion_button_id(s.kind.value, s.key),
                    n_clicks=0,
                    variant="light",
                    size="sm",
                ),
            ],
            justify="space-between",
            wrap="nowrap",
        )

    metrics = [s for s in suggestions if s.kind == ColumnKind.METRIC]
    artifacts = [s for s in suggestions if s.kind == ColumnKind.ARTIFACT]
    sections: list[Component] = []
    if metrics:
        sections.append(dmc.Text("Metrics", fw=600, size="sm", mt="sm"))
        sections.extend(_row(s) for s in metrics)
    if artifacts:
        sections.append(dmc.Text("Artifacts", fw=600, size="sm", mt="sm"))
        sections.extend(_row(s) for s in artifacts)
    return dmc.Stack(sections, gap="xs")


def _register_auto_populate(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.AUTO_POPULATE_BUTTON_ID, "n_clicks"),
        {
            "delimiter": State(core.AUTO_POPULATE_DELIMITER_ID, "value"),
            "mode": State(core.AUTO_POPULATE_MODE_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            **core.edit_ctx_state(),
        },
        prevent_initial_call=True,
    )
    def auto_populate_charts(n_clicks: int, auto_populate_ctx: _AutoPopulateCtx) -> tuple[Any, str]:
        if not n_clicks:
            raise PreventUpdate
        experiment_id = auto_populate_ctx["experiment_id"]
        full_df_json = auto_populate_ctx["view_state"]["full_df_json"]
        column_kinds = auto_populate_ctx["view_state"]["column_kinds"]
        if not column_kinds:
            # Edit mode (the only thing that normally populates these caches) may never have been
            # toggled on this page load -- fetch fresh rather than wrongly reporting no data.
            full_df_json, column_kinds = core.compute_full_df_and_column_kinds(
                get_data_store(), experiment_id
            )
        if not column_kinds:
            msg = "No metrics or artifacts logged for this experiment yet"
            raise ValueError(msg)
        split_mode: SplitMode = "suffix" if auto_populate_ctx["mode"] == "suffix" else "prefix"
        lightning = core.is_lightning_experiment(get_data_store(), experiment_id)
        single_value_columns = _single_value_columns(full_df_json, column_kinds)

        def replace_with_generated_panels(_panels: list[Any]) -> list[Any]:
            return build_auto_panels(
                column_kinds,
                delimiter=auto_populate_ctx["delimiter"] or core.DEFAULT_DELIMITER,
                mode=split_mode,
                lightning=lightning,
                single_value_columns=single_value_columns,
            )

        page, container = core.mutate_panels_and_rerender(
            auto_populate_ctx["page_json"],
            experiment_id,
            replace_with_generated_panels,
            view_state=core.EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )
        return container, page.model_dump_json()


def _resolve_suggestion_trigger(
    n_clicks: int, current_scope: str | None
) -> tuple[str | None, bool | NoUpdate]:
    """
    The suggestion scope (`None` = whole experiment) and whether to open the drawer.

    Raises `PreventUpdate` for a freshly-mounted button's own spurious "triggered" report (see
    `refresh_suggestions`); otherwise falls back to `current_scope`/leaving `opened` alone for any
    other trigger (the drawer's own delimiter/mode inputs, which only need suggestions recomputed).
    """
    triggered_id = cast(
        "str | dict[str, str] | None",
        ctx.triggered_id,  # pyright: ignore[reportUnknownMemberType]
    )
    if not triggered_id:
        raise PreventUpdate
    if triggered_id == core.SUGGEST_CHARTS_BUTTON_ID:
        if not n_clicks:
            raise PreventUpdate
        return None, True
    if isinstance(triggered_id, dict) and triggered_id.get("type") == "panel-suggest-charts":
        if not ctx.triggered[0]["value"]:
            raise PreventUpdate
        return triggered_id["panel"], True
    return current_scope, no_update


class _SuggestCtx(TypedDict):
    delimiter: str | None
    mode: str | None
    page_json: str
    column_kinds: dict[str, str] | None
    full_df_json: str | None
    current_scope: str | None


def _register_suggestions(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Output(core.SUGGEST_DRAWER_ID, "title", allow_duplicate=True),
        Output(core.SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Output(core.SUGGEST_SCOPE_STORE_ID, "data", allow_duplicate=True),
        Output(core.FULL_DF_STORE_ID, "data", allow_duplicate=True),
        Output(core.COLUMN_KINDS_STORE_ID, "data", allow_duplicate=True),
        Input(core.SUGGEST_CHARTS_BUTTON_ID, "n_clicks"),
        Input({"type": "panel-suggest-charts", "panel": ALL}, "n_clicks"),
        {
            "delimiter": Input(core.SUGGEST_DELIMITER_ID, "value"),
            "mode": Input(core.SUGGEST_MODE_ID, "value"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
            "column_kinds": State(core.COLUMN_KINDS_STORE_ID, "data"),
            "full_df_json": State(core.FULL_DF_STORE_ID, "data"),
            "current_scope": State(core.SUGGEST_SCOPE_STORE_ID, "data"),
        },
        prevent_initial_call=True,
    )
    def refresh_suggestions(
        n_clicks: int, _panel_clicks: list[int], suggest_ctx: _SuggestCtx
    ) -> tuple[
        bool | NoUpdate,
        str | NoUpdate,
        Component,
        list[dict[str, Any]],
        str | None,
        str | None,
        dict[str, str] | None,
    ]:
        # Either button mounting for the first time reports itself as "triggered" with `n_clicks`
        # still 0 -- each is rendered into its own container (the panel controls row / a panel's
        # own `panel_header_controls`), separate from the delimiter/mode inputs below, so
        # `ctx.triggered_id` unambiguously resolves to the button on that mount even though
        # nothing was clicked.
        scope, opened = _resolve_suggestion_trigger(n_clicks, suggest_ctx["current_scope"])

        page_json, column_kinds = suggest_ctx["page_json"], suggest_ctx["column_kinds"]
        full_df_json = suggest_ctx["full_df_json"]
        curr_page = core.BasicExperimentPage.model_validate_json(page_json)
        if not column_kinds and curr_page.experiment_id is not None:
            # Edit mode (the only thing that normally populates this cache) may never have been
            # toggled on this page load -- fetch fresh rather than wrongly reporting no data, and
            # write the result back to the cache stores so the *next* add-chart/suggest-charts
            # click this page load doesn't pay for the same full-experiment fetch again.
            full_df_json, column_kinds = core.compute_full_df_and_column_kinds(
                get_data_store(), curr_page.experiment_id
            )
        title = f"Suggested charts for {scope}" if scope else "Suggested charts"
        if not column_kinds:
            empty = dmc.Text("No metrics or artifacts logged for this experiment yet", c="dimmed", size="sm")
            return opened, title, empty, [], scope, full_df_json, column_kinds

        split_mode: SplitMode = "suffix" if suggest_ctx["mode"] == "suffix" else "prefix"
        uncharted = find_uncharted_keys(curr_page.panels, column_kinds)
        lightning = curr_page.experiment_id is not None and core.is_lightning_experiment(
            get_data_store(), curr_page.experiment_id
        )
        suggestions = build_suggestions(
            uncharted,
            delimiter=suggest_ctx["delimiter"] or core.DEFAULT_DELIMITER,
            mode=split_mode,
            lightning=lightning,
            single_value_columns=_single_value_columns(full_df_json, column_kinds),
        )
        if scope is not None:
            suggestions = [s for s in suggestions if s.panel_name == scope]

        return (
            opened,
            title,
            _render_suggestions(suggestions),
            [
                {
                    "key": s.key,
                    "kind": s.kind.value,
                    "panel_name": s.panel_name,
                    "chart_type": s.chart.chart_type,
                    "parameters": s.chart.parameters,
                }
                for s in suggestions
            ],
            scope,
            full_df_json,
            column_kinds,
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Input({"type": "add-suggestion", "kind": ALL, "key": ALL}, "n_clicks"),
        State(core.SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.FULL_DF_STORE_ID, "data", allow_optional=True),
        State(core.COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def add_suggested_chart(
        _n_clicks_list: list[int],
        stored_suggestions: list[dict[str, Any]] | None,
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[Any, str, Component, list[dict[str, Any]]]:
        triggered_id = cast("dict[str, str]", core.require_triggered_id())
        kind, key = triggered_id["kind"], triggered_id["key"]

        stored_suggestions = stored_suggestions or []
        match = next((s for s in stored_suggestions if s["kind"] == kind and s["key"] == key), None)
        if match is None:
            raise PreventUpdate

        chart = ChartInstance[Any, Any](chart_type=match["chart_type"], parameters=match["parameters"])
        panel_name = match["panel_name"]

        def apply_chart(panels: list[Any]) -> list[Any]:
            return core.add_chart_to_panel_by_name(panels, panel_name, chart)

        page, container = core.mutate_panels_and_rerender(
            page_json,
            experiment_id,
            apply_chart,
            view_state=core.EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )

        remaining = [s for s in stored_suggestions if not (s["kind"] == kind and s["key"] == key)]
        remaining_suggestions = [
            Suggestion(
                key=s["key"],
                kind=ColumnKind(s["kind"]),
                panel_name=s["panel_name"],
                chart=ChartInstance[Any, Any](chart_type=s["chart_type"], parameters=s["parameters"]),
            )
            for s in remaining
        ]
        return (
            container,
            page.model_dump_json(),
            _render_suggestions(remaining_suggestions),
            remaining,
        )


def register_chart_suggestions_callbacks(app: Dash) -> None:
    _register_auto_populate(app)
    _register_suggestions(app)
