# pyright: reportPrivateUsage=false
"""Tests for the shared ag-grid theming/dtype-inference helpers."""

from __future__ import annotations

import pytest

from dlboard.plugins.charts._table_style import infer_column_dtype, themed_grid_kwargs


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


def test_themed_grid_kwargs_has_expected_keys() -> None:
    kwargs = themed_grid_kwargs()
    assert set(kwargs) == {"className", "style"}
