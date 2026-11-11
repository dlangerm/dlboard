"""
Enum values (and chart type names) that are part of the storage/wire contract, frozen.

Every one of these is either written into a database column as its plain string value (an enum
has no SQL type of its own -- see `_sql.py:column_type`), sent over the REST API, or both. Renaming
a Python member is free -- only the *value* ever reaches a column or the wire -- but renaming or
removing a *value* breaks every row (or every client/server) that already has it. Adding a new
value is always safe, so each check here is "at least this set", not "exactly this set": that's
what lets a real chart type or scope get added without this test standing in the way.

A failure here means the change needs a compatibility plan (a migration translating the old value,
or documenting the break for the next major) before it ships -- see `docs/compatibility.md`.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import BaseModel

from dlboard._compat import StrEnum
from dlboard.conftest import builtin_chart_types
from dlboard.models import (
    AuditAction,
    EntityType,
    ExperimentSource,
    FileKind,
    PinColor,
    ProjectRole,
    RunStatus,
    Scope,
    SizeUnit,
    TimeUnit,
)
from dlboard.models._metric import MetricColumn
from dlboard.plugins.charts._table_settings import Aggregate, TableMode, TableRows
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.file_list import FileListChart
from dlboard.plugins.charts.histogram import Colorscale, HistogramChart, HistogramDisplay
from dlboard.plugins.charts.image_series import ImageChart, LayerVisibility
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.series_band import SeriesBandChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._custodian import TaskOutcome

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

    from dlboard.models._view import ChartType

_FROZEN_ENUM_VALUES: dict[type[StrEnum], frozenset[str]] = {
    Scope: frozenset(
        {
            "*",
            "project:delete",
            "experiment:delete",
            "run:delete",
            "artifact:delete",
            "restore",
            "purge",
            "audit_log:read",
            "user:manage",
        }
    ),
    ProjectRole: frozenset({"viewer", "editor", "owner"}),
    AuditAction: frozenset({"soft_delete", "restore", "purge"}),
    EntityType: frozenset({"project", "experiment", "run", "artifact"}),
    ExperimentSource: frozenset({"pytorch_lightning"}),
    FileKind: frozenset({"checkpoint"}),
    RunStatus: frozenset({"running", "finished", "failed", "unknown"}),
    MetricColumn: frozenset({"run_id", "step", "timestamp_utc"}),
    TableMode: frozenset({"runs", "pivot", "artifacts"}),
    TableRows: frozenset({"run", "field", "artifact"}),
    Aggregate: frozenset({"latest", "first", "min", "max", "mean"}),
    HistogramDisplay: frozenset({"heatmap", "bands", "step"}),
    Colorscale: frozenset({"Viridis", "Cividis", "Plasma", "Inferno", "Magma", "Blues"}),
    LayerVisibility: frozenset({"shown", "hidden", "off"}),
    TimeUnit: frozenset({"ns", "us", "ms", "s", "min", "h", "d"}),
    SizeUnit: frozenset({"B", "kB", "MB", "GB", "TB"}),
    PinColor: frozenset(
        {"plain", "red", "orange", "yellow", "green", "teal", "blue", "violet", "pink", "gray"}
    ),
    TaskOutcome: frozenset({"success", "failure", "hung"}),
}


@pytest.mark.parametrize(("enum_cls", "frozen_values"), _FROZEN_ENUM_VALUES.items())
def test_persisted_enum_keeps_every_frozen_value(
    enum_cls: type[StrEnum], frozen_values: frozenset[str]
) -> None:
    assert {member.value for member in enum_cls} >= frozen_values


_FROZEN_CHART_TYPE_NAMES: dict[type[ChartType[typing.Any, typing.Any, typing.Any]], str] = {
    ImageChart: "image",
    LineChart: "line",
    BarChart: "bar",
    TableChart: "table",
    HistogramChart: "histogram",
    FileListChart: "files",
    SeriesBandChart: "series_band",
}


@pytest.mark.parametrize(("chart_type", "frozen_name"), _FROZEN_CHART_TYPE_NAMES.items())
def test_builtin_chart_type_keeps_its_frozen_name(
    chart_type: type[ChartType[typing.Any, typing.Any, typing.Any]], frozen_name: str
) -> None:
    """
    A `Page`'s saved charts store `chart_type` as this plain string (`ChartInstance.chart_type`) --
    renaming it here would make every chart of this type in every saved view unrenderable
    (`UnknownChartTypeError`, see `ChartInstance.render`).
    """
    assert chart_type.name == frozen_name


@pytest.mark.parametrize("chart_type", [pytest.param(c, id=c.name) for c in builtin_chart_types()])
def test_every_builtin_chart_type_has_a_frozen_name(
    chart_type: type[ChartType[typing.Any, typing.Any, typing.Any]],
) -> None:
    assert chart_type in _FROZEN_CHART_TYPE_NAMES, f"freeze {chart_type.__name__}'s name here"


_FROZEN_SETTING_LITERALS: dict[str, frozenset[str]] = {
    "line.x_axis_type": frozenset({"number", "category", "date", "time"}),
    "bar.aggregation": frozenset({"latest", "mean", "median", "min", "max", "sum", "count"}),
    "bar.orientation": frozenset({"horizontal", "vertical"}),
    "bar.sort": frozenset({"none", "ascending", "descending"}),
    "bar.x_axis_type": frozenset({"auto", "number", "category", "date"}),
}
"""The values of each `typing.Literal` setting a built-in chart saves, by `<chart type>.<setting>`."""


def _fixed_value_fields(annotation: object, path: str) -> Iterator[tuple[str, object]]:
    """Every `Literal` or `StrEnum` in a settings model, through optionals, containers and nested models."""
    if typing.get_origin(annotation) is typing.Literal or (
        isinstance(annotation, type) and issubclass(annotation, StrEnum)
    ):
        yield path, annotation
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        for name, field in annotation.model_fields.items():
            yield from _fixed_value_fields(field.annotation, f"{path}.{name}")
    else:
        for argument in typing.get_args(annotation):
            yield from _fixed_value_fields(argument, path)


_BUILTIN_SETTINGS_WITH_FIXED_VALUES: dict[str, object] = {
    path: annotation
    for chart_type in _FROZEN_CHART_TYPE_NAMES
    for path, annotation in _fixed_value_fields(chart_type.parameter_type(), chart_type.name)
}


@pytest.mark.parametrize(("setting", "frozen_values"), _FROZEN_SETTING_LITERALS.items())
def test_chart_setting_literal_keeps_every_frozen_value(setting: str, frozen_values: frozenset[str]) -> None:
    """A chart's settings are saved in a view as JSON, so dropping a value fails every saved chart that has it."""
    assert set(typing.get_args(_BUILTIN_SETTINGS_WITH_FIXED_VALUES[setting])) >= frozen_values


@pytest.mark.parametrize(("setting", "annotation"), _BUILTIN_SETTINGS_WITH_FIXED_VALUES.items())
def test_every_built_in_chart_setting_with_fixed_values_is_frozen(setting: str, annotation: object) -> None:
    """A new `Literal` or enum setting on a built-in chart must be listed above, so it is frozen from the start."""
    if isinstance(annotation, type):
        assert annotation in _FROZEN_ENUM_VALUES, f"freeze {annotation.__name__} in _FROZEN_ENUM_VALUES"
    else:
        assert setting in _FROZEN_SETTING_LITERALS, f"freeze {setting} in _FROZEN_SETTING_LITERALS"
