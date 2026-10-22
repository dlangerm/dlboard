"""A chart that reads one value per run is drawn the same from a row per run as from every step."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import pandas as pd
import pendulum
import pytest
from dash._utils import to_json

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.models import ChartInstance, MetricFetch, MetricReduction, PanelInstance
from dlboard.plugins.charts._table_settings import Aggregate
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


def _whole(_chart: ChartInstance[Any, Any]) -> MetricFetch:
    return MetricFetch()


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
    assert chart.metric_fetch() != MetricFetch()

    reduced = panel_data.fetch_panel_dataframe(store, experiment_id, panel)
    monkeypatch.setattr(ChartInstance, "metric_fetch", _whole)
    whole = panel_data.fetch_panel_dataframe(store, experiment_id, panel)

    assert len(reduced) < len(whole)
    assert _drawn(chart, reduced) == _drawn(chart, whole)


def _chart(chart_type: str, **parameters: Any) -> ChartInstance[Any, Any]:  # noqa: ANN401
    return ChartInstance(chart_type=chart_type, parameters=parameters)


_LINE = _chart("line", column="loss", x_axis="step")
_BAR = _chart("bar", column="loss", x_axis="lr")
_LATEST = MetricFetch(MetricReduction.LATEST)
_THINNED = MetricFetch(max_points=500)
_WHOLE = MetricFetch()


@pytest.mark.parametrize(
    ("charts", "expected"),
    [
        pytest.param([_BAR], {_LATEST: {"loss", "lr"}}, id="one-chart"),
        pytest.param(
            [_BAR, _chart("line", column="acc", x_axis="step")],
            {_LATEST: {"loss", "lr"}, _THINNED: {"acc", "step"}},
            id="different-metrics-each-as-asked",
        ),
        pytest.param(
            [_BAR, _LINE],
            {_LATEST: {"lr"}, _THINNED: {"loss", "step"}},
            id="a-line-serves-the-latest-too",
        ),
        pytest.param(
            [_chart("table", aggregate="mean", show_metrics=["loss"]), _LINE],
            {_WHOLE: {"loss", "step"}},
            id="a-mean-needs-every-step",
        ),
        pytest.param(
            [_chart("line", column="loss", x_axis="epoch"), _LINE],
            {_WHOLE: {"loss", "epoch"}, _THINNED: {"step"}},
            id="a-line-against-another-metric-needs-every-step",
        ),
        pytest.param(
            [_chart("line", column="loss", x_axis="step", sample=False)],
            {_WHOLE: {"loss", "step"}},
            id="unsampled",
        ),
        pytest.param(
            [_chart("line", column="loss", x_axis="step", max_points=100), _LINE],
            {MetricFetch(max_points=500): {"loss", "step"}},
            id="the-most-points-any-line-draws",
        ),
        pytest.param([_chart("table")], {_LATEST: None}, id="every-metric-as-asked"),
        pytest.param([_chart("table"), _BAR], {_LATEST: None}, id="every-metric-when-all-ask-the-same"),
        pytest.param([_chart("table"), _LINE], {_THINNED: None}, id="every-metric-thinned-when-they-differ"),
        pytest.param(
            [_chart("table", aggregate="mean"), _LINE, _BAR],
            {_WHOLE: None},
            id="every-metric-whole-for-a-mean",
        ),
        pytest.param([_chart("table", show_metrics=[])], {}, id="no-metrics"),
    ],
)
def test_a_panel_fetches_each_metric_as_little_as_every_chart_of_it_allows(
    charts: list[ChartInstance[Any, Any]], expected: dict[MetricFetch, set[str] | None]
) -> None:
    assert PanelInstance[Any, Any](charts=charts).metric_fetches() == expected
