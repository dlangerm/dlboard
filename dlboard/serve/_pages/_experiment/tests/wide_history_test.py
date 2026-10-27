"""A chart over many metrics' every step says how many, and past a limit is not drawn or read at all (#417)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.conftest import props
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts._table_settings import TableRows
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _panel_data as panel_data

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

TableChart.register(allow_override=True)


@pytest.mark.parametrize(
    ("metrics", "said", "read"),
    [
        pytest.param(2, None, True, id="few-enough-to-say-nothing"),
        pytest.param(3, "reads 3 metrics' history", True, id="many-says-how-many"),
        pytest.param(5, "Too many metrics to draw whole", False, id="too-many-is-not-read"),
    ],
)
def test_a_chart_of_many_metrics_whole_says_so_and_too_many_are_not_read(  # noqa: PLR0913
    store: SQLLiteStore,
    experiment_id: int,
    monkeypatch: pytest.MonkeyPatch,
    metrics: int,
    said: str | None,
    *,
    read: bool,
) -> None:
    monkeypatch.setenv("DLBOARD_WIDE_HISTORY_METRICS", "2")
    monkeypatch.setenv("DLBOARD_MAX_HISTORY_METRICS", "4")
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={f"layer_{i}/grad_norm": float(i) for i in range(metrics)} | {"loss": 1.0},
                step=0,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=pendulum.now(pendulum.UTC),
            )
        ]
    )
    # A table of a row per step draws each metric whole: the built-in chart that can read a family's every step.
    table = ChartInstance[Any, Any](
        chart_type=TableChart.name,
        parameters={
            "rows": TableRows.FIELD,
            "row_field": "step",
            "show_metrics": ["layer_*/grad_norm"],
            "show_hparams": [],
        },
    )
    panel = PanelInstance[Any, Any](name="p", charts=[table])

    data = panel_data.fetch_panel_data(store, experiment_id, panel, run_ids=None)
    [item] = chart_view.render_panel_charts(panel, data)

    assert bool(data.metrics) is read
    shown = str(props(item)["children"])
    notices = ("reads 3 metrics' history", "Too many metrics to draw whole")
    assert [notice for notice in notices if notice in shown] == [said] * (said is not None)
