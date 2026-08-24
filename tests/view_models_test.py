# pyright: reportPrivateUsage=false
"""Tests for the chart/panel view model registry and aggregation logic."""

from __future__ import annotations

import typing
from typing import ClassVar

import pandas as pd
import pytest
from pydantic import BaseModel

from dltrack.models._view import ChartInstance, ChartType, ChartTypeRegistry, ColumnKind, PanelInstance


class _FakeParams(BaseModel, frozen=True, extra="forbid"):
    metric: str
    flag: bool = False
    hparam: str = ""


class _FakeChart(ChartType[_FakeParams, pd.DataFrame, dict[str, object]], frozen=True, extra="forbid"):
    name: ClassVar[str] = "fake"

    @classmethod
    @typing.override
    def parameter_type(cls) -> type[_FakeParams]:
        return _FakeParams

    @classmethod
    @typing.override
    def render(cls, parameters: _FakeParams, dataframe: pd.DataFrame) -> dict[str, object]:
        return {"metric": parameters.metric, "rows": len(dataframe)}

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: _FakeParams) -> set[str] | None:
        return None if parameters.metric == "all" else {parameters.metric}

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: _FakeParams) -> set[str] | None:
        return {"img"} if parameters.flag else set()

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: _FakeParams) -> set[str] | None:
        return None if parameters.hparam == "all" else ({parameters.hparam} if parameters.hparam else set())

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"metric": ColumnKind.METRIC, "hparam": ColumnKind.HPARAM}


@pytest.fixture(autouse=True)
def _clean_registry() -> typing.Iterator[None]:
    original = dict(ChartTypeRegistry._all_charts)
    yield
    ChartTypeRegistry._all_charts = original


def test_register_duplicate_name_raises() -> None:
    _FakeChart.register()
    with pytest.raises(AttributeError):
        _FakeChart.register()
    _FakeChart.register(allow_override=True)  # does not raise


def test_get_registered_chart_types_describes_fields() -> None:
    _FakeChart.register()
    fields = ChartTypeRegistry.get_registered_chart_types()["fake"]
    assert fields["metric"].type == "str"
    assert fields["metric"].required is True
    assert fields["metric"].column_kind == ColumnKind.METRIC
    assert fields["flag"].type == "bool"
    assert fields["flag"].required is False
    assert fields["flag"].default is False


def _make_fake_chart(
    chart_name: str, params_type: type[BaseModel]
) -> type[ChartType[typing.Any, pd.DataFrame, dict[str, object]]]:
    """
    Build a minimal registrable `ChartType` around `params_type`.

    Only `_describe_parameter_fields`'s field-type inference is under test wherever this is
    used, so `render`/the `hint_required_*` methods are no-ops — just enough to satisfy
    `ChartType`'s abstract interface.
    """

    class _Fake(ChartType[typing.Any, pd.DataFrame, dict[str, object]], frozen=True, extra="forbid"):
        name: ClassVar[str] = chart_name

        @classmethod
        @typing.override
        def parameter_type(cls) -> type[BaseModel]:
            return params_type

        @classmethod
        @typing.override
        def render(cls, parameters: BaseModel, dataframe: pd.DataFrame) -> dict[str, object]:
            return {}

        @classmethod
        @typing.override
        def hint_required_columns(cls, parameters: BaseModel) -> set[str] | None:
            return set()

        @classmethod
        @typing.override
        def hint_required_artifact_keys(cls, parameters: BaseModel) -> set[str] | None:
            return set()

        @classmethod
        @typing.override
        def hint_required_hparams(cls, parameters: BaseModel) -> set[str] | None:
            return set()

        @classmethod
        @typing.override
        def field_column_kinds(cls) -> dict[str, ColumnKind]:
            return {}

    return _Fake


class _FakeParamsWithChoices(BaseModel, frozen=True, extra="forbid"):
    mode: typing.Literal["prefix", "suffix"] = "prefix"


def test_get_registered_chart_types_describes_literal_field_as_str_with_choices() -> None:
    """A `Literal[...]`-typed field (e.g. line chart's `x_axis_type`) must describe as a plain
    `str` field with its fixed `choices` populated — the UI renders those as a dropdown.
    """
    _make_fake_chart("fake-choices", _FakeParamsWithChoices).register()
    fields = ChartTypeRegistry.get_registered_chart_types()["fake-choices"]
    assert fields["mode"].type == "str"
    assert fields["mode"].choices == ("prefix", "suffix")
    assert fields["mode"].required is False
    assert fields["mode"].default == "prefix"


class _FakeParamsWithOptionalInt(BaseModel, frozen=True, extra="forbid"):
    font_size: int | None = None


def test_get_registered_chart_types_describes_optional_int_field() -> None:
    """`int | None`-typed fields (e.g. table chart's `font_size`) must unwrap to plain `int` rather
    than raising `TypeError` — regression test for a crash when opening the add-chart modal.
    """
    _make_fake_chart("fake-optional-int", _FakeParamsWithOptionalInt).register()
    fields = ChartTypeRegistry.get_registered_chart_types()["fake-optional-int"]
    assert fields["font_size"].type == "int"
    assert fields["font_size"].required is False
    assert fields["font_size"].default is None


def test_chart_instance_delegates_through_registry() -> None:
    _FakeChart.register()
    chart = ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={"metric": "loss"})
    assert chart.render(pd.DataFrame({"loss": [1, 2, 3]})) == {"metric": "loss", "rows": 3}
    assert chart.hint_required_columns() == {"loss"}


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (["loss", "acc"], {"loss", "acc"}),
        (["loss", "all"], None),
    ],
)
def test_panel_hint_required_columns_aggregates_and_short_circuits(
    metrics: list[str], expected: set[str] | None
) -> None:
    _FakeChart.register()
    panel = PanelInstance[pd.DataFrame, dict[str, object]](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={"metric": m})
            for m in metrics
        ],
    )
    assert panel.hint_required_columns() == expected


def test_panel_hint_required_artifact_keys_aggregates() -> None:
    _FakeChart.register()
    panel = PanelInstance[pd.DataFrame, dict[str, object]](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, dict[str, object]](
                chart_type="fake", parameters={"metric": "loss", "flag": True}
            ),
            ChartInstance[pd.DataFrame, dict[str, object]](
                chart_type="fake", parameters={"metric": "acc", "flag": False}
            ),
        ],
    )
    assert panel.hint_required_artifact_keys() == {"img"}


@pytest.mark.parametrize(
    ("hparams", "expected"),
    [
        (["lr", "batch_size"], {"lr", "batch_size"}),
        (["lr", "all"], None),
        (["", ""], set[str]()),
    ],
)
def test_panel_hint_required_hparams_aggregates_and_short_circuits(
    hparams: list[str], expected: set[str] | None
) -> None:
    _FakeChart.register()
    panel = PanelInstance[pd.DataFrame, dict[str, object]](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, dict[str, object]](
                chart_type="fake", parameters={"metric": "loss", "hparam": h}
            )
            for h in hparams
        ],
    )
    assert panel.hint_required_hparams() == expected
