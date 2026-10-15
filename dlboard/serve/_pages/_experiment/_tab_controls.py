"""
Tabs: create one, rename the active one, and drop a panel or chart onto one that already exists.

Owns its ids, the new-tab and rename-tab modals (rendered once by the experiment page, beside the panel
area -- see `tab_control_modals`), and the callbacks behind them. The tab bar itself, and its "New tab" and
"Rename the active tab" buttons, are part of the panel area's render tree (`_panel_view.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate

from dlboard.models import PanelInstance
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import busy_while_running
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._paging import Paging

if TYPE_CHECKING:
    from dash.development.base_component import Component

# --- new-tab modal -- the *only* place a brand-new tab name can be typed; moving a panel to an
# already-existing tab is dragging it onto that tab in the tab bar instead (`.dl-tab-target`, see
# `_experiment_page_dragdrop.js` and `TAB_DROP_STORE_ID`). ---
NEW_TAB_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("new-tab-modal")
NEW_TAB_NAME_INPUT_ID: ValueId[ids.ExperimentPage] = ValueId("new-tab-name-input")
NEW_TAB_PANELS_SELECT_ID: ValueId[ids.ExperimentPage] = ValueId("new-tab-panels-select")
NEW_TAB_ERROR_ID: DivId[ids.ExperimentPage] = DivId("new-tab-error")
NEW_TAB_SAVE_ID: ButtonId[ids.ExperimentPage] = ButtonId("new-tab-save")
NEW_TAB_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("new-tab-cancel")

# --- rename-tab modal -- renames whichever tab is currently active (read off `PANEL_TABS_ID`'s own
# value), not a per-tab control. ---
RENAME_TAB_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("rename-tab-modal")
RENAME_TAB_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("rename-tab-target")
RENAME_TAB_NAME_INPUT_ID: ValueId[ids.ExperimentPage] = ValueId("rename-tab-name-input")
RENAME_TAB_ERROR_ID: DivId[ids.ExperimentPage] = DivId("rename-tab-error")
RENAME_TAB_SAVE_ID: ButtonId[ids.ExperimentPage] = ButtonId("rename-tab-save")
RENAME_TAB_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("rename-tab-cancel")

# --- drag-and-drop onto a tab: a completed drag reports here (`_experiment_page_dragdrop.js` calls
# `set_props` on drop). Live in the static page layout (`serve/_pages/experiment.py`), not the panel
# area's render tree, so the drop target always exists regardless of what's rendered underneath it. ---
TAB_DROP_STORE_ID: StoreId[ids.ExperimentPage] = StoreId("tab-drop-request")
CHART_TAB_DROP_STORE_ID: StoreId[ids.ExperimentPage] = StoreId("chart-tab-drop-request")


class TabDropRequest(TypedDict):
    """A panel dropped onto an existing tab: move `panel` to `tab` (`""` for the ungrouped tab)."""

    panel: str
    tab: str


class ChartTabDropRequest(TypedDict):
    """A chart dropped onto an existing tab: move the chart at `index` in `panel` to `tab`."""

    panel: str
    index: int
    tab: str


class _NewTabCtx(edits.EditCtx):
    name: str | None
    panel_names: list[str] | None
    experiment_id: int


class _RenameTabCtx(edits.EditCtx):
    old_name: str | None
    new_name: str | None
    experiment_id: int


def _new_tab_modal() -> dmc.Modal:
    """
    The *only* place a brand-new tab gets created (see `NEW_TAB_BUTTON_ID`).

    Name it and, optionally, pick existing panels to move into it -- leave the picker empty and the
    tab starts with one fresh, empty panel instead. Moving one more panel in later, or moving a
    single chart into an existing tab, is drag-and-drop (`.dl-tab-target`), not this modal, which
    only ever creates a brand-new tab.
    """
    return dmc.Modal(
        id=NEW_TAB_MODAL_ID,
        title="New tab",
        opened=False,
        children=[
            dmc.Stack(
                [
                    dmc.TextInput(id=NEW_TAB_NAME_INPUT_ID, label="Tab name"),
                    dmc.MultiSelect(
                        id=NEW_TAB_PANELS_SELECT_ID,
                        label="Panels",
                        description="Which panels should move into this tab (optional -- leave "
                        "empty to start with a new, empty panel)",
                        data=[],
                        searchable=True,
                    ),
                    dmc.Text(id=NEW_TAB_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=NEW_TAB_CANCEL_ID, variant="default"),
                            dmc.Button("Create", id=NEW_TAB_SAVE_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def _rename_tab_modal() -> dmc.Modal:
    """Renames whichever tab is active when `RENAME_TAB_BUTTON_ID` is clicked -- disabled on "General"."""
    return dmc.Modal(
        id=RENAME_TAB_MODAL_ID,
        title="Rename tab",
        opened=False,
        children=[
            Store(id=RENAME_TAB_TARGET_ID),
            dmc.Stack(
                [
                    dmc.TextInput(id=RENAME_TAB_NAME_INPUT_ID, label="Tab name"),
                    dmc.Text(id=RENAME_TAB_ERROR_ID, c="red", size="sm"),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=RENAME_TAB_CANCEL_ID, variant="default"),
                            dmc.Button("Save", id=RENAME_TAB_SAVE_ID),
                        ],
                        justify="flex-end",
                    ),
                ],
            ),
        ],
    )


def tab_control_modals() -> list[Component]:
    """The new-tab and rename-tab modals, for the experiment page to render once, beside the panel area."""
    return [_new_tab_modal(), _rename_tab_modal()]


def _register_tab_drop(app: Dash) -> None:
    """
    Move a panel (or a single chart) onto an *existing* tab -- see `_experiment_page_dragdrop.js`.

    The only way to create a brand-new tab is `NEW_TAB_BUTTON_ID`; both callbacks below only ever
    move something to a tab that already exists (whatever `.dl-tab-target` the drag landed on). Both
    also switch the active tab to the destination -- otherwise the thing you just dragged would
    vanish from view into a tab you're not looking at.
    """

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(TAB_DROP_STORE_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def drop_panel_on_tab(
        request: TabDropRequest | None,
        page_json: str,
        paging_json: str,
    ) -> tuple[html.Div, str]:
        if not request:
            raise PreventUpdate
        panel_name, new_tab = request["panel"], request["tab"]

        current_page = page_model.BasicExperimentPage.model_validate_json(page_json)
        current_tab = next((p.tab for p in current_page.panels if p.name == panel_name), None)
        if new_tab == current_tab:
            raise PreventUpdate

        def move(panels: list[Any]) -> list[Any]:
            return [p.model_copy(update={"tab": new_tab}) if p.name == panel_name else p for p in panels]

        page, container = edits.commit(
            page_json,
            paging_json,
            edits.PageEdit(
                panels=move,
                tab=new_tab,
                reveal=edits.RevealPanel(panel_name),
            ),
        )
        return container, page.model_dump_json()

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(CHART_TAB_DROP_STORE_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def drop_chart_on_tab(
        request: ChartTabDropRequest | None,
        page_json: str,
        paging_json: str,
    ) -> tuple[html.Div, str]:
        """Move a single chart to an *existing* tab -- see `drop_panel_on_tab`, its panel-level twin."""
        if not request:
            raise PreventUpdate
        panel_name, index, new_tab = request["panel"], request["index"], request["tab"]

        def move(panels: list[Any]) -> list[Any]:
            return page_model.move_chart_to_tab(panels, panel_name, index, new_tab)

        page, container = edits.commit(
            page_json,
            paging_json,
            edits.PageEdit(
                panels=move,
                tab=new_tab,
                reveal=edits.RevealChartAt(panel_name, index),
            ),
        )
        return container, page.model_dump_json()


def _panels_with_new_tab(
    panels: list[PanelInstance[Any, Any]], *, name: str, panel_names: set[str]
) -> list[PanelInstance[Any, Any]]:
    """
    `panels`, moving each named in `panel_names` onto tab `name`.

    If none were picked, adds a fresh empty panel on that tab instead, so there's always somewhere
    to drop a chart right away.
    """
    new_panels = [p.model_copy(update={"tab": name}) if p.name in panel_names else p for p in panels]
    if not panel_names:
        new_panels.append(PanelInstance(name=page_model.unique_panel_name(panels, name), tab=name))
    return new_panels


def _panels_with_renamed_tab(
    panels: list[PanelInstance[Any, Any]], *, old_name: str, new_name: str
) -> list[PanelInstance[Any, Any]]:
    return [p.model_copy(update={"tab": new_name}) if p.tab == old_name else p for p in panels]


def _new_tab_error(name: str, panels: list[PanelInstance[Any, Any]]) -> str | None:
    """Validate a new-tab submission, or `None` if it's good to create."""
    if not name:
        return "Tab name cannot be empty"
    if name == page_model.UNGROUPED_TAB_LABEL:
        return f'"{page_model.UNGROUPED_TAB_LABEL}" is reserved for the default tab'
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

    @app.callback(
        Output(NEW_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(NEW_TAB_NAME_INPUT_ID, "value"),
        Output(NEW_TAB_PANELS_SELECT_ID, "data"),
        Output(NEW_TAB_PANELS_SELECT_ID, "value"),
        Output(NEW_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(panel_view.NEW_TAB_BUTTON_ID, "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def open_new_tab_modal(n_clicks: int, page_json: str) -> tuple[bool, str, list[str], list[str], str]:
        if not n_clicks:
            raise PreventUpdate
        page = page_model.BasicExperimentPage.model_validate_json(page_json)
        return True, "", [p.name for p in page.panels], [], ""

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(NEW_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(NEW_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(NEW_TAB_SAVE_ID, "n_clicks"),
        {
            "name": State(NEW_TAB_NAME_INPUT_ID, "value"),
            "panel_names": State(NEW_TAB_PANELS_SELECT_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(ids.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
        running=busy_while_running(NEW_TAB_SAVE_ID),
    )
    def create_new_tab(
        n_clicks: int, new_tab_ctx: _NewTabCtx
    ) -> tuple[Any, str | NoUpdate, bool | NoUpdate, str]:
        if not n_clicks:
            raise PreventUpdate
        name = (new_tab_ctx["name"] or "").strip()
        panel_names = new_tab_ctx["panel_names"] or []
        curr_page = page_model.BasicExperimentPage.model_validate_json(new_tab_ctx["page_json"])
        error = _new_tab_error(name, curr_page.panels)
        if error:
            return no_update, no_update, no_update, error

        selected = set(panel_names)
        page, container = edits.commit(
            new_tab_ctx["page_json"],
            new_tab_ctx["paging_json"],
            edits.PageEdit(
                panels=lambda panels: _panels_with_new_tab(panels, name=name, panel_names=selected),
                tab=name,
            ),
        )
        return container, page.model_dump_json(), False, ""

    @app.callback(
        Output(NEW_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Input(NEW_TAB_CANCEL_ID, "n_clicks"),
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
    if new_name == page_model.UNGROUPED_TAB_LABEL:
        return f'"{page_model.UNGROUPED_TAB_LABEL}" is reserved for the default tab'
    if new_name != old_name and any(p.tab == new_name for p in panels):
        return f"A tab named {new_name!r} already exists"
    return None


def _register_rename_tab(app: Dash) -> None:
    """Rename whichever tab is currently active (read off `PANEL_TABS_ID`'s own value)."""

    @app.callback(
        Output(RENAME_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_TAB_TARGET_ID, "data"),
        Output(RENAME_TAB_NAME_INPUT_ID, "value"),
        Output(RENAME_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(panel_view.RENAME_TAB_BUTTON_ID, "n_clicks"),
        State(panel_view.PANEL_TABS_ID, "value"),
        prevent_initial_call=True,
    )
    def open_rename_tab_modal(n_clicks: int, active_tab_value: str | None) -> tuple[bool, str, str, str]:
        active_tab = panel_view.tab_from_component_value(active_tab_value or "")
        if not n_clicks or not active_tab:
            raise PreventUpdate
        return True, active_tab, active_tab, ""

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(RENAME_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Output(RENAME_TAB_ERROR_ID, "children", allow_duplicate=True),
        Input(RENAME_TAB_SAVE_ID, "n_clicks"),
        {
            "old_name": State(RENAME_TAB_TARGET_ID, "data"),
            "new_name": State(RENAME_TAB_NAME_INPUT_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(ids.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
        running=busy_while_running(RENAME_TAB_SAVE_ID),
    )
    def rename_tab(
        n_clicks: int, rename_ctx: _RenameTabCtx
    ) -> tuple[Any, str | NoUpdate, bool | NoUpdate, str]:
        old_name = rename_ctx["old_name"]
        if not n_clicks or not old_name:
            raise PreventUpdate
        new_name = (rename_ctx["new_name"] or "").strip()
        curr_page = page_model.BasicExperimentPage.model_validate_json(rename_ctx["page_json"])
        error = _rename_tab_error(old_name, new_name, curr_page.panels)
        if error:
            return no_update, no_update, no_update, error

        # Renaming the tab being shown keeps it the one shown.
        follows = (
            Paging.model_validate_json(rename_ctx["paging_json"]).shown_tab(curr_page.panels) == old_name
        )
        page, container = edits.commit(
            rename_ctx["page_json"],
            rename_ctx["paging_json"],
            edits.PageEdit(
                panels=lambda panels: _panels_with_renamed_tab(panels, old_name=old_name, new_name=new_name),
                tab=new_name if follows else None,
            ),
        )
        return container, page.model_dump_json(), False, ""

    @app.callback(
        Output(RENAME_TAB_MODAL_ID, "opened", allow_duplicate=True),
        Input(RENAME_TAB_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_rename_tab(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def register_tab_controls_callbacks(app: Dash) -> None:
    _register_tab_drop(app)
    _register_new_tab(app)
    _register_rename_tab(app)
