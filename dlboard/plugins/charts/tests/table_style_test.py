"""Tests for the shared ag-grid theming/dtype-inference helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard.plugins.charts._table_style import format_tags_caption, infer_column_dtype, themed_grid_kwargs

if TYPE_CHECKING:
    from dlboard import models


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


@pytest.mark.parametrize(
    ("tags", "caption"),
    [
        ({"split": "val"}, "split: val"),
        ({"score": 0.140984, "epoch": 3, "best": True}, "score: 0.1410, epoch: 3, best: true"),
        ({"score": "0.1410"}, "score: 0.1410"),
        (
            {"a": 0.14377, "b": 5.0, "c": 1e-05, "d": False, "e": ""},
            "a: 0.1438, b: 5.000, c: 1.000e-05, d: false, e: ",
        ),
        (float("nan"), ""),
    ],
    ids=["text", "scalars", "text-matches-scalar", "float-formats", "gap"],
)
def test_format_tags_caption_formats_scalars_consistently(
    tags: dict[str, models.TagValue] | float, caption: str
) -> None:
    assert format_tags_caption(tags) == caption
