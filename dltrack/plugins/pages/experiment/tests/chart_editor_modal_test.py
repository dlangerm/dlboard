# pyright: reportPrivateUsage=false
"""
Regression tests: the add/edit-chart modal must offer real column choices (and a live preview) the
*first* time it's opened, not just after a prior suggest-charts/auto-populate interaction has
happened to populate `COLUMN_KINDS_STORE_ID`/`FULL_DF_STORE_ID` this page load.

`open_chart_modal` (`_chart_editor_modal.py`) never populates those caches itself, so
`build_chart_param_form`/`render_chart_preview` must fetch fresh data whenever they're empty --
mirroring `auto_populate_charts`/`refresh_suggestions`'s existing fallback (see
`edit_mode_test.py::test_compute_full_df_and_column_kinds_finds_data_without_a_cache_populated`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dltrack import models
from dltrack.conftest import props as _to_props
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.charts.table_chart import TableChart
from dltrack.plugins.pages.experiment._chart_editor_modal import build_chart_param_form, render_chart_preview

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)

LineChart.register(allow_override=True)
TableChart.register(allow_override=True)


def _log_metrics_and_hparams(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5, "acc": 0.9},
                step=0,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
        ]
    )
    store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))


def _props_by_field(fields: list[Any]) -> dict[str, dict[str, Any]]:
    return {_to_props(f)["id"]["field"]: _to_props(f) for f in fields}


def test_build_chart_param_form_offers_real_columns_on_first_open(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """
    The very first "Add chart" click: `column_kinds` (from `COLUMN_KINDS_STORE_ID`) is still
    `None` -- nothing has populated it yet -- even though metrics were already logged.
    """
    _log_metrics_and_hparams(store, experiment_id)

    fields, full_df_json, column_kinds = build_chart_param_form(
        "line", None, None, store=store, experiment_id=experiment_id
    )
    by_field = _props_by_field(fields)

    assert set(by_field["column"]["data"]) >= {"loss", "acc"}
    assert by_field["x_axis"]["data"]
    # The resolved cache is handed back too, so the caller can write it into the cache stores and
    # spare the *next* add/edit-chart click this page load the same full-experiment fetch.
    assert full_df_json
    assert column_kinds is not None
    assert column_kinds["loss"] == "metric"


def test_build_chart_param_form_table_chart_offers_metrics_and_hparams(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Same regression, for the table chart's metrics/hparams/pivot_on/pivot_metric fields."""
    _log_metrics_and_hparams(store, experiment_id)

    fields, _full_df_json, _column_kinds = build_chart_param_form(
        "table", None, None, store=store, experiment_id=experiment_id
    )
    by_field = _props_by_field(fields)

    assert set(by_field["metrics"]["data"]) >= {"loss", "acc"}
    assert "lr" in by_field["hparams"]["data"]
    assert by_field["pivot_on"]["data"]
    assert by_field["pivot_metric"]["data"]


def test_build_chart_param_form_respects_an_already_populated_cache(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """When `column_kinds` *is* already populated (a later add-chart click this page load), use it
    directly rather than re-fetching -- proves the fallback is additive, not a behavior change.
    """
    fields, full_df_json, column_kinds = build_chart_param_form(
        "line", {"custom_metric": "metric"}, None, store=store, experiment_id=experiment_id
    )
    by_field = _props_by_field(fields)

    assert by_field["column"]["data"] == ["custom_metric"]
    # No fetch happened, so there's nothing new to hand back -- the caller must not clobber an
    # already-populated cache with `None`.
    assert full_df_json is None
    assert column_kinds == {"custom_metric": "metric"}


def test_render_chart_preview_renders_on_first_open(store: SQLLiteStore, experiment_id: int) -> None:
    """The live preview must also work the first time, not just report "fill in required fields"."""
    _log_metrics_and_hparams(store, experiment_id)

    field_ids = [{"type": "chart-param", "field": "column"}, {"type": "chart-param", "field": "x_axis"}]
    preview, error, df_json = render_chart_preview(
        "line", ["loss", "step"], [None, None], field_ids, None, store=store, experiment_id=experiment_id
    )

    assert error == ""
    assert preview is not None
    assert df_json
