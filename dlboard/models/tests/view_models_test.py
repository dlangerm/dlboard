"""Tests for the chart/panel view model registry and aggregation logic."""

from __future__ import annotations

import typing
from typing import ClassVar

import pandas as pd
import pytest
from pydantic import BaseModel, Field, ValidationError

from dlboard.models import is_inlineable_artifact
from dlboard.models._view import (
    ChartInstance,
    ChartType,
    ChartTypeRegistry,
    ColumnKind,
    PanelInstance,
    UnknownChartTypeError,
)


class _FakeParams(BaseModel, frozen=True, extra="forbid"):
    metric: str = Field(description="Which metric to plot.")
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
    def natural_width(cls, parameters: _FakeParams) -> int:
        return 400

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
    assert fields["metric"].description == "Which metric to plot."
    assert fields["flag"].type == "bool"
    assert fields["flag"].required is False
    assert fields["flag"].default is False
    assert fields["flag"].description is None


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
        def natural_width(cls, parameters: BaseModel) -> int:
            return 400

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


# ---- a chart whose persisted parameters no longer validate must not crash the whole page ----


def test_hint_and_width_methods_fall_back_instead_of_raising_for_invalid_parameters() -> None:
    """
    Regression: parameters are validated against the chart type's settings model lazily, on every
    call -- a chart saved before a schema change (or otherwise malformed) used to raise straight out
    of `hint_required_columns`/`natural_width`, called unguarded while just laying out the page, and
    take the whole page down with an unhandled 500. These must degrade gracefully instead; only
    `render()` (already isolated per-chart by the page layer) is allowed to still raise.
    """
    _FakeChart.register()
    # `metric` is required by `_FakeParams`, so an empty `parameters` dict fails validation.
    chart = ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={})

    assert chart.hint_required_columns() is None
    assert chart.hint_required_artifact_keys() is None
    assert chart.hint_required_hparams() is None
    assert chart.natural_width() == 400

    with pytest.raises(ValidationError):
        chart.render(pd.DataFrame())


def test_hint_and_width_methods_fall_back_instead_of_raising_for_an_unregistered_chart_type() -> None:
    """
    Regression: a saved view can name a chart type whose plugin isn't installed on this deployment
    (removed, or just not part of this server's plugin list) -- `ChartTypeRegistry` raises
    `UnknownChartTypeError` (a `KeyError`) for that, and these must degrade the same way they do for
    invalid parameters, not crash the whole page's layout.
    """
    chart = ChartInstance[pd.DataFrame, dict[str, object]](chart_type="not-installed", parameters={})

    assert chart.hint_required_columns() is None
    assert chart.hint_required_artifact_keys() is None
    assert chart.hint_required_hparams() is None
    assert chart.natural_width() == 400

    with pytest.raises(UnknownChartTypeError):
        chart.render(pd.DataFrame())


def test_panel_hint_required_columns_short_circuits_when_one_chart_is_broken() -> None:
    """A broken chart's `None` hint must widen the whole panel's fetch to "everything" (the existing
    short-circuit semantics for `None`), not crash the aggregation.
    """
    _FakeChart.register()
    panel = PanelInstance[pd.DataFrame, dict[str, object]](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={"metric": "loss"}),
            ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={}),
        ],
    )
    assert panel.hint_required_columns() is None


def test_panel_hint_required_columns_short_circuits_when_one_chart_type_is_unregistered() -> None:
    """Same short-circuit as a broken chart's parameters, but for a chart type with no plugin installed."""
    _FakeChart.register()
    panel = PanelInstance[pd.DataFrame, dict[str, object]](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={"metric": "loss"}),
            ChartInstance[pd.DataFrame, dict[str, object]](chart_type="not-installed", parameters={}),
        ],
    )
    assert panel.hint_required_columns() is None


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


def test_a_chart_without_an_id_gets_a_stable_one_from_its_content() -> None:
    chart = ChartInstance[object, object](chart_type="line", parameters={"column": "loss"})

    assert chart.id
    assert chart.id == ChartInstance[object, object](chart_type="line", parameters={"column": "loss"}).id
    assert chart.id != ChartInstance[object, object](chart_type="line", parameters={"column": "acc"}).id


def test_a_saved_chart_keeps_its_id_when_its_parameters_change() -> None:
    saved = ChartInstance[object, object].model_validate_json(
        ChartInstance[object, object](chart_type="line", parameters={"column": "loss"}).model_dump_json()
    )

    edited = ChartInstance[object, object].model_validate(
        {**saved.model_dump(), "parameters": {"column": "loss", "height": 400}}
    )

    assert edited.id == saved.id


@pytest.mark.parametrize(
    ("fname", "inline"),
    [
        ("a.png", True),
        ("a.jpg", True),
        ("a.webp", True),
        ("last.ckpt", False),
        ("plot.svg", False),
        ("a", False),
    ],
)
def test_only_raster_images_are_shown_inline(fname: str, *, inline: bool) -> None:
    """Matches what the download route serves without forcing a download (SVG can carry script)."""
    assert is_inlineable_artifact(fname) is inline
