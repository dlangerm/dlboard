"""What the live-update poll redraws: a chart whose data moved past what its on-screen body was drawn from."""

from __future__ import annotations

import typing
from typing import Any, ClassVar

import pandas as pd
import pytest
from dash import no_update
from pydantic import BaseModel

from dlboard.conftest import panel_data_of
from dlboard.conftest import props as _props
from dlboard.models import EVERY_KEY, ChartInstance, ChartType, ColumnKind, DataRequest, PanelInstance
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment._live_updates import _refresh_chart_bodies

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


def _print(chart: ChartInstance[Any, Any], wide: pd.DataFrame) -> str:
    """The fingerprint of `chart` drawn from what its panel reads of a store holding `wide`."""
    return panel_data.chart_data_fingerprint(panel_data_of(wide).for_chart(chart))


def _shown(df: pd.DataFrame) -> dict[tuple[str, int | None], str]:
    """The fingerprints the bodies on screen carry, as the poll reads them back from the page."""
    return {("p", i): _print(chart, df) for i, chart in enumerate(_PANEL.charts)}


def test_a_body_carries_the_fingerprint_of_the_data_it_was_drawn_from() -> None:
    body = chart_view.chart_body(
        _PANEL, 0, panel_data_of(_BEFORE).for_chart(_PANEL.charts[0]), page_size=12, wide=None
    )

    carried = _props(body[1])
    assert carried["id"] == chart_view.CHART_FINGERPRINT.of("p", 0)
    assert carried["children"] == _print(_PANEL.charts[0], _BEFORE)


def _body_ids() -> list[chart_view.ChartID]:
    """The ids the browser reports back for the two charts' bodies -- JSON a callback reads as `ChartID`s."""
    return [chart_view.ChartID.model_validate(chart_view.CHART_BODY.of("p", i)) for i in (0, 1)]


def test_only_a_chart_whose_data_moved_gets_a_new_body_and_the_rest_are_left_alone() -> None:
    chart_ids = _body_ids()

    contents, ok = _refresh_chart_bodies(
        chart_ids, {"p": _PANEL}, {"p": panel_data_of(_AFTER)}, _shown(_BEFORE), page_size=12
    )

    assert ok
    assert contents[1] is no_update  # no new point for "acc"
    new_body: list[Any] = contents[0]
    assert _props(new_body[1])["children"] == _print(_PANEL.charts[0], _AFTER)


def test_a_chart_just_drawn_from_the_current_data_is_not_redrawn_by_the_next_poll() -> None:
    chart_ids = _body_ids()

    contents, _ok = _refresh_chart_bodies(
        chart_ids, {"p": _PANEL}, {"p": panel_data_of(_AFTER)}, _shown(_AFTER), page_size=12
    )

    assert contents == [no_update, no_update]


# ---- what a chart's fingerprint covers ----


class _ThirdPartyParams(BaseModel, frozen=True, extra="forbid"):
    metrics: list[str] | None
    hparams: list[str]


class _ThirdPartyChart(ChartType[_ThirdPartyParams, pd.DataFrame, None], frozen=True, extra="forbid"):
    """A chart type dlboard knows nothing about, declaring its columns only through its `data_request`."""

    name: ClassVar[str] = "third-party"

    @classmethod
    @typing.override
    def parameter_type(cls) -> type[_ThirdPartyParams]:
        return _ThirdPartyParams

    @classmethod
    @typing.override
    def render(cls, parameters: _ThirdPartyParams, dataframe: pd.DataFrame) -> None:
        return None

    @classmethod
    @typing.override
    def data_request(cls, parameters: _ThirdPartyParams) -> DataRequest:
        if parameters.metrics is None:
            return DataRequest(metric_patterns=EVERY_KEY, hparams=frozenset(parameters.hparams))
        return DataRequest(metrics=frozenset(parameters.metrics), hparams=frozenset(parameters.hparams))

    @classmethod
    @typing.override
    def natural_width(cls, parameters: _ThirdPartyParams) -> int:
        return 400

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"metrics": ColumnKind.METRIC, "hparams": ColumnKind.HPARAM}


LineChart.register(allow_override=True)
_ThirdPartyChart.register(allow_override=True)
_CUSTOM = ChartInstance[Any, Any](
    chart_type="third-party", parameters={"metrics": ["loss"], "hparams": ["lr"]}
)
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

    assert _print(_CUSTOM, grown) == _print(_CUSTOM, _WIDE)


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
    assert _print(_CUSTOM, edited) != _print(_CUSTOM, _WIDE)


_ALL_METRICS_ONE_HPARAM = ChartInstance[Any, Any](
    chart_type="third-party", parameters={"metrics": None, "hparams": ["lr"]}
)
_WITH_OTHER_HPARAM = _WIDE.assign(hparam__momentum=[0.9, 0.9, 0.8])


@pytest.mark.parametrize(
    ("column", "value", "redraws"),
    [
        ("acc", 0.1, True),  # every metric is shown, so any of them moving redraws it
        ("hparam__lr", 0.5, True),
        ("hparam__momentum", 0.5, False),  # a hyperparameter it was not asked to show
    ],
    ids=["a-metric", "its-hparam", "another-hparam"],
)
def test_a_chart_showing_every_metric_but_some_hparams_is_scoped_by_each_hint_alone(
    column: str, value: float, *, redraws: bool
) -> None:
    edited = _WITH_OTHER_HPARAM.assign(**{column: value})
    before = _print(_ALL_METRICS_ONE_HPARAM, _WITH_OTHER_HPARAM)

    assert (_print(_ALL_METRICS_ONE_HPARAM, edited) != before) is redraws
