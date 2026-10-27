"""A chart that reads one value per run is drawn the same from a row per run as from every step."""

from __future__ import annotations

import dataclasses
import json
import math
from typing import TYPE_CHECKING, Any

import pandas as pd
import pendulum
import pytest
from dash._utils import to_json

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.models import ChartInstance, DataRequest, MetricReduction, MetricReductions, PanelInstance
from dlboard.plugins.charts._table_settings import Aggregate, TableRows
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _panel_data as panel_data

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

BarChart.register(allow_override=True)
LineChart.register(allow_override=True)
TableChart.register(allow_override=True)

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _train(store: SQLLiteStore, experiment_id: int) -> None:
    """Three runs: a metric at every step, one at every tenth, a diverged last step, a re-sent step."""
    for lr, last in ((0.1, 0.25), (0.2, math.nan), (0.1, 1.5)):
        run = store.create_run(models.NewRun(experiment_id=experiment_id))
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": lr}))
        steps = [
            models.LoggedMetrics(
                metrics={"loss": 1.0 / (step + 1) + lr} | ({"acc": step / 100} if step % 10 == 9 else {}),
                step=step,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS.add(seconds=step),
            )
            for step in range(30)
        ]
        resent = steps[5].model_copy(update={"metrics": {"loss": 99.0}})
        diverged = steps[-1].model_copy(update={"metrics": {"loss": last}})
        store.log_metrics([*steps[:-1], resent, diverged])
        store.log_metrics([steps[5]])  # the newest write of step 5 is the original one


_ASKED = ChartInstance.data_request


def _whole(chart: ChartInstance[Any, Any]) -> DataRequest:
    """What `chart` asks for, but every metric of it whole."""
    return dataclasses.replace(_ASKED(chart), reductions=MetricReductions())


def _drawn(chart: ChartInstance[Any, Any], frame: pd.DataFrame) -> object:
    """What `chart` draws from `frame`, as data: a mean computed in SQL and in pandas agree to rounding, not to the bit."""
    rendered: Component = chart.render(frame)
    text: str = to_json(rendered)
    return json.loads(text, parse_float=lambda text: round(float(text), 9))


_CHARTS = [
    pytest.param(
        ChartInstance(
            chart_type=TableChart.name,
            parameters={"show_metrics": ["loss", "acc"], "show_hparams": ["lr"], "aggregate": aggregate},
        ),
        id=f"table-{aggregate}",
    )
    for aggregate in Aggregate
] + [
    pytest.param(
        ChartInstance(chart_type=BarChart.name, parameters={"column": "loss", "x_axis": "lr"}),
        id="bar-by-hparam",
    ),
    pytest.param(
        ChartInstance(
            chart_type=BarChart.name, parameters={"column": "loss", "x_axis": "acc", "aggregation": "max"}
        ),
        id="bar-by-metric",
    ),
    pytest.param(
        ChartInstance(chart_type=BarChart.name, parameters={"column": "loss", "x_axis": "run_id"}),
        id="bar-by-run",
    ),
]


@pytest.mark.parametrize("chart", _CHARTS)
def test_a_chart_is_drawn_the_same_from_one_row_per_run_as_from_every_step(
    store: SQLLiteStore, experiment_id: int, chart: ChartInstance[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _train(store, experiment_id)
    panel = PanelInstance[pd.DataFrame, object](name="p", charts=[chart])
    assert chart.data_request().reductions.default is not MetricReduction.HISTORY

    reduced = panel_data.fetch_panel_dataframe(store, experiment_id, panel, run_ids=None)
    monkeypatch.setattr(ChartInstance, "data_request", _whole)
    whole = panel_data.fetch_panel_dataframe(store, experiment_id, panel, run_ids=None)

    assert len(reduced) < len(whole)
    assert _drawn(chart, reduced) == _drawn(chart, whole)


def _chart(chart_type: str, **parameters: Any) -> ChartInstance[Any, Any]:  # noqa: ANN401
    return ChartInstance(chart_type=chart_type, parameters=parameters)


_LINE = _chart("line", column="loss", x_axis="step")
_BAR = _chart("bar", column="loss", x_axis="lr")


@pytest.mark.parametrize(
    ("charts", "expected"),
    [
        pytest.param([_BAR], {MetricReduction.LATEST: {"loss", "lr"}}, id="one-chart"),
        pytest.param(
            [_BAR, _chart("line", column="acc", x_axis="step")],
            {MetricReduction.LATEST: {"loss", "lr"}, MetricReduction.HISTORY: {"acc", "step"}},
            id="different-metrics-each-as-asked",
        ),
        pytest.param(
            [_BAR, _LINE],
            {MetricReduction.LATEST: {"lr"}, MetricReduction.HISTORY: {"loss", "step"}},
            id="a-metric-they-disagree-on-is-fetched-whole",
        ),
        pytest.param([_chart("table")], {MetricReduction.LATEST: None}, id="every-metric-as-asked"),
        pytest.param(
            [_chart("table"), _BAR], {MetricReduction.LATEST: None}, id="every-metric-when-all-ask-the-same"
        ),
        pytest.param(
            [_chart("table"), _LINE],
            {MetricReduction.HISTORY: None},
            id="every-metric-whole-when-they-disagree",
        ),
        pytest.param([_chart("table", show_metrics=[])], {}, id="no-metrics"),
    ],
)
def test_a_panel_fetches_each_metric_as_little_as_every_chart_of_it_allows(
    charts: list[ChartInstance[Any, Any]], expected: dict[MetricReduction, set[str] | None]
) -> None:
    assert panel_data.metrics_by_reduction([chart.data_request() for chart in charts]) == expected


def test_a_panel_whose_charts_need_different_fetches_draws_each_from_its_own(
    store: SQLLiteStore, experiment_id: int
) -> None:
    _train(store, experiment_id)
    bar = _chart("bar", column="loss", x_axis="lr")
    line = _chart("line", column="acc", x_axis="step")
    panel = PanelInstance[pd.DataFrame, object](name="p", charts=[bar, line])

    frame = panel_data.fetch_panel_dataframe(store, experiment_id, panel, run_ids=None)

    runs = frame.groupby("run_id")
    assert set(frame.columns) >= {"loss", "acc", "step"}
    # `acc` is every step the run logged it at; `loss`, which only the bar chart wants, is each run's last.
    assert runs.acc.count().tolist() == [2, 2, 2]  # step 29 diverged: it logged only a loss
    assert runs.loss.count().tolist() == [1, 1, 1]
    assert _drawn(bar, frame) == _drawn(
        bar, panel_data.fetch_panel_dataframe(store, experiment_id, panel, run_ids=None)
    )


@pytest.mark.parametrize("aggregate", list(Aggregate))
def test_a_table_of_runs_reads_each_metric_as_the_reduction_of_its_aggregate(aggregate: Aggregate) -> None:
    """`Aggregate` and `MetricReduction` name the same reductions by the same values, so a table needs no mapping."""
    chart = _chart("table", rows=TableRows.RUN, aggregate=aggregate)

    assert chart.data_request().reductions.default.value == aggregate.value
    assert chart.data_request().reductions.of("loss") is MetricReduction(aggregate.value)
