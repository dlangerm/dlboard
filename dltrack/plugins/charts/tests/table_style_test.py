# pyright: reportPrivateUsage=false
"""Tests for the shared dash_table.DataTable theming/dtype-inference helpers."""

from __future__ import annotations

import pytest

from dltrack.plugins.charts._table_style import infer_column_dtype, themed_datatable_kwargs


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([{"k": 1}, {"k": 2}], "numeric"),
        ([{"k": "a"}], "text"),
        ([{"k": None}, {"k": 3}], "numeric"),
        ([{"k": None}], "text"),
        ([{"k": True}], "text"),
    ],
)
def test_infer_column_dtype(rows: list[dict[str, object]], expected: str) -> None:
    assert infer_column_dtype(rows, "k") == expected


def test_themed_datatable_kwargs_has_expected_keys() -> None:
    kwargs = themed_datatable_kwargs()
    assert set(kwargs) == {"style_table", "style_header", "style_cell", "style_data_conditional", "css"}
