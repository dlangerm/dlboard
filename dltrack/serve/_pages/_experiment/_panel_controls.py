"""
Panel/chart CRUD callbacks: create/rename/delete/move a panel; delete/move a chart; add a chart.

The add/edit-chart modal's own form-building/preview callbacks live in `_chart_editor_modal.py`;
this module owns the "submit" mutation, plus toggling a panel's sync/layout.

Callbacks needing more than a couple of `State`s take them as one Dash "flexible callback
signature" grouping -- a `dict` of `Input`/`State` objects that Dash flattens for dependency
tracking but delivers back as a single dict argument (typed by a `core.EditCtx` subclass), which is
what keeps them under ruff's `max-args`.

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
from dltrack.serve import get_data_store
from dltrack.serve._pages._experiment import _experiment_page_state as core


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


class _NewTabCtx(core.EditCtx):
    name: str | None
    panel_names: list[str] | None
    experiment_id: int


class _RenameTabCtx(core.EditCtx):
    old_name: str | None
    new_name: str | None
    experiment_id: int


def _register_create_panel(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.NEW_PANEL_ID, "n_clicks"),
        State(core.NEW_PANEL_NAME_ID, "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def create_panel(n_clicks: int, panel_name: str, page_json: str) -> tuple[html.Div, str]:
        if not n_clicks:
            raise PreventUpdate
        if not panel_name:
            msg = "Panel name cannot be empty"
            raise ValueError(msg)

        def add_panel(panels: list[Any]) -> list[Any]:
            if core.panel_name_taken(panels, panel_name):
                msg = f"A panel named {panel_name!r} already exists"
                raise ValueError(msg)
            return [*panels, PanelInstance(name=panel_name)]

        page, container = core.mutate_panels_and_rerender(page_json, add_panel)
        return container, page.model_dump_json()


def _register_add_chart(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.ADD_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.ADD_CHART_ERROR_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.ADD_CHART_SUBMIT_ID, "loading", allow_duplicate=True),
        Input(core.ADD_CHART_SUBMIT_ID, "n_clicks"),
        {
            "chart_type_name": State(core.ADD_CHART_TYPE_SELECT_ID, "value"),
            "values": State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "value"),
            "checked_values": State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "checked"),
            "field_ids": State({"type": core.CHART_PARAM_TYPE, "field": ALL}, "id"),
            "target": State(core.ADD_CHART_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
        },
        prevent_initial_call=True,
    )
    def add_chart(n_clicks: int, add_chart_ctx: _AddChartCtx) -> tuple[Any, bool, str, str | NoUpdate, bool]:
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
            return no_update, True, f"Invalid parameters: {exc}", no_update, False

        panel_name, index = target["panel"], target.get("index")

        def apply_chart(panels: list[Any]) -> list[Any]:
            return [core.upsert_chart(p, panel_name, index, new_chart) for p in panels]

        page, container = core.mutate_panels_and_rerender(
            add_chart_ctx["page_json"],
            apply_chart,
        )
        return container, False, "", page.model_dump_json(), False


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
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
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
            remove_chart,
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
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
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
            remove_panel,
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


def _register_reorder(app: Dash) -> None:
    """
    Panel/chart drag-and-drop drop handlers -- see `_experiment_page_dragdrop.js`.

    Unlike every other mutation in this module, a reorder never changes what any chart shows --
    only where it sits. Routing it through `mutate_panels_and_rerender` (refetch + rebuild every
    open panel from scratch) would pay the full cost of a data-changing edit for a change that has
    no data to refetch, which is exactly the "blanks out for a couple seconds" experience a drag
    shouldn't have. Instead: persist the new order (a cheap page-row update, no metric/artifact
    query) and splice the client's own already-rendered tree into that order in place -- see
    `core.reorder_rendered_panels`/`reorder_rendered_charts`.
    """

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.PANEL_REORDER_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.METRIC_CONTENT_ID, "children"),
        prevent_initial_call=True,
    )
    def reorder_panel(
        request: core.PanelReorderRequest | None, page_json: str, container: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        if not request:
            raise PreventUpdate
        curr_page = core.BasicExperimentPage.model_validate_json(page_json)
        new_panels = core.reorder_panel(
            curr_page.panels, request["panel"], request["target"], after=request["after"]
        )
        updated_page = get_data_store().update_page(curr_page.model_copy(update={"panels": new_panels}))
        new_container = core.reorder_rendered_panels(container, [p.name for p in new_panels])
        return new_container, updated_page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.CHART_REORDER_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.METRIC_CONTENT_ID, "children"),
        prevent_initial_call=True,
    )
    def reorder_chart(
        request: core.ChartReorderRequest | None, page_json: str, container: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        if not request:
            raise PreventUpdate
        curr_page = core.BasicExperimentPage.model_validate_json(page_json)
        panel_name, index, target_index = request["panel"], request["index"], request["target_index"]
        panel = next(p for p in curr_page.panels if p.name == panel_name)
        new_panels = core.reorder_chart(
            curr_page.panels, panel_name, index, target_index, after=request["after"]
        )
        updated_page = get_data_store().update_page(curr_page.model_copy(update={"panels": new_panels}))
        new_order = core.move_index(
            list(range(len(panel.charts))), index, target_index, after=request["after"]
        )
        new_container = core.reorder_rendered_charts(container, panel_name, new_order)
        return new_container, updated_page.model_dump_json()


def _register_toggle(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-sync", "panel": ALL}, "checked"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def toggle_panel_sync(
        _checked_list: list[bool],
        page_json: str,
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
            toggle,
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input({"type": "panel-layout", "panel": ALL}, "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def toggle_panel_layout(
        _value_list: list[str],
        page_json: str,
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
            toggle,
        )
        return container, page.model_dump_json()


def _register_tab_drop(app: Dash) -> None:
    """
    Move a panel (or a single chart) onto an *existing* tab -- see `_experiment_page_dragdrop.js`.

    The only way to create a brand-new tab is `NEW_TAB_BUTTON_ID`; both callbacks below only ever
    move something to a tab that already exists (whatever `.dl-tab-target` the drag landed on). Both
    also switch the active tab to the destination -- otherwise the thing you just dragged would
    vanish from view into a tab you're not looking at.
    """

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.TAB_DROP_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def drop_panel_on_tab(
        request: core.TabDropRequest | None,
        page_json: str,
    ) -> tuple[html.Div, str]:
        if not request:
            raise PreventUpdate
        panel_name, new_tab = request["panel"], request["tab"]

        current_page = core.BasicExperimentPage.model_validate_json(page_json)
        current_tab = next((p.tab for p in current_page.panels if p.name == panel_name), None)
        if new_tab == current_tab:
            raise PreventUpdate

        def move(panels: list[Any]) -> list[Any]:
            return [p.model_copy(update={"tab": new_tab}) if p.name == panel_name else p for p in panels]

        page, container = core.mutate_panels_and_rerender(
            page_json,
            move,
            extra_settings={core.ACTIVE_TAB_KEY: new_tab},
        )
        return container, page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.CHART_TAB_DROP_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def drop_chart_on_tab(
        request: core.ChartTabDropRequest | None,
        page_json: str,
    ) -> tuple[html.Div, str]:
        """Move a single chart to an *existing* tab -- see `drop_panel_on_tab`, its panel-level twin."""
        if not request:
            raise PreventUpdate
        panel_name, index, new_tab = request["panel"], request["index"], request["tab"]

        def move(panels: list[Any]) -> list[Any]:
            return core.move_chart_to_tab(panels, panel_name, index, new_tab)

        page, container = core.mutate_panels_and_rerender(
            page_json,
            move,
            extra_settings={core.ACTIVE_TAB_KEY: new_tab},
        )
        return container, page.model_dump_json()


def _register_chart_panel_move(app: Dash) -> None:
    """
    Move a single chart into a *different* panel by dragging it there.

    See `reorder_chart` for the same-panel case (a cheap client-side splice, since nothing about
    what's fetched/shown changes), which this defers to via `core.move_chart_to_panel` if the drop
    somehow targets its own panel.
    """

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.CHART_PANEL_MOVE_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def drop_chart_on_panel(
        request: core.ChartPanelMoveRequest | None,
        page_json: str,
    ) -> tuple[html.Div, str]:
        if not request:
            raise PreventUpdate
        panel_name, index = request["panel"], request["index"]
        target_panel, target_index, after = request["target_panel"], request["target_index"], request["after"]

        def move(panels: list[Any]) -> list[Any]:
            return core.move_chart_to_panel(
                panels, panel_name, index, target_panel, target_index, after=after
            )

        page, container = core.mutate_panels_and_rerender(
            page_json,
            move,
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
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
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
        if core.panel_name_taken(curr_page.panels, new_name, ignore=old_name):
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
        container = core.accordion_view(store, cast("core.BasicExperimentPage", curr_page))
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


def _new_tab_error(name: str, panels: list[PanelInstance[Any, Any]]) -> str | None:
    """Validate a new-tab submission, or `None` if it's good to create."""
    if not name:
        return "Tab name cannot be empty"
    if name == "General":
        return '"General" is reserved for the default tab'
    if any(p.tab == name for p in panels):
        return f"A tab named {name!r} already exists"
    return None


def _register_new_tab(app: Dash) -> None:
    """
    Create a brand-new tab: name it, optionally pick existing panels to move into it.

    The *only* place a new tab name can be typed -- moving one more panel into an already-existing
    tab is drag-and-drop (`drop_panel_on_tab`, above), which only ever targets tabs that already exist.
    Picking no panels doesn't leave the tab empty -- it gets one fresh, empty panel (named after the
    tab) instead, so there's always somewhere to drag or add a chart into right away.
    """

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.NEW_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.NEW_TAB_NAME_INPUT_ID, "value"),
        Output(core.NEW_TAB_PANELS_SELECT_ID, "data"),
        Output(core.NEW_TAB_PANELS_SELECT_ID, "value"),
        Output(core.NEW_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(core.NEW_TAB_BUTTON_ID, "n_clicks"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def open_new_tab_modal(n_clicks: int, page_json: str) -> tuple[bool, str, list[str], list[str], str]:
        if not n_clicks:
            raise PreventUpdate
        page = core.BasicExperimentPage.model_validate_json(page_json)
        return True, "", [p.name for p in page.panels], [], ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.NEW_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.NEW_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(core.NEW_TAB_SAVE_ID, "n_clicks"),
        {
            "name": State(core.NEW_TAB_NAME_INPUT_ID, "value"),
            "panel_names": State(core.NEW_TAB_PANELS_SELECT_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
        },
        prevent_initial_call=True,
    )
    def create_new_tab(
        n_clicks: int, new_tab_ctx: _NewTabCtx
    ) -> tuple[Any, str | NoUpdate, bool | NoUpdate, str]:
        if not n_clicks:
            raise PreventUpdate
        name = (new_tab_ctx["name"] or "").strip()
        panel_names = new_tab_ctx["panel_names"] or []
        curr_page = core.BasicExperimentPage.model_validate_json(new_tab_ctx["page_json"])
        error = _new_tab_error(name, curr_page.panels)
        if error:
            return no_update, no_update, no_update, error

        selected = set(panel_names)
        new_panels: list[PanelInstance[Any, Any]] = [
            p.model_copy(update={"tab": name}) if p.name in selected else p for p in curr_page.panels
        ]
        if not selected:
            new_panels.append(PanelInstance(name=core.unique_panel_name(curr_page.panels, name), tab=name))
        new_settings = {**curr_page.page_settings, core.ACTIVE_TAB_KEY: name}

        store = get_data_store()
        curr_page = store.update_page(
            curr_page.model_copy(update={"panels": new_panels, "page_settings": new_settings})
        )
        container = core.accordion_view(store, cast("core.BasicExperimentPage", curr_page))
        return container, curr_page.model_dump_json(), False, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.NEW_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.NEW_TAB_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_new_tab(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def _rename_tab_error(old_name: str, new_name: str, panels: list[PanelInstance[Any, Any]]) -> str | None:
    """Validate a tab-rename submission, or `None` if it's good to save."""
    if not new_name:
        return "Tab name cannot be empty"
    if new_name == "General":
        return '"General" is reserved for the default tab'
    if new_name != old_name and any(p.tab == new_name for p in panels):
        return f"A tab named {new_name!r} already exists"
    return None


def _register_rename_tab(app: Dash) -> None:
    """Rename whichever tab is currently active (read off `PANEL_TABS_ID`'s own value)."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.RENAME_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.RENAME_TAB_TARGET_ID, "data"),
        Output(core.RENAME_TAB_NAME_INPUT_ID, "value"),
        Output(core.RENAME_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(core.RENAME_TAB_BUTTON_ID, "n_clicks"),
        State(core.PANEL_TABS_ID, "value"),
        prevent_initial_call=True,
    )
    def open_rename_tab_modal(n_clicks: int, active_tab_value: str | None) -> tuple[bool, str, str, str]:
        active_tab = core.tab_from_component_value(active_tab_value or "")
        if not n_clicks or not active_tab:
            raise PreventUpdate
        return True, active_tab, active_tab, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.RENAME_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(core.RENAME_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(core.RENAME_TAB_SAVE_ID, "n_clicks"),
        {
            "old_name": State(core.RENAME_TAB_TARGET_ID, "data"),
            "new_name": State(core.RENAME_TAB_NAME_INPUT_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
        },
        prevent_initial_call=True,
    )
    def rename_tab(
        n_clicks: int, rename_ctx: _RenameTabCtx
    ) -> tuple[Any, str | NoUpdate, bool | NoUpdate, str]:
        old_name = rename_ctx["old_name"]
        if not n_clicks or not old_name:
            raise PreventUpdate
        new_name = (rename_ctx["new_name"] or "").strip()
        curr_page = core.BasicExperimentPage.model_validate_json(rename_ctx["page_json"])
        error = _rename_tab_error(old_name, new_name, curr_page.panels)
        if error:
            return no_update, no_update, no_update, error

        new_panels = [
            p.model_copy(update={"tab": new_name}) if p.tab == old_name else p for p in curr_page.panels
        ]
        active_tab = curr_page.page_settings.get(core.ACTIVE_TAB_KEY)
        new_settings = dict(curr_page.page_settings)
        if active_tab == old_name:
            new_settings[core.ACTIVE_TAB_KEY] = new_name

        store = get_data_store()
        curr_page = store.update_page(
            curr_page.model_copy(update={"panels": new_panels, "page_settings": new_settings})
        )
        container = core.accordion_view(store, cast("core.BasicExperimentPage", curr_page))
        return container, curr_page.model_dump_json(), False, ""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.RENAME_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Input(core.RENAME_TAB_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_rename_tab(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def register_panel_controls_callbacks(app: Dash) -> None:
    _register_create_panel(app)
    _register_add_chart(app)
    _register_delete_chart(app)
    _register_delete_panel(app)
    _register_reorder(app)
    _register_toggle(app)
    _register_tab_drop(app)
    _register_chart_panel_move(app)
    _register_rename(app)
    _register_new_tab(app)
    _register_rename_tab(app)
