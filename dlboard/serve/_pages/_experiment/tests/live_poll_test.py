"""Tests for what a live-update poll tick fetches."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment import _fetch_open_panel_dataframes

if TYPE_CHECKING:
    import pytest

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)


def test_a_tick_fetches_only_the_charts_on_screen(
    store: SQLLiteStore, experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    charts = [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": f"m{i}", "x_axis": "step"})
        for i in range(20)
    ]
    page = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p", charts=charts)]}))
    page = state.load_page(store, state.PageRef(experiment_id, None))
    on_screen = [state.chart_content_id("p", i) for i in range(6, 9)]
    fetched: list[frozenset[str] | None] = []
    fetch_metrics = store.fetch_metrics

    def spy(experiment_id: int, **kwargs: Any) -> Any:  # noqa: ANN401
        fetched.append(kwargs["keys"])
        return fetch_metrics(experiment_id, **kwargs)

    monkeypatch.setattr(store, "fetch_metrics", spy)

    dataframes, ok = _fetch_open_panel_dataframes(store, experiment_id, page, on_screen)

    assert ok
    assert set(dataframes) == {"p"}
    [keys] = fetched
    assert keys is not None
    assert {"m6", "m7", "m8"} <= keys
    assert not keys & {"m0", "m5", "m9", "m19"}
