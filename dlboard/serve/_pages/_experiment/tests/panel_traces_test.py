"""A panel's reads and its charts' renders are each a span: what the trace of a slow callback breaks down into."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum

from dlboard import models
from dlboard._telemetry.traces import flush
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _panel_data as panel_data

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from dlboard.serve import SQLStoreBase

LineChart.register(allow_override=True)
TableChart.register(allow_override=True)


def test_a_panel_traces_each_read_by_kind_and_each_chart_it_renders(
    span_exporter: InMemorySpanExporter, store: SQLStoreBase[Any], experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 1 / (step + 1)},
                step=step,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=pendulum.now(pendulum.UTC),
            )
            for step in range(3)
        ]
    )
    # A table that names no metrics reads every one of them: the read that grows with the experiment.
    charts = [
        ChartInstance(chart_type=LineChart.name, parameters={"column": "loss", "x_axis": "step"}),
        ChartInstance(chart_type=TableChart.name),
    ]
    panel = PanelInstance[Any, Any](name="a panel name", charts=charts)

    frame = panel_data.fetch_panel_dataframe(store, experiment_id, panel, None)
    for chart in charts:
        chart.render(frame)
    flush()

    spans = span_exporter.get_finished_spans()
    reads = [dict(s.attributes or {}) for s in spans if s.name == "dlboard.panel.read"]
    assert {(read["dlboard.panel.read"], read.get("dlboard.read.unscoped")) for read in reads} == {
        ("metrics", True),
        ("hyperparams", None),
        ("run_names", None),
    }
    renders = [
        dict(s.attributes or {})["dlboard.chart.type"] for s in spans if s.name == "dlboard.chart.render"
    ]
    assert renders == [LineChart.name, TableChart.name]
