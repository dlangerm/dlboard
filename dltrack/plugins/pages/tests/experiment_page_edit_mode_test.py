# pyright: reportPrivateUsage=false
"""Regression tests for edit-mode state surviving a panel/chart re-render.

Adding a panel or chart (or changing run selection) while in edit mode used to
silently drop back out of edit mode and wipe the cached full-dataframe store,
because `accordion_view` hardcoded `checked=False` and emitted fresh, empty
`Store`s on every re-render. These tests pin the fix: `edit_mode`,
`full_df_json`, and `column_kinds` must round-trip through a re-render.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from dltrack import models
from dltrack.conftest import find_props as _find_props
from dltrack.models._view import ColumnKind, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.pages.simple_experiment_page import (
    AUTO_POPULATE_BUTTON_ID,
    COLUMN_KINDS_STORE_ID,
    EDIT_DRAWER_ID,
    FULL_DF_STORE_ID,
    SUGGEST_CHARTS_BUTTON_ID,
    BasicExperimentPage,
    EditViewState,
    _compute_full_df_and_column_kinds,
    _delete_panel_button_id,
    _persist_settings_and_rerender,
    accordion_view,
)

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)

LineChart.register(allow_override=True)


# ---- accordion_view: edit_mode / cached dataframe must round-trip, not reset ----


def test_accordion_view_defaults_to_edit_drawer_closed(store: SQLLiteStore, experiment_id: int) -> None:
    container = accordion_view(store, experiment_id)

    drawer = _find_props(cast("Any", container).children, EDIT_DRAWER_ID)
    assert drawer is not None
    assert drawer["opened"] is False

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store.get("data") is None


def test_accordion_view_preserves_edit_drawer_state_and_cached_dataframe(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = accordion_view(
        store,
        experiment_id,
        view_state=EditViewState(
            edit_mode=True,
            edit_drawer_opened=True,
            full_df_json='{"cached": true}',
            column_kinds={"loss": ColumnKind.METRIC},
        ),
    )

    drawer = _find_props(cast("Any", container).children, EDIT_DRAWER_ID)
    assert drawer is not None
    assert drawer["opened"] is True

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'

    column_kinds_store = _find_props(cast("Any", container).children, COLUMN_KINDS_STORE_ID)
    assert column_kinds_store is not None
    assert column_kinds_store["data"] == {"loss": ColumnKind.METRIC}


def test_persist_settings_and_rerender_does_not_reset_edit_drawer_state(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Regression: changing run selection (or any page_settings update) while editing must not
    close the edit drawer or drop the cached dataframe used for chart previews.
    """
    store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    _page, container = _persist_settings_and_rerender(
        store,
        experiment_id,
        {"open_panel": []},
        view_state=EditViewState(
            edit_mode=True,
            edit_drawer_opened=True,
            full_df_json='{"cached": true}',
            column_kinds={"loss": ColumnKind.METRIC},
        ),
    )

    drawer = _find_props(cast("Any", container).children, EDIT_DRAWER_ID)
    assert drawer is not None
    assert drawer["opened"] is True

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'


# ---- _compute_full_df_and_column_kinds: the edit-mode-cache fallback ----


def test_compute_full_df_and_column_kinds_finds_data_without_edit_mode_ever_toggled(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """
    Regression: auto-generating charts (or suggesting them) right after opening a brand new
    experiment used to wrongly report "no data logged" -- `COLUMN_KINDS_STORE_ID` is normally only
    populated by toggling edit mode on, which a first-time visitor may never have done, even though
    metrics were already logged.
    """
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5}, step=0, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            )
        ]
    )

    full_df_json, column_kinds = _compute_full_df_and_column_kinds(store, experiment_id)

    assert column_kinds["loss"] == ColumnKind.METRIC.value
    assert full_df_json


def test_compute_full_df_and_column_kinds_empty_for_an_experiment_with_no_data(
    store: SQLLiteStore, experiment_id: int
) -> None:
    _full_df_json, column_kinds = _compute_full_df_and_column_kinds(store, experiment_id)

    assert column_kinds == {}


# ---- toolbar: auto-generate charts (empty view) vs suggest charts (non-empty view) ----


def test_accordion_view_shows_auto_populate_when_view_is_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = accordion_view(store, experiment_id, view_state=EditViewState(edit_mode=True))

    assert _find_props(cast("Any", container).children, AUTO_POPULATE_BUTTON_ID) is not None
    assert _find_props(cast("Any", container).children, SUGGEST_CHARTS_BUTTON_ID) is None


def test_accordion_view_shows_suggest_charts_when_view_is_not_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = accordion_view(store, experiment_id, view_state=EditViewState(edit_mode=True))

    assert _find_props(cast("Any", container).children, SUGGEST_CHARTS_BUTTON_ID) is not None
    assert _find_props(cast("Any", container).children, AUTO_POPULATE_BUTTON_ID) is None


def test_accordion_view_manage_panels_list_reachable_without_opening_a_panel(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The whole point: delete/rename/reorder must be usable without opening (fetching) the panel."""
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = accordion_view(store, experiment_id, view_state=EditViewState(edit_mode=True))

    delete_button = _find_props(cast("Any", container).children, _delete_panel_button_id("p"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True
