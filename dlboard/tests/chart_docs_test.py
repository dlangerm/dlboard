"""
Every chart type dlboard ships is in the chart plugin docs, and its saved name is in the compatibility promise.

A chart the chart docs leave out is one their reader never learns exists; a name `docs/compatibility.md` leaves out is
one a reader does not know every saved view depends on.
"""

import typing
from pathlib import Path

import pytest

from dlboard.conftest import builtin_chart_types
from dlboard.models import ChartType

_DOCS = Path(__file__).parents[2] / "docs"


@pytest.mark.parametrize("chart_type", [pytest.param(c, id=c.name) for c in builtin_chart_types()])
def test_every_builtin_chart_type_is_documented(
    chart_type: type[ChartType[typing.Any, typing.Any, typing.Any]],
) -> None:
    module = chart_type.__module__.rpartition(".")[2]
    assert f"`{module}.py`" in (_DOCS / "plugins" / "charts.md").read_text(), (
        f"add `{module}.py` to charts.md"
    )
    assert f"`{chart_type.name}`" in (_DOCS / "compatibility.md").read_text(), (
        f"add `{chart_type.name}` to the chart type names in compatibility.md"
    )
