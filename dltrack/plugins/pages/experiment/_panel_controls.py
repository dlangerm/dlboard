"""
Panel/chart CRUD callbacks: create/rename/delete/move a panel; delete/move a chart; add a chart.

The add/edit-chart modal's own form-building/preview callbacks live in `_chart_editor_modal.py`;
this module owns the "submit" mutation, plus toggling a panel's sync/layout.

Several of these callbacks repeat the same `(page_json, experiment_id, full_df_json, column_kinds)`
tail. Rather than list each as its own Python parameter (which is what earned the original
`plug()` its `# noqa: PLR0913`s), they're passed as one Dash "flexible callback signature" grouping
-- a `dict` of `Input`/`State` objects that Dash flattens for dependency-tracking purposes but
delivers back to the callback as a single dict argument. `_experiment_page_state.edit_ctx_state()`
supplies the `(page_json, view_state)` portion shared by every callback below; each callback merges
in whatever else it needs (a `target`, `old_name`/`new_name`, ...) into the same grouped `State`.

Each feature (create, add-chart, delete-chart, delete-panel, move, toggle, rename) registers via
its own small `_register_*` function -- mirroring `simple_admin_page.py`'s existing
`_register_tab_callbacks`/`_register_restore_callback`/`_register_purge_callbacks` split -- so no
single function accumulates enough nested callbacks to trip ruff's complexity/statement limits.
"""

from __future__ import annotations

import typing
from typing import Any, cast

from dash import ALL, Dash, Input, NoUpdate, Output, State, ctx, html, no_update
from dash.exceptions import PreventUpdate
from pydantic import ValidationError

from dltrack.models import ChartTypeRegistry, PanelInstance, constants
from dltrack.plugins.pages.experiment import _experiment_page_state as core
from dltrack.serve import get_data_store


class _AddChartCtx(core.EditCtx):
    chart_type_name: str | None
    values: list[Any]
    checked_values: list[Any]
    field_ids: list[dict[str, str]]
    target: core.ChartTargetData | None
    experiment_id: int


class _DeleteChartCtx(core.EditCtx):
    target: core.ChartTargetData | None
    experiment_id: int


class _DeletePanelCtx(core.EditCtx):
    target: str | None
    experiment_id: int


class _RenamePanelCtx(core.EditCtx):
    old_name: str | None
    new_name: str | None
    experiment_id: int


def _register_create_panel(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.NEW_PANEL_ID, "n_clicks"),
        Input(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.NEW_PANEL_NAME_ID, "value"),
        core.edit_ctx_state(),
        prevent_initial_call=True,
    )
    def create_panel(
        n_clicks: int, experiment_id: int, panel_name: str, edit_ctx: core.EditCtx
    ) -> tuple[html.Div, str]:
        if not n_clicks:
            raise PreventUpdate
        if not panel_name:
            msg = "Panel name cannot be empty"
            raise ValueError(msg)

        def add_panel(panels: list[Any]) -> list[Any]:
            return [*panels, PanelInstance(name=panel_name)]

        page, container = core.mutate_panels_and_rerender(
            edit_ctx["page_json"],
            experiment_id,
            add_panel,
            view_state=core.edit_view_state_from_ctx(edit_ctx),
        )
        return container, page.model_dump_json()


def _register_add_chart(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.ADD_CHART_ERROR_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.ADD_CHART_SUBMIT_ID, "n_clicks"),
        {
            "chart_type_name": State(core.ADD_CHART_TYPE_SELECT_ID, "value"),
            "values": State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "value"),
            "checked_values": State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "checked"),
            "field_ids": State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "id"),
            "target": State(core.ADD_CHART_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            **core.edit_ctx_state(),
        },
        prevent_initial_call=True,
    )
    def add_chart(n_clicks: int, add_chart_ctx: _AddChartCtx) -> tuple[Any, bool, str, str | NoUpdate]:
        chart_type_name, target = add_chart_ctx["chart_type_name"], add_chart_ctx["target"]
        if not n_clicks or not chart_type_name or not target:
            raise PreventUpdate

        fields = ChartTypeRegistry.get_registered_chart_types().get(chart_type_name, {})
        parameters = core.merge_chart_param_values(
            add_chart_ctx["values"], add_chart_ctx["checked_values"], add_chart_ctx["field_ids"], fields
        )
        try:
            new_chart = core.build_validated_chart_instance(chart_type_name, parameters)
        except (ValidationError, KeyError) as exc:
            return no_update, True, f"Invalid parameters: {exc}", no_update

        panel_name, index = target["panel"], target.get("index")

        def apply_chart(panels: list[Any]) -> list[Any]:
            return [core.upsert_chart(p, panel_name, index, new_chart) for p in panels]

        page, container = core.mutate_panels_and_rerender(
            add_chart_ctx["page_json"],
            add_chart_ctx["experiment_id"],
            apply_chart,
            view_state=core.edit_view_state_from_ctx(add_chart_ctx),
        )
        return container, False, "", page.model_dump_json()


def _register_delete_chart(app: Dash) -> None:
    # --- delete chart: click opens a confirm modal, Delete in the modal does the actual mutation ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.DELETE_CHART_TARGET_ID, "data"),
        Input({"type": "delete-chart", "panel": ALL, "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_delete_chart_modal(_n_clicks_list: list[int]) -> tuple[bool, core.ChartTargetData]:
        triggered_id = cast("core.ChartTargetData", core.require_triggered_id())
        return True, {"panel": triggered_id["panel"], "index": triggered_id["index"]}

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.DELETE_CHART_CONFIRM_ID, "n_clicks"),
        {
            "target": State(core.DELETE_CHART_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            **core.edit_ctx_state(),
        },
        prevent_initial_call=True,
    )
    def delete_chart(
        n_clicks: int, delete_ctx: _DeleteChartCtx
    ) -> tuple[html.Div | NoUpdate, str | NoUpdate, bool]:
        target = delete_ctx["target"]
        if not n_clicks or not target:
            raise PreventUpdate
        panel_name, index = target["panel"], target["index"]

        def remove_chart(panels: list[Any]) -> list[Any]:
            return [
                p
                if p.name != panel_name
                else p.model_copy(update={"charts": [c for i, c in enumerate(p.charts) if i != index]})
                for p in panels
            ]

        page, container = core.mutate_panels_and_rerender(
            delete_ctx["page_json"],
            delete_ctx["experiment_id"],
            remove_chart,
            view_state=core.edit_view_state_from_ctx(delete_ctx),
        )
        return container, page.model_dump_json(), False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.DELETE_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_chart(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def _register_delete_panel(app: Dash) -> None:
    # --- delete panel: same shape as delete chart, above ---
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.DELETE_PANEL_TARGET_ID, "data"),
        Input({"type": "delete-panel", "panel": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_delete_panel_modal(_n_clicks_list: list[int]) -> tuple[bool, str]:
        triggered_id = cast("dict[str, str]", core.require_triggered_id())
        return True, triggered_id["panel"]

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.DELETE_PANEL_CONFIRM_ID, "n_clicks"),
        {
            "target": State(core.DELETE_PANEL_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            **core.edit_ctx_state(),
        },
        prevent_initial_call=True,
    )
    def delete_panel(
        n_clicks: int, delete_ctx: _DeletePanelCtx
    ) -> tuple[html.Div | NoUpdate, str | NoUpdate, bool]:
        panel_name = delete_ctx["target"]
        if not n_clicks or not panel_name:
            raise PreventUpdate

        def remove_panel(panels: list[Any]) -> list[Any]:
            return [p for p in panels if p.name != panel_name]

        page, container = core.mutate_panels_and_rerender(
            delete_ctx["page_json"],
            delete_ctx["experiment_id"],
            remove_panel,
            view_state=core.edit_view_state_from_ctx(delete_ctx),
        )
        return container, page.model_dump_json(), False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.DELETE_PANEL_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_panel(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def _register_move(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "move-panel", "panel": ALL, "direction": ALL}, "n_clicks"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.FULL_DF_STORE_ID, "data", allow_optional=True),
        State(core.COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def move_panel(
        _n_clicks_list: list[int],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        triggered_id = cast("dict[str, str]", core.require_triggered_id())
        panel_name = triggered_id["panel"]
        direction = cast("core.PanelMoveDirection", triggered_id["direction"])

        def reorder(panels: list[Any]) -> list[Any]:
            return core.move_panel(panels, panel_name, direction)

        page, container = core.mutate_panels_and_rerender(
            page_json,
            experiment_id,
            reorder,
            view_state=core.EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "move-chart", "panel": ALL, "index": ALL, "direction": ALL}, "n_clicks"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.FULL_DF_STORE_ID, "data", allow_optional=True),
        State(core.COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def move_chart(
        _n_clicks_list: list[int],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        triggered_id = cast("dict[str, Any]", core.require_triggered_id())
        panel_name, index, direction = (
            triggered_id["panel"],
            triggered_id["index"],
            cast("core.ChartMoveDirection", triggered_id["direction"]),
        )

        def reorder(panels: list[Any]) -> list[Any]:
            return core.move_chart(panels, panel_name, index, direction)

        page, container = core.mutate_panels_and_rerender(
            page_json,
            experiment_id,
            reorder,
            view_state=core.EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )
        return container, page.model_dump_json()


def _register_toggle(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-sync", "panel": ALL}, "checked"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.FULL_DF_STORE_ID, "data", allow_optional=True),
        State(core.COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def toggle_panel_sync(
        _checked_list: list[bool],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        # Unlike button clicks, a Switch's `checked` is a meaningful trigger value even when
        # `False`, so this can't reuse `require_triggered_id`'s "falsy value means no real
        # trigger" check.
        if not ctx.triggered_id:  # pyright: ignore[reportUnknownMemberType]
            raise PreventUpdate
        triggered_id = cast("dict[str, str]", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        panel_name = triggered_id["panel"]
        sync = bool(cast("Any", ctx.triggered[0]["value"]))

        # Like `sync_run_selection` (`_run_comparison_table.py`): this Switch's `checked` is *set
        # from* the panel's current `sync` value on every render, so a panel-sync switch mounting
        # for the first time (e.g. this page's very first render) reports that same value back as
        # a "change" even though nothing was actually toggled -- `prevent_initial_call` doesn't
        # catch it because this component isn't in the static layout. Skip the rebuild when
        # nothing really changed.
        current_page = core.BasicExperimentPage.model_validate_json(page_json)
        current_sync = next((p.sync for p in current_page.panels if p.name == panel_name), None)
        if sync == current_sync:
            raise PreventUpdate

        def toggle(panels: list[Any]) -> list[Any]:
            return core.set_panel_sync(panels, panel_name, sync=sync)

        page, container = core.mutate_panels_and_rerender(
            page_json,
            experiment_id,
            toggle,
            view_state=core.EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-layout", "panel": ALL}, "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.FULL_DF_STORE_ID, "data", allow_optional=True),
        State(core.COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def toggle_panel_layout(
        _value_list: list[str],
        page_json: str,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[html.Div, str]:
        if not ctx.triggered_id:  # pyright: ignore[reportUnknownMemberType]
            raise PreventUpdate
        triggered_id = cast("dict[str, str]", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        panel_name = triggered_id["panel"]
        layout = cast("typing.Literal['packed', 'grid']", ctx.triggered[0]["value"])

        # Mirrors `toggle_panel_sync`: a SegmentedControl's `value` mounting for the first time
        # reports the panel's current layout back as a "change" even though nothing was actually
        # toggled -- skip the rebuild when nothing really changed.
        current_page = core.BasicExperimentPage.model_validate_json(page_json)
        current_layout = next((p.layout for p in current_page.panels if p.name == panel_name), None)
        if layout == current_layout:
            raise PreventUpdate

        def toggle(panels: list[Any]) -> list[Any]:
            return core.set_panel_layout(panels, panel_name, layout)

        page, container = core.mutate_panels_and_rerender(
            page_json,
            experiment_id,
            toggle,
            view_state=core.EditViewState(full_df_json=full_df_json, column_kinds=column_kinds),
        )
        return container, page.model_dump_json()


def _register_rename(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.RENAME_PANEL_TARGET_ID, "data"),
        Output(core.RENAME_PANEL_NAME_INPUT_ID, "value"),
        Output(core.RENAME_PANEL_ERROR_ID, "children", allow_duplicate=True),
        Input({"type": "rename-panel", "panel": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_rename_panel_modal(_n_clicks_list: list[int]) -> tuple[bool, str, str, str]:
        triggered_id = cast("dict[str, str]", core.require_triggered_id())
        panel_name = triggered_id["panel"]
        return True, panel_name, panel_name, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.RENAME_PANEL_ERROR_ID, "children", allow_duplicate=True),
        Input(core.RENAME_PANEL_SAVE_ID, "n_clicks"),
        {
            "old_name": State(core.RENAME_PANEL_TARGET_ID, "data"),
            "new_name": State(core.RENAME_PANEL_NAME_INPUT_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            **core.edit_ctx_state(),
        },
        prevent_initial_call=True,
    )
    def rename_panel(
        n_clicks: int, rename_ctx: _RenamePanelCtx
    ) -> tuple[Any, str | NoUpdate, bool | NoUpdate, str]:
        old_name = rename_ctx["old_name"]
        if not n_clicks or not old_name:
            raise PreventUpdate
        new_name = (rename_ctx["new_name"] or "").strip()
        if not new_name:
            return no_update, no_update, no_update, "Panel name cannot be empty"

        curr_page = core.BasicExperimentPage.model_validate_json(rename_ctx["page_json"])
        if new_name != old_name and any(p.name == new_name for p in curr_page.panels):
            return no_update, no_update, no_update, f"A panel named {new_name!r} already exists"

        new_panels = [
            p.model_copy(update={"name": new_name}) if p.name == old_name else p for p in curr_page.panels
        ]
        # Carry the rename into OPEN_PANEL_KEY too, so a currently-open panel doesn't appear to
        # collapse just because its name changed underneath it.
        open_panel = curr_page.page_settings.get(core.OPEN_PANEL_KEY)
        new_settings = dict(curr_page.page_settings)
        if isinstance(open_panel, list):
            # Panel names (and so OPEN_PANEL_KEY) are always strings; page_settings' value type is
            # broader (shared by every settings key), hence the cast.
            open_panel = cast("list[str]", open_panel)
            new_settings[core.OPEN_PANEL_KEY] = [new_name if v == old_name else v for v in open_panel]
        elif open_panel == old_name:
            new_settings[core.OPEN_PANEL_KEY] = new_name

        store = get_data_store()
        curr_page = store.update_page(
            curr_page.model_copy(update={"panels": new_panels, "page_settings": new_settings})
        )
        container = core.accordion_view(
            store,
            experiment_id=rename_ctx["experiment_id"],
            view_state=core.edit_view_state_from_ctx(rename_ctx),
        )
        return container, curr_page.model_dump_json(), False, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.RENAME_PANEL_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_rename_panel(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def register_panel_controls_callbacks(app: Dash) -> None:
    _register_create_panel(app)
    _register_add_chart(app)
    _register_delete_chart(app)
    _register_delete_panel(app)
    _register_move(app)
    _register_toggle(app)
    _register_rename(app)
