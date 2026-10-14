"""The shared ag-grid theming and dtype inference every grid in the app uses."""

from __future__ import annotations

import pytest

from dlboard.serve._grid import (
    NUMBER_FORMATTER,
    NUMERIC,
    TEXT,
    ColumnDType,
    column_def,
    infer_column_dtype,
    themed_grid_kwargs,
)


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


@pytest.mark.parametrize(("dtype", "formatter"), [(NUMERIC, NUMBER_FORMATTER), (TEXT, None)])
def test_only_a_numeric_column_is_formatted_as_a_number(dtype: ColumnDType, formatter: object) -> None:
    assert column_def("k", dtype).get("valueFormatter") == formatter


def test_themed_grid_kwargs_has_expected_keys() -> None:
    kwargs = themed_grid_kwargs()
    assert set(kwargs) == {"className", "style"}
