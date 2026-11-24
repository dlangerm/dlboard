"""The shared ag-grid theming and dtype inference every grid in the app uses."""

from __future__ import annotations

import pytest

from dlboard.conftest import props
from dlboard.serve._grid import (
    CELL_TOOLTIP,
    NUMBER_FORMATTER,
    NUMERIC,
    TEXT,
    ColumnDType,
    Tooltips,
    column_def,
    grid,
    infer_column_dtype,
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


def test_a_grid_is_themed_with_a_class_and_style_of_its_own_merged_over_the_themes() -> None:
    made = props(
        grid(id="g", class_name="dl-mine", style={"height": "40px"}, dashGridOptions={"rowHeight": 30})
    )

    assert made["className"] == "ag-theme-quartz dl-mine"
    assert made["style"]["height"] == "40px"
    assert made["style"]["--ag-background-color"] == "var(--mantine-color-body)"
    assert made["dashGridOptions"]["rowHeight"] == 30


@pytest.mark.parametrize(
    ("tooltips", "options", "columns"),
    [
        (Tooltips.WHEN_CUT_OFF, {"tooltipShowMode": "whenTruncated"}, {"tooltipValueGetter": CELL_TOOLTIP}),
        (Tooltips.COLUMNS_OWN, {}, {}),
    ],
)
def test_a_grid_says_what_a_cut_off_cell_holds_unless_its_columns_say_more(
    tooltips: Tooltips, options: dict[str, str], columns: dict[str, object]
) -> None:
    made = props(grid(id="g", tooltips=tooltips, defaultColDef={"minWidth": 100}))

    assert made["dashGridOptions"].get("tooltipShowMode") == options.get("tooltipShowMode")
    assert made["defaultColDef"] == {**columns, "minWidth": 100}


def test_a_columns_header_says_its_whole_name_where_it_is_cut_off() -> None:
    assert column_def("k", TEXT, headerName="A long name").get("headerTooltip") == "A long name"
