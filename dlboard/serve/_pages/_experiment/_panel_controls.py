"""
Panel/chart CRUD: create/rename/delete/move a panel; delete/move a chart; toggle a panel's sync/layout.

Owns its ids, the rename-panel and delete-panel/chart confirm modals (rendered once by the experiment
page, beside the panel area -- see `panel_control_modals`), and the callbacks behind them. Adding or
editing a chart is `_chart_editor_modal.py`'s; tabs are `_tab_controls.py`'s.

Callbacks needing more than a couple of `State`s take them as one Dash "flexible callback
signature" grouping -- a `dict` of `Input`/`State` objects that Dash flattens for dependency
tracking but delivers back as a single dict argument (typed by a `core.EditCtx` subclass), which is
what keeps them under ruff's `max-args`.

Each feature (create, delete-chart, delete-panel, move, toggle, rename) registers via its own small
`_register_*` function -- mirroring `simple_admin_page.py`'s existing
`_register_tab_callbacks`/`_register_restore_callback`/`_register_purge_callbacks` split -- so no
single function accumulates enough nested callbacks to trip ruff's complexity/statement limits.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, ctx, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import ValidationError

from dlboard.models import PanelInstance
from dlboard.serve import ClientsideScript
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import busy_while_running, require_triggered_id
from dlboard.serve._pages._experiment import _experiment_page_state as core

if TYPE_CHECKING:
    from dash.development.base_component import Component

_PANEL_GRID_COLUMNS_JS = ClientsideScript(Path(__file__).with_name("panel_grid_columns.js"))
_PANEL_GRID_FOLLOWERS_JS = ClientsideScript(Path(__file__).with_name("panel_grid_followers.js"))

# --- rename-panel modal ---
RENAME_PANEL_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("rename-panel-modal")
RENAME_PANEL_TARGET_ID: StoreId[core.ExperimentPage] = StoreId("rename-panel-target")
RENAME_PANEL_NAME_INPUT_ID: ValueId[core.ExperimentPage] = ValueId("rename-panel-name-input")
RENAME_PANEL_ERROR_ID: DivId[core.ExperimentPage] = DivId("rename-panel-error")
RENAME_PANEL_SAVE_ID: ButtonId[core.ExperimentPage] = ButtonId("rename-panel-save")
RENAME_PANEL_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("rename-panel-cancel")

# --- delete-panel / delete-chart confirm modals ---
DELETE_PANEL_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("delete-panel-modal")
DELETE_PANEL_TARGET_ID: StoreId[core.ExperimentPage] = StoreId("delete-panel-target")
DELETE_PANEL_CONFIRM_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-panel-confirm")
DELETE_PANEL_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-panel-cancel")

DELETE_CHART_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("delete-chart-modal")
DELETE_CHART_TARGET_ID: StoreId[core.ExperimentPage] = StoreId("delete-chart-target")
DELETE_CHART_CONFIRM_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-chart-confirm")
DELETE_CHART_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-chart-cancel")

# --- drag-and-drop reorder requests: a completed drag reports here (`_experiment_page_dragdrop.js`
# calls `set_props` on drop). Live in the static page layout (`serve/_pages/experiment.py`), not the
# panel area's render tree, so the drop target always exists regardless of what's rendered underneath it. ---
PANEL_REORDER_STORE_ID: StoreId[core.ExperimentPage] = StoreId("panel-reorder-request")
CHART_REORDER_STORE_ID: StoreId[core.ExperimentPage] = StoreId("chart-reorder-request")
CHART_PANEL_MOVE_STORE_ID: StoreId[core.ExperimentPage] = StoreId("chart-panel-move-request")


class PanelReorderRequest(TypedDict):
    """A completed panel drag: move `panel` to just before/after `target`."""

    panel: str
    target: str
    after: bool


class ChartReorderRequest(TypedDict):
    """A completed chart drag: move the chart at `index` in `panel` to just before/after `target_index`."""

    panel: str
    index: int
    target_index: int
    after: bool


class ChartPanelMoveRequest(TypedDict):
    """
    A chart dropped into a *different* panel: move the chart at `index` in `panel` to `target_panel`.

    `target_index` is `None` when the drop landed on the target panel's body generally (an empty
    panel, or anywhere that isn't one of its existing charts) rather than on one of its charts --
    the chart is appended there instead of inserted at a particular position.
    """

    panel: str
    index: int
    target_panel: str
    target_index: int | None
    after: bool


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


def _rename_panel_modal() -> dmc.Modal:
    return dmc.Modal(
        id=RENAME_PANEL_MODAL_ID,
        title="Rename panel",
        opened=False,
        children=[
            Store(id=RENAME_PANEL_TARGET_ID),
            dmc.Stack(
                [
                    dmc.TextInput(id=RENAME_PANEL_NAME_INPUT_ID, label="Panel name"),
                    dmc.Text(id=RENAME_PANEL_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=RENAME_PANEL_CANCEL_ID, variant="default"),
                            dmc.Button("Save", id=RENAME_PANEL_SAVE_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _delete_confirm_modal(  # noqa: PLR0913
    *,
    modal_id: ModalId[core.ExperimentPage],
    target_id: StoreId[core.ExperimentPage],
    confirm_id: ButtonId[core.ExperimentPage],
    cancel_id: ButtonId[core.ExperimentPage],
    title: str,
    body: str,
) -> dmc.Modal:
    return dmc.Modal(
        id=modal_id,
        title=title,
        opened=False,
        children=[
            Store(id=target_id),
            dmc.Text(body),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=cancel_id, variant="default"),
                    dmc.Button("Delete", id=confirm_id, color="red"),
                ],
                justify="flex-end",
                mt="sm",
            ),
        ],
    )


def panel_control_modals() -> list[Component]:
    """The rename-panel and delete confirm modals, for the experiment page to render once, beside the panel area."""
    return [
        _rename_panel_modal(),
        _delete_confirm_modal(
            modal_id=DELETE_PANEL_MODAL_ID,
            target_id=DELETE_PANEL_TARGET_ID,
            confirm_id=DELETE_PANEL_CONFIRM_ID,
            cancel_id=DELETE_PANEL_CANCEL_ID,
            title="Delete this panel?",
            body="This deletes the panel and every chart in it. This can't be undone.",
        ),
        _delete_confirm_modal(
            modal_id=DELETE_CHART_MODAL_ID,
            target_id=DELETE_CHART_TARGET_ID,
            confirm_id=DELETE_CHART_CONFIRM_ID,
            cancel_id=DELETE_CHART_CANCEL_ID,
            title="Delete this chart?",
            body="This can't be undone.",
        ),
    ]


def _validated_layout_change(page_json: str) -> tuple[str, dict[str, Any]]:
    """
    The panel a layout control just changed, and the validated edit that makes to it (`{field: value}`).

    Raises `PreventUpdate` unless the value really differs: a control's `value` mounting for the first
    time reports the panel's current value back as a "change" even though nothing was actually
    touched (and a cleared number input reports nothing usable).
    """
    if not ctx.triggered_id:
        raise PreventUpdate
    triggered_id = cast("dict[str, str]", ctx.triggered_id)
    panel_name = triggered_id["panel"]
    value = cast("Any", ctx.triggered[0]["value"])  # pyrefly: ignore [unsupported-operation]
    match triggered_id["type"]:
        case core.PANEL_LAYOUT_CONTROL.type:
            edit = {"layout": value}
        case core.PANEL_GRID_COLUMNS.type:
            edit = {"grid_columns": value}
        case _:
            raise PreventUpdate

    current_page = core.BasicExperimentPage.model_validate_json(page_json)
    panel = next((p for p in current_page.panels if p.name == panel_name), None)
    if panel is None:
        raise PreventUpdate
    try:
        changed = PanelInstance[Any, Any].model_validate({**panel.model_dump(), **edit})
    except ValidationError:
        raise PreventUpdate from None
    if changed == panel:
        raise PreventUpdate
    return panel_name, changed.model_dump(include=set(edit))


def _register_create_panel(app: Dash) -> None:
    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.NEW_PANEL_NAME_ID, "error"),
        Input(core.NEW_PANEL_ID, "n_clicks"),
        Input(core.NEW_PANEL_NAME_ID, "n_submit"),
        State(core.NEW_PANEL_NAME_ID, "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
        running=busy_while_running(core.NEW_PANEL_ID),
    )
    def create_panel(
        n_clicks: int | None, n_submit: int | None, panel_name: str | None, page_json: str, paging_json: str
    ) -> tuple[Any, str | NoUpdate, str | None]:
        """Add an empty panel to whichever tab is showing, so it appears right where you are."""
        if not n_clicks and not n_submit:
            raise PreventUpdate
        panel_name = (panel_name or "").strip()
        curr_page = core.BasicExperimentPage.model_validate_json(page_json)
        if not panel_name:
            return no_update, no_update, "Give the panel a name"
        if core.panel_name_taken(curr_page.panels, panel_name):
            return no_update, no_update, f"A panel named {panel_name!r} already exists"

        active_tab = curr_page.page_settings.get(core.ACTIVE_TAB_KEY)
        tab = (
            active_tab
            if isinstance(active_tab, str) and any(p.tab == active_tab for p in curr_page.panels)
            else ""
        )
        page, container = core.mutate_panels_and_rerender(
            page_json,
            paging_json,
            lambda panels: [*panels, PanelInstance(name=panel_name, tab=tab)],
            reveal_panel=panel_name,
        )
        return container, page.model_dump_json(), None


def _register_delete_chart(app: Dash) -> None:
    # --- delete chart: click opens a confirm modal, Delete in the modal does the actual mutation ---
    @app.callback(
        Output(DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Output(DELETE_CHART_TARGET_ID, "data"),
        Input(core.DELETE_CHART_BUTTON.all(), "n_clicks"),
        prevent_initial_call=True,
    )
    def open_delete_chart_modal(_n_clicks_list: list[int]) -> tuple[bool, core.ChartTargetData]:
        triggered_id = cast("core.ChartTargetData", require_triggered_id())
        return True, {"panel": triggered_id["panel"], "index": triggered_id["index"]}

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_CHART_CONFIRM_ID, "n_clicks"),
        {
            "target": State(DELETE_CHART_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(core.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
        running=busy_while_running(DELETE_CHART_CONFIRM_ID),
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
            delete_ctx["paging_json"],
            remove_chart,
        )
        return container, page.model_dump_json(), False

    @app.callback(
        Output(DELETE_CHART_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_CHART_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_chart(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def _register_delete_panel(app: Dash) -> None:
    # --- delete panel: same shape as delete chart, above ---
    @app.callback(
        Output(DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(DELETE_PANEL_TARGET_ID, "data"),
        Input(core.DELETE_PANEL_BUTTON.all(), "n_clicks"),
        prevent_initial_call=True,
    )
    def open_delete_panel_modal(_n_clicks_list: list[int]) -> tuple[bool, str]:
        triggered_id = cast("dict[str, str]", require_triggered_id())
        return True, triggered_id["panel"]

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_PANEL_CONFIRM_ID, "n_clicks"),
        {
            "target": State(DELETE_PANEL_TARGET_ID, "data"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(core.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
        running=busy_while_running(DELETE_PANEL_CONFIRM_ID),
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
            delete_ctx["paging_json"],
            remove_panel,
        )
        return container, page.model_dump_json(), False

    @app.callback(
        Output(DELETE_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_PANEL_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_panel(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def _register_reorder(app: Dash) -> None:
    """
    Panel/chart drag-and-drop drop handlers -- see `_experiment_page_dragdrop.js`.

    A reorder rebuilds the panel area: only one page of panels, and of each panel's charts, is ever
    rendered, so the new order can't be spliced into the client's tree the way it once could -- what it
    moves between may not be in it. That rebuild fetches only the pages being shown.
    """

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(PANEL_REORDER_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def reorder_panel(
        request: PanelReorderRequest | None, page_json: str, paging_json: str
    ) -> tuple[html.Div, str]:
        if not request:
            raise PreventUpdate
        page, container = core.mutate_panels_and_rerender(
            page_json,
            paging_json,
            lambda panels: core.reorder_panel(
                panels, request["panel"], request["target"], after=request["after"]
            ),
            reveal_panel=request["panel"],
        )
        return container, page.model_dump_json()

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(CHART_REORDER_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def reorder_chart(
        request: ChartReorderRequest | None, page_json: str, paging_json: str
    ) -> tuple[html.Div, str]:
        if not request:
            raise PreventUpdate
        panel_name, index = request["panel"], request["index"]
        page, container = core.mutate_panels_and_rerender(
            page_json,
            paging_json,
            lambda panels: core.reorder_chart(
                panels, panel_name, index, request["target_index"], after=request["after"]
            ),
            reveal_chart=core.chart_id_at(page_json, panel_name, index),
        )
        return container, page.model_dump_json()


def _register_toggle(app: Dash) -> None:
    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.PANEL_SYNC_SWITCH.all(), "checked"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def toggle_panel_sync(
        _checked_list: list[bool],
        page_json: str,
        paging_json: str,
    ) -> tuple[html.Div, str]:
        # Unlike button clicks, a Switch's `checked` is a meaningful trigger value even when
        # `False`, so this can't reuse `require_triggered_id`'s "falsy value means no real
        # trigger" check.
        if not ctx.triggered_id:
            raise PreventUpdate
        triggered_id = cast("dict[str, str]", ctx.triggered_id)
        panel_name = triggered_id["panel"]
        sync = bool(cast("Any", ctx.triggered[0]["value"]))  # pyrefly: ignore [unsupported-operation]

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
            paging_json,
            toggle,
        )
        return container, page.model_dump_json()

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.PANEL_LAYOUT_CONTROL.all(), "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def change_panel_layout(_layouts: list[str], page_json: str, paging_json: str) -> tuple[html.Div, str]:
        """Apply a panel's Packed/Grid toggle -- every chart's width changes with it, so the page is rebuilt."""
        panel_name, edit = _validated_layout_change(page_json)
        page, container = core.mutate_panels_and_rerender(
            page_json, paging_json, lambda panels: core.update_panel(panels, panel_name, edit)
        )
        return container, page.model_dump_json()

    # A grid's column count is only CSS: the browser applies it the instant the number changes, and the
    # server just saves it. Rebuilding every chart in a big panel for it (as the layout toggle above
    # must) blanked the page for as long as that took, on every click of the counter.
    app.clientside_callback(
        _PANEL_GRID_COLUMNS_JS.source,
        Output(core.PANEL_GRID.match(), "cols"),
        Input(core.PANEL_GRID_COLUMNS.match(), "value"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        _PANEL_GRID_FOLLOWERS_JS.source,
        Input(core.PANEL_GRID_COLUMNS.all(), "value"),
        State(core.PANEL_GRID_COLUMNS.all(), "id"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(core.PANEL_GRID_COLUMNS.all(), "value"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def save_panel_grid_columns(_grid_columns: list[int], page_json: str) -> str:
        """Persist a column count the browser has already applied -- see `panel_grid_columns.js`."""
        panel_name, edit = _validated_layout_change(page_json)
        page = core.mutate_panels(page_json, lambda panels: core.update_panel(panels, panel_name, edit))
        return page.model_dump_json()


def _register_chart_panel_move(app: Dash) -> None:
    """
    Move a single chart into a *different* panel by dragging it there.

    See `reorder_chart` for the same-panel case (a cheap client-side splice, since nothing about
    what's fetched/shown changes), which this defers to via `core.move_chart_to_panel` if the drop
    somehow targets its own panel.
    """

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(CHART_PANEL_MOVE_STORE_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        State(core.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def drop_chart_on_panel(
        request: ChartPanelMoveRequest | None,
        page_json: str,
        paging_json: str,
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
            paging_json,
            move,
            reveal_chart=core.chart_id_at(page_json, panel_name, index),
        )
        return container, page.model_dump_json()


def _panels_with_renamed_panel(
    panels: list[PanelInstance[Any, Any]], *, old_name: str, new_name: str
) -> list[PanelInstance[Any, Any]]:
    return [p.model_copy(update={"name": new_name}) if p.name == old_name else p for p in panels]


def _register_rename(app: Dash) -> None:
    @app.callback(
        Output(RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_PANEL_TARGET_ID, "data"),
        Output(RENAME_PANEL_NAME_INPUT_ID, "value"),
        Output(RENAME_PANEL_ERROR_ID, "children", allow_duplicate=True),
        Input(core.RENAME_PANEL_BUTTON.all(), "n_clicks"),
        prevent_initial_call=True,
    )
    def open_rename_panel_modal(_n_clicks_list: list[int]) -> tuple[bool, str, str, str]:
        triggered_id = cast("dict[str, str]", require_triggered_id())
        panel_name = triggered_id["panel"]
        return True, panel_name, panel_name, ""

    @app.callback(
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_PANEL_ERROR_ID, "children", allow_duplicate=True),
        Input(RENAME_PANEL_SAVE_ID, "n_clicks"),
        {
            "old_name": State(RENAME_PANEL_TARGET_ID, "data"),
            "new_name": State(RENAME_PANEL_NAME_INPUT_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(core.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(core.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
        running=busy_while_running(RENAME_PANEL_SAVE_ID),
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

        # Carry the rename into OPEN_PANEL_KEY too, so a currently-open panel doesn't appear to
        # collapse just because its name changed underneath it.
        open_panel = curr_page.page_settings.get(core.OPEN_PANEL_KEY)
        extra_settings: dict[str, Any] = {}
        if isinstance(open_panel, list):
            # Panel names (and so OPEN_PANEL_KEY) are always strings; page_settings' value type is
            # broader (shared by every settings key), hence the cast.
            open_panel = cast("list[str]", open_panel)
            extra_settings[core.OPEN_PANEL_KEY] = [new_name if v == old_name else v for v in open_panel]
        elif open_panel == old_name:
            extra_settings[core.OPEN_PANEL_KEY] = new_name

        page, container = core.mutate_panels_and_rerender(
            rename_ctx["page_json"],
            rename_ctx["paging_json"],
            lambda panels: _panels_with_renamed_panel(panels, old_name=old_name, new_name=new_name),
            extra_settings=extra_settings,
        )
        return container, page.model_dump_json(), False, ""

    @app.callback(
        Output(RENAME_PANEL_MODAL_ID, "opened", allow_duplicate=True),
        Input(RENAME_PANEL_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_rename_panel(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def register_panel_controls_callbacks(app: Dash) -> None:
    _register_create_panel(app)
    _register_delete_chart(app)
    _register_delete_panel(app)
    _register_reorder(app)
    _register_toggle(app)
    _register_chart_panel_move(app)
    _register_rename(app)
