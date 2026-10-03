"""
Auto-populate charts for a brand-new (empty) view; suggest charts for un-charted keys otherwise.

The empty-view controls and the suggest-charts drawer's static shell live in
`_experiment_page_state.py` (needed by `accordion_view`'s static render tree); this module owns
the dynamic suggestion list content and the callbacks that drive both flows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
from dash import ALL, Dash, Input, NoUpdate, Output, State, ctx, no_update
from dash.exceptions import PreventUpdate

from dltrack.models import ChartInstance, ColumnKind
from dltrack.serve import Icon, get_data_store, icon
from dltrack.serve import _constants as constants
from dltrack.serve._pages._experiment import _experiment_page_state as core
from dltrack.serve._pages._experiment._chart_autogen import (
    Suggestion,
    build_auto_panels,
    build_suggestions,
    find_uncharted_keys,
    group_keys_into_panels,
)
from dltrack.serve._pages._experiment._dataframe_helpers import ColumnCatalog

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.serve._pages._experiment._chart_autogen import SplitMode


_NO_KEYS_MESSAGE = "No metrics or artifacts logged for this experiment yet"


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
                        dmc.Text([icon(Icon.MOVE_TO), " ", s.panel_name], size="xs", c="dimmed"),
                    ],
                    gap=0,
                ),
                dmc.ActionIcon(
                    icon(Icon.ADD),
                    id=core.add_suggestion_button_id(s.kind.value, s.key),
                    n_clicks=0,
                    variant="light",
                    size="sm",
                    **cast("dict[str, Any]", {"aria-label": f"Add a chart for {s.key}"}),
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


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _render_auto_preview(groups: dict[str, list[tuple[str, ColumnKind]]]) -> Component:
    """What "Create charts" would add: each panel, and the keys charted in it."""
    n_charts = sum(len(keys) for keys in groups.values())
    return dmc.Stack(
        [
            dmc.Text(f"{_plural(len(groups), 'panel')} · {_plural(n_charts, 'chart')}", size="sm", fw=600),
            dmc.ScrollArea(
                dmc.Stack(
                    [
                        dmc.Paper(
                            [
                                dmc.Group(
                                    [
                                        dmc.Text(panel_name, size="sm", fw=600),
                                        dmc.Badge(_plural(len(keys), "chart"), variant="light", size="sm"),
                                    ],
                                    justify="space-between",
                                    wrap="nowrap",
                                ),
                                dmc.Text(
                                    ", ".join(key for key, _kind in keys), size="xs", c="dimmed", lineClamp=2
                                ),
                            ],
                            withBorder=True,
                            radius="sm",
                            px="sm",
                            py="xs",
                        )
                        for panel_name, keys in groups.items()
                    ],
                    gap="xs",
                ),
                mah=320,
                type="auto",
            ),
        ],
        gap="xs",
    )


def _register_auto_populate(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.AUTO_POPULATE_MODAL_ID, "opened"),
        Output(core.AUTO_POPULATE_PREVIEW_ID, "children"),
        Output(core.AUTO_POPULATE_BUTTON_ID, "disabled"),
        Input(core.AUTO_POPULATE_OPEN_ID, "n_clicks"),
        Input(core.AUTO_POPULATE_DELIMITER_ID, "value"),
        Input(core.AUTO_POPULATE_MODE_ID, "value"),
        State(core.AUTO_POPULATE_MODAL_ID, "opened"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def preview_auto_populate(
        n_clicks: int | None,
        delimiter: str | None,
        mode: str | None,
        opened: bool,  # noqa: FBT001
        experiment_id: int,
    ) -> tuple[bool | NoUpdate, Component, bool]:
        """Open the confirm modal, and keep its preview in step with the delimiter and grouping picked."""
        opening = cast("str | None", ctx.triggered_id) == core.AUTO_POPULATE_OPEN_ID  # pyright: ignore[reportUnknownMemberType]
        if (opening and not n_clicks) or (not opening and not opened):
            raise PreventUpdate
        store = get_data_store()
        catalog = ColumnCatalog.load(store, experiment_id)
        if not catalog.has_chartable_keys:
            return True, dmc.Text(_NO_KEYS_MESSAGE, c="dimmed", size="sm"), True
        groups = group_keys_into_panels(
            catalog,
            delimiter=delimiter or core.DEFAULT_DELIMITER,
            mode="suffix" if mode == "suffix" else "prefix",
            lightning=core.is_lightning_experiment(store, experiment_id),
        )
        return True if opening else no_update, _render_auto_preview(groups), False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.AUTO_POPULATE_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.AUTO_POPULATE_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_auto_populate(n_clicks: int | None) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.AUTO_POPULATE_BUTTON_ID, "n_clicks"),
        {
            "delimiter": State(core.AUTO_POPULATE_DELIMITER_ID, "value"),
            "mode": State(core.AUTO_POPULATE_MODE_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
        },
        prevent_initial_call=True,
    )
    def auto_populate_charts(n_clicks: int, auto_populate_ctx: _AutoPopulateCtx) -> tuple[Any, str]:
        if not n_clicks:
            raise PreventUpdate
        experiment_id = auto_populate_ctx["experiment_id"]
        store = get_data_store()
        catalog = ColumnCatalog.load(store, experiment_id)
        if not catalog.has_chartable_keys:
            raise ValueError(_NO_KEYS_MESSAGE)
        split_mode: SplitMode = "suffix" if auto_populate_ctx["mode"] == "suffix" else "prefix"
        lightning = core.is_lightning_experiment(store, experiment_id)

        def replace_with_generated_panels(_panels: list[Any]) -> list[Any]:
            return build_auto_panels(
                catalog,
                delimiter=auto_populate_ctx["delimiter"] or core.DEFAULT_DELIMITER,
                mode=split_mode,
                lightning=lightning,
            )

        page, container = core.mutate_panels_and_rerender(
            auto_populate_ctx["page_json"], replace_with_generated_panels
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
    current_scope: str | None


def _register_suggestions(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Output(core.SUGGEST_DRAWER_ID, "title", allow_duplicate=True),
        Output(core.SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Output(core.SUGGEST_SCOPE_STORE_ID, "data", allow_duplicate=True),
        Input(core.SUGGEST_CHARTS_BUTTON_ID, "n_clicks"),
        Input({"type": "panel-suggest-charts", "panel": ALL}, "n_clicks"),
        {
            "delimiter": Input(core.SUGGEST_DELIMITER_ID, "value"),
            "mode": Input(core.SUGGEST_MODE_ID, "value"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
            "current_scope": State(core.SUGGEST_SCOPE_STORE_ID, "data"),
        },
        prevent_initial_call=True,
    )
    def refresh_suggestions(
        n_clicks: int, _panel_clicks: list[int], suggest_ctx: _SuggestCtx
    ) -> tuple[bool | NoUpdate, str | NoUpdate, Component, list[dict[str, Any]], str | None]:
        # Either button mounting for the first time reports itself as "triggered" with `n_clicks`
        # still 0 -- each is rendered into its own container (the panel controls row / a panel's
        # own `panel_header_controls`), separate from the delimiter/mode inputs below, so
        # `ctx.triggered_id` unambiguously resolves to the button on that mount even though
        # nothing was clicked.
        scope, opened = _resolve_suggestion_trigger(n_clicks, suggest_ctx["current_scope"])

        curr_page = core.BasicExperimentPage.model_validate_json(suggest_ctx["page_json"])
        store = get_data_store()
        catalog = (
            ColumnCatalog.load(store, curr_page.experiment_id)
            if curr_page.experiment_id is not None
            else ColumnCatalog()
        )
        title = f"Suggested charts for {scope}" if scope else "Suggested charts"
        if not catalog.has_chartable_keys:
            return opened, title, dmc.Text(_NO_KEYS_MESSAGE, c="dimmed", size="sm"), [], scope

        split_mode: SplitMode = "suffix" if suggest_ctx["mode"] == "suffix" else "prefix"
        lightning = curr_page.experiment_id is not None and core.is_lightning_experiment(
            store, curr_page.experiment_id
        )
        suggestions = build_suggestions(
            find_uncharted_keys(curr_page.panels, catalog),
            delimiter=suggest_ctx["delimiter"] or core.DEFAULT_DELIMITER,
            mode=split_mode,
            lightning=lightning,
            single_value_columns=catalog.single_value_metrics,
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
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Input({"type": "add-suggestion", "kind": ALL, "key": ALL}, "n_clicks"),
        State(core.SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def add_suggested_chart(
        _n_clicks_list: list[int],
        stored_suggestions: list[dict[str, Any]] | None,
        page_json: str,
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

        page, container = core.mutate_panels_and_rerender(page_json, apply_chart)

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
