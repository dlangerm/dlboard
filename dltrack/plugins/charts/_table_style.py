"""Shared dash-ag-grid theming, reused by the hparam table and the table chart type."""

from __future__ import annotations

from typing import Any, Literal

HPARAM_COLUMN_PREFIX = "hparam__"
"""Hyperparameter columns are merged into the same wide dataframe as metrics/artifacts, prefixed
so a hparam key can never collide with a same-named metric or artifact column."""

ARTIFACT_COLUMN_PREFIX = "artifact__"
"""Artifact columns are prefixed for the same reason -- see `artifact_column`/`artifact_tags_column`."""


def artifact_column(key: str) -> str:
    """The panel-dataframe column holding each step's ref for artifact `key`."""
    return f"{ARTIFACT_COLUMN_PREFIX}{key}"


def artifact_tags_column(key: str) -> str:
    """The panel-dataframe column holding each step's tags dict for artifact `key`."""
    return f"{artifact_column(key)}__tags"


ColumnDType = Literal["numeric", "text"]
"""An ag-grid column dtype, as inferred by `infer_column_dtype`."""

NUMERIC: ColumnDType = "numeric"
TEXT: ColumnDType = "text"


def infer_column_dtype[T](rows: list[dict[T, Any]], key: T) -> ColumnDType:
    """Guess a column's ag-grid dtype from the first non-null value seen."""
    for row in rows:
        val = row.get(key)
        if val is not None:
            return NUMERIC if isinstance(val, (int, float)) and not isinstance(val, bool) else TEXT
    return TEXT


def column_def(name: str, dtype: ColumnDType, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    """Build one ag-grid `columnDefs` entry, `**extra` merged in last so callers can override any key."""
    base: dict[str, Any] = {"field": name, "headerName": name, "sortable": True, "filter": True}
    if dtype == NUMERIC:
        base["filter"] = "agNumberColumnFilter"
        base["valueFormatter"] = {"function": "params.value == null ? '' : params.value.toFixed(3)"}
    return {**base, **extra}


DEFAULT_TABLE_FONT_SIZE = "var(--mantine-font-size-sm)"
"""Default cell/header font size — matches the rest of the charts, which sit noticeably smaller
than ag-grid's default text."""


def themed_grid_kwargs(*, font_size: str = DEFAULT_TABLE_FONT_SIZE) -> dict[str, Any]:
    """
    CSS-var-driven style/className kwargs so an `AgGrid` matches the Mantine theme (incl. dark mode).

    Spread into a `dash_ag_grid.AgGrid(...)` call: `AgGrid(..., **themed_grid_kwargs())`.
    """
    return {
        "className": "ag-theme-quartz",
        "style": {
            "width": "100%",
            "--ag-background-color": "var(--mantine-color-body)",
            "--ag-foreground-color": "var(--mantine-color-text)",
            "--ag-header-background-color": "var(--mantine-color-default-hover)",
            "--ag-header-foreground-color": "var(--mantine-color-text)",
            "--ag-border-color": "var(--mantine-color-default-border)",
            "--ag-row-border-color": "var(--mantine-color-default-border)",
            "--ag-odd-row-background-color": "var(--mantine-color-default-hover)",
            "--ag-row-hover-color": "var(--mantine-color-default-hover)",
            "--ag-selected-row-background-color": "var(--mantine-primary-color-light)",
            "--ag-font-family": "var(--mantine-font-family)",
            "--ag-font-size": font_size,
            "--ag-border-radius": "var(--mantine-radius-md)",
        },
    }
