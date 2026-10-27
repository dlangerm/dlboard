"""Tests for the chart/panel view model registry and aggregation logic."""

from __future__ import annotations

import typing
from typing import ClassVar

import pandas as pd
import pytest
from pydantic import BaseModel, Field, ValidationError

from dlboard.conftest import make_fake_chart
from dlboard.models import DataRequest, is_inlineable_artifact
from dlboard.models._view import (
    ADVANCED_SECTION,
    DATA_SECTION,
    DISPLAY_SECTION,
    ChartInstance,
    ChartType,
    ChartTypeRegistry,
    ColumnKind,
    ParameterField,
    ParameterFieldType,
    SettingsSection,
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
    def data_request(cls, parameters: _FakeParams) -> DataRequest:
        return DataRequest(
            metrics=frozenset({parameters.metric}), artifacts=frozenset({"img"} if parameters.flag else ())
        )

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


class _FakeParamsWithChoices(BaseModel, frozen=True, extra="forbid"):
    mode: typing.Literal["prefix", "suffix"] = "prefix"


def test_get_registered_chart_types_describes_literal_field_as_str_with_choices() -> None:
    """A `Literal[...]`-typed field (e.g. line chart's `x_axis_type`) must describe as a plain
    `str` field with its fixed `choices` populated — the UI renders those as a dropdown.
    """
    make_fake_chart("fake-choices", _FakeParamsWithChoices).register()
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
    make_fake_chart("fake-optional-int", _FakeParamsWithOptionalInt).register()
    fields = ChartTypeRegistry.get_registered_chart_types()["fake-optional-int"]
    assert fields["font_size"].type == "int"
    assert fields["font_size"].required is False
    assert fields["font_size"].default is None


_CUSTOM_SECTION = SettingsSection("Custom", opened=True)


class _FakeParamsWithSections(BaseModel, frozen=True, extra="forbid"):
    metric: str = ""
    height: typing.Annotated[int, DISPLAY_SECTION] = 300
    limit: typing.Annotated[int, ADVANCED_SECTION, _CUSTOM_SECTION] = 5
    width: typing.Annotated[int | None, DISPLAY_SECTION] = None


def test_a_setting_is_in_the_section_it_is_marked_with_and_the_data_section_if_unmarked() -> None:
    make_fake_chart("fake-sectioned", _FakeParamsWithSections).register()

    fields = ChartTypeRegistry.get_registered_chart_types()["fake-sectioned"]

    assert {name: field.section for name, field in fields.items()} == {
        "metric": DATA_SECTION,
        "height": DISPLAY_SECTION,
        "limit": ADVANCED_SECTION,  # the first mark wins
        "width": DISPLAY_SECTION,
    }


class _FakeParamsWithTwoSectionsOfOneTitle(BaseModel, frozen=True, extra="forbid"):
    height: typing.Annotated[int, SettingsSection("Display", opened=True)] = 300
    width: typing.Annotated[int, DISPLAY_SECTION] = 400


def test_two_different_sections_may_not_share_a_title(monkeypatch: pytest.MonkeyPatch) -> None:
    clashing = make_fake_chart("fake-clashing", _FakeParamsWithTwoSectionsOfOneTitle)
    # Registered for this test only: a chart type that cannot be described would fail every later listing of them.
    monkeypatch.setitem(ChartTypeRegistry._all_charts, clashing.name, clashing)

    with pytest.raises(ValueError, match="two different settings sections"):
        ChartTypeRegistry.get_registered_chart_types()


class _FakeParamsWithTitles(BaseModel, frozen=True, extra="forbid"):
    max_points: int = 100
    lr: typing.Annotated[float, Field(title="LR")] = 0.1
    native_size: bool = Field(default=False, title="Logged resolution")


@pytest.mark.parametrize(
    ("name", "label"),
    [
        pytest.param("max_points", "Max points", id="a-name-is-a-sentence-by-default"),
        pytest.param("lr", "LR", id="a-title-is-what-the-setting-is-called"),
        pytest.param("native_size", "Logged resolution", id="a-title-wins-over-the-sentence"),
    ],
)
def test_a_setting_is_called_by_its_title_or_else_its_name_as_a_sentence(name: str, label: str) -> None:
    make_fake_chart("fake-titled", _FakeParamsWithTitles).register(allow_override=True)

    assert ChartTypeRegistry.get_registered_chart_types()["fake-titled"][name].label == label


def test_chart_instance_delegates_through_registry() -> None:
    _FakeChart.register()
    chart = ChartInstance[pd.DataFrame, dict[str, object]](chart_type="fake", parameters={"metric": "loss"})
    assert chart.render(pd.DataFrame({"loss": [1, 2, 3]})) == {"metric": "loss", "rows": 3}
    assert chart.data_request() == DataRequest(metrics=frozenset({"loss"}))


# ---- a chart whose persisted parameters no longer validate must not crash the whole page ----


@pytest.mark.parametrize(
    ("chart_type", "parameters", "render_error"),
    [
        # `metric` is required by `_FakeParams`, so an empty `parameters` dict fails validation.
        pytest.param("fake", {}, ValidationError, id="invalid-parameters"),
        # A saved view can name a chart type whose plugin isn't installed on this deployment.
        pytest.param("not-installed", {}, UnknownChartTypeError, id="unregistered-chart-type"),
    ],
)
def test_a_broken_chart_reads_nothing_and_has_a_width_instead_of_raising(
    chart_type: str, parameters: dict[str, object], render_error: type[Exception]
) -> None:
    """
    Regression: parameters are validated against the chart type's settings model lazily, on every
    call -- a chart saved before a schema change (or otherwise malformed) used to raise straight out
    of what it reads and `natural_width`, called unguarded while just laying out the page, and
    take the whole page down with an unhandled 500. The same went for a chart type no installed
    plugin provides. These must degrade gracefully instead; only `render()` (already isolated
    per-chart by the page layer) is allowed to still raise, so nothing is read for a chart that cannot use it.
    """
    _FakeChart.register()
    chart = ChartInstance[pd.DataFrame, dict[str, object]](chart_type=chart_type, parameters=parameters)

    assert chart.data_request() == DataRequest()
    assert chart.natural_width() == 400

    with pytest.raises(render_error):
        chart.render(pd.DataFrame())


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


@pytest.mark.parametrize(
    ("name", "label"),
    [("height", "Height"), ("tag_precision", "Tag precision"), ("x_axis", "X axis")],
)
def test_the_editor_calls_a_setting_by_its_name_as_a_sentence(name: str, label: str) -> None:
    assert ParameterField(name=name, type=ParameterFieldType.STR, required=False).label == label
