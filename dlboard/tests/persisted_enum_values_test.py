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

from dlboard.models import AuditAction, EntityType, ExperimentSource, ProjectRole, Scope
from dlboard.models._metric import MetricColumn
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart

if typing.TYPE_CHECKING:
    from dlboard._compat import StrEnum
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
    MetricColumn: frozenset({"run_id", "step", "timestamp_utc"}),
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
