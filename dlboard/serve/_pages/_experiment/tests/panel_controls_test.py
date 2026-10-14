"""The panel and tab controls that rebuild the whole panel area show a spinner on their button while they run."""

from __future__ import annotations

from typing import Any, cast

from dash import Dash

from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment import _panel_controls as panel_controls
from dlboard.serve._pages._experiment import _tab_controls as tab_controls


def test_buttons_that_rebuild_the_panel_area_spin_until_their_callback_returns() -> None:
    app = Dash(__name__)
    panel_controls.register_panel_controls_callbacks(app)
    tab_controls.register_tab_controls_callbacks(app)

    # Dash ships no types for its callback registry.
    callbacks = cast("list[dict[str, Any]]", app._callback_list)
    spinning = {
        prop: (cb["running"]["running"][prop], cb["running"]["runningOff"][prop])
        for cb in callbacks
        if "running" in cb
        for prop in cb["running"]["running"]
    }

    assert spinning == {
        f"{button}.loading": (True, False)
        for button in (
            state.NEW_PANEL_ID,
            tab_controls.NEW_TAB_SAVE_ID,
            panel_controls.RENAME_PANEL_SAVE_ID,
            tab_controls.RENAME_TAB_SAVE_ID,
            panel_controls.DELETE_PANEL_CONFIRM_ID,
            panel_controls.DELETE_CHART_CONFIRM_ID,
        )
    }
