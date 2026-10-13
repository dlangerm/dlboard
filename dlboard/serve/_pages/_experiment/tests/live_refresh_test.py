"""What the live-update poll redraws: a chart whose data moved past what its on-screen body was drawn from."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from dash import no_update

from dlboard.conftest import props as _props
from dlboard.models import ChartInstance, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment import _refresh_chart_bodies

_PANEL = PanelInstance[Any, Any](
    name="p",
    charts=[
        ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"}),
        ChartInstance[Any, Any](chart_type="line", parameters={"column": "acc", "x_axis": "step"}),
    ],
)
_BEFORE = pd.DataFrame({"run_id": [1], "step": [0], "loss": [1.0], "acc": [0.8]})
_AFTER = pd.concat(
    [_BEFORE, pd.DataFrame({"run_id": [1], "step": [1], "loss": [0.5], "acc": [None]})], ignore_index=True
)


def _shown(df: pd.DataFrame) -> dict[tuple[str, int | None], str]:
    """The fingerprints the bodies on screen carry, as the poll reads them back from the page."""
    return {("p", i): state.chart_data_fingerprint(chart, df) for i, chart in enumerate(_PANEL.charts)}


def test_a_body_carries_the_fingerprint_of_the_data_it_was_drawn_from() -> None:
    body = state.chart_body(_PANEL, 0, _BEFORE, page_size=12)

    carried = _props(body[1])
    assert carried["id"] == state.chart_fingerprint_id("p", 0)
    assert carried["children"] == state.chart_data_fingerprint(_PANEL.charts[0], _BEFORE)


def test_only_a_chart_whose_data_moved_gets_a_new_body_and_the_rest_are_left_alone() -> None:
    chart_ids = [state.chart_body_id("p", 0), state.chart_body_id("p", 1)]

    contents, ok = _refresh_chart_bodies(
        chart_ids, {"p": _PANEL}, {"p": _AFTER}, _shown(_BEFORE), page_size=12
    )

    assert ok
    assert contents[1] is no_update  # no new point for "acc"
    new_body: list[Any] = contents[0]
    assert _props(new_body[1])["children"] == state.chart_data_fingerprint(_PANEL.charts[0], _AFTER)


def test_a_chart_just_drawn_from_the_current_data_is_not_redrawn_by_the_next_poll() -> None:
    chart_ids = [state.chart_body_id("p", 0), state.chart_body_id("p", 1)]

    contents, _ok = _refresh_chart_bodies(
        chart_ids, {"p": _PANEL}, {"p": _AFTER}, _shown(_AFTER), page_size=12
    )

    assert contents == [no_update, no_update]


# ---- what a chart's fingerprint covers ----

LineChart.register(allow_override=True)
TableChart.register(allow_override=True)
_TABLE = ChartInstance[Any, Any](chart_type="table", parameters={"metrics": ["loss"], "hparams": ["lr"]})
_WIDE = pd.DataFrame(
    {
        "run_id": [1, 1, 2],
        "step": [0, 1, 0],
        "loss": [1.0, 0.5, 0.9],
        "acc": [0.8, 0.9, 0.7],
        "hparam__lr": [0.1, 0.1, 0.2],
        "run_name": ["one", "one", "two"],
    }
)


def test_a_new_row_for_another_metric_does_not_redraw_a_chart_that_shows_none_of_it() -> None:
    """Rows of another metric, in an earlier run, renumber the index of every row after them."""
    extra = pd.DataFrame({"run_id": [1], "step": [2], "acc": [0.95]})
    grown = pd.concat([_WIDE.iloc[:2], extra, _WIDE.iloc[2:]], ignore_index=True)

    assert state.chart_data_fingerprint(_TABLE, grown) == state.chart_data_fingerprint(_TABLE, _WIDE)


@pytest.mark.parametrize(
    ("column", "value"),
    [("hparam__lr", 0.5), ("run_name", "renamed")],
    ids=["hparam-edited", "run-renamed"],
)
def test_editing_a_hyperparameter_or_renaming_a_run_redraws_the_charts_that_show_it(
    column: str, value: str | float
) -> None:
    edited = _WIDE.copy()
    edited[column] = value
    assert state.chart_data_fingerprint(_TABLE, edited) != state.chart_data_fingerprint(_TABLE, _WIDE)
