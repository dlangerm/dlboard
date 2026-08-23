"""Shared dash_table.DataTable theming, reused by the hparam table and the table chart type."""

from __future__ import annotations

from typing import Any, Literal

HPARAM_COLUMN_PREFIX = "hparam__"
"""Hyperparameter columns are merged into the same wide dataframe as metrics/artifacts, prefixed
so a hparam key can never collide with a same-named metric or artifact column."""

TAGS_COLUMN_SUFFIX = "__tags"
"""Artifact tag columns (`<artifact_key>__tags`) hold dicts, not scalar values — not renderable
as a plain table cell."""

ColumnDType = Literal["numeric", "text"]
"""A `dash_table.DataTable` column dtype, as inferred by `infer_column_dtype`."""

NUMERIC: ColumnDType = "numeric"
TEXT: ColumnDType = "text"


def infer_column_dtype[T](rows: list[dict[T, Any]], key: T) -> ColumnDType:
    """Guess a column's dash_table dtype from the first non-null value seen."""
    for row in rows:
        val = row.get(key)
        if val is not None:
            return NUMERIC if isinstance(val, (int, float)) and not isinstance(val, bool) else TEXT
    return TEXT


DEFAULT_TABLE_FONT_SIZE = "var(--mantine-font-size-sm)"
"""Default cell/header font size — matches the rest of the charts, which sit noticeably smaller
than a `dash_table.DataTable`'s unstyled (browser-default) text."""


def themed_datatable_kwargs(*, font_size: str = DEFAULT_TABLE_FONT_SIZE) -> dict[str, Any]:
    """
    CSS-var-driven style kwargs so dash_table.DataTable matches the Mantine theme (incl. dark mode).

    Spread into a `dash_table.DataTable(...)` call: `dash_table.DataTable(..., **themed_datatable_kwargs())`.
    """
    return {
        "style_table": {"overflowX": "auto"},
        "style_header": {
            "backgroundColor": "var(--mantine-color-default-hover)",
            "color": "var(--mantine-color-text)",
            "fontFamily": "var(--mantine-font-family)",
            "fontSize": font_size,
            "fontWeight": 600,
            "border": "none",
            "borderBottom": "1px solid var(--mantine-color-default-border)",
        },
        "style_cell": {
            "backgroundColor": "var(--mantine-color-body)",
            "color": "var(--mantine-color-text)",
            "fontFamily": "var(--mantine-font-family)",
            "fontSize": font_size,
            "border": "none",
            "borderBottom": "1px solid var(--mantine-color-default-border)",
            "padding": "6px 10px",
        },
        "style_data_conditional": [
            {
                "if": {"state": "selected"},
                "backgroundColor": "var(--mantine-primary-color-light)",
                "border": "1px solid var(--mantine-primary-color-filled)",
            },
            {
                "if": {"row_index": "odd"},
                "backgroundColor": "var(--mantine-color-default-hover)",
            },
        ],
        "css": [
            {
                "selector": ".dash-filter input",
                "rule": (
                    "background-color: var(--mantine-color-body);"
                    "color: var(--mantine-color-text);"
                    "border: 1px solid var(--mantine-color-default-border);"
                    "border-radius: 4px;"
                ),
            },
            {
                "selector": ".dash-spreadsheet-pagination",
                "rule": "color: var(--mantine-color-text);",
            },
            {
                "selector": ".dash-spreadsheet-pagination button",
                "rule": (
                    "background-color: var(--mantine-color-default-hover);"
                    "color: var(--mantine-color-text);"
                    "border: 1px solid var(--mantine-color-default-border);"
                ),
            },
        ],
    }
