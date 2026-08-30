# pyright: reportPrivateUsage=false
"""Regression tests for the cached-dataframe store surviving a panel/chart re-render.

Adding a panel or chart (or changing run selection) used to silently emit fresh, empty `Store`s on
every re-render, wiping the cached full-dataframe store used for chart previews. These tests pin
the fix: `full_df_json`/`column_kinds` must round-trip through a re-render.
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


# ---- accordion_view: cached dataframe must round-trip, not reset ----


def test_accordion_view_defaults_to_no_cached_dataframe(store: SQLLiteStore, experiment_id: int) -> None:
    container = accordion_view(store, experiment_id)

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store.get("data") is None


def test_accordion_view_preserves_cached_dataframe(store: SQLLiteStore, experiment_id: int) -> None:
    container = accordion_view(
        store,
        experiment_id,
        view_state=EditViewState(
            full_df_json='{"cached": true}',
            column_kinds={"loss": ColumnKind.METRIC},
        ),
    )

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'

    column_kinds_store = _find_props(cast("Any", container).children, COLUMN_KINDS_STORE_ID)
    assert column_kinds_store is not None
    assert column_kinds_store["data"] == {"loss": ColumnKind.METRIC}


def test_persist_settings_and_rerender_does_not_reset_cached_dataframe(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Regression: changing run selection (or any page_settings update) must not drop the cached
    dataframe used for chart previews.
    """
    store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    _page, container = _persist_settings_and_rerender(
        store,
        experiment_id,
        {"open_panel": []},
        view_state=EditViewState(
            full_df_json='{"cached": true}',
            column_kinds={"loss": ColumnKind.METRIC},
        ),
    )

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'


# ---- _compute_full_df_and_column_kinds: the on-demand fallback used by auto-populate/suggest ----


def test_compute_full_df_and_column_kinds_finds_data_without_a_cache_populated(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """
    Regression: auto-generating charts (or suggesting them) right after opening a brand new
    experiment used to wrongly report "no data logged" -- `COLUMN_KINDS_STORE_ID` is only ever
    populated by a prior add/edit-chart or suggest-charts interaction, which a first-time visitor
    may never have done, even though metrics were already logged.
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
    container = accordion_view(store, experiment_id)

    assert _find_props(cast("Any", container).children, AUTO_POPULATE_BUTTON_ID) is not None
    assert _find_props(cast("Any", container).children, SUGGEST_CHARTS_BUTTON_ID) is None


def test_accordion_view_shows_suggest_charts_when_view_is_not_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = accordion_view(store, experiment_id)

    assert _find_props(cast("Any", container).children, SUGGEST_CHARTS_BUTTON_ID) is not None
    assert _find_props(cast("Any", container).children, AUTO_POPULATE_BUTTON_ID) is None


def test_accordion_view_delete_panel_reachable_without_opening_a_panel(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The whole point: delete/rename/reorder must be usable without opening (fetching) the panel."""
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = accordion_view(store, experiment_id)

    delete_button = _find_props(cast("Any", container).children, _delete_panel_button_id("p"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True
