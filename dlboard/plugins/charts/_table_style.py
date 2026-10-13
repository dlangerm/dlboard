"""Shared dash-ag-grid theming, reused by the hparam table and the table chart type."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import TypeAdapter

from dlboard.models import TagDisplay, TagValue, format_tag_value

if TYPE_CHECKING:
    from collections.abc import Mapping

HPARAM_COLUMN_PREFIX = "hparam__"
"""Hyperparameter columns are merged into the same wide dataframe as metrics/artifacts, prefixed
so a hparam key can never collide with a same-named metric or artifact column."""

ARTIFACT_COLUMN_PREFIX = "artifact__"
"""Artifact columns are prefixed for the same reason -- see `artifact_column`/`artifact_tags_column`."""


def artifact_column(key: str) -> str:
    """The panel-dataframe column holding each step's download URL for artifact `key`."""
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


_TAGS_ADAPTER: TypeAdapter[dict[str, TagValue]] = TypeAdapter(dict[str, TagValue])


def parse_tags(raw_tags: object) -> dict[str, TagValue]:
    """
    An artifact `<key>__tags` cell as typed tags.

    `build_artifacts_dataframe` puts each artifact's tags in as plain JSON (a `Quantity` is a
    `{"value": ..., "unit": ...}` dict by then), and a `float` (NaN) where pivoting left a gap for a
    (run, step) with no tags logged -- the sole non-dict case, which has none.
    """
    return _TAGS_ADAPTER.validate_python(raw_tags) if isinstance(raw_tags, dict) else {}


def format_tags_caption(
    raw_tags: object,
    display: TagDisplay = TagDisplay(),  # noqa: B008
    by_key: Mapping[str, TagDisplay] | None = None,
) -> str:
    """
    Format an artifact `<key>__tags` cell into a caption.

    The cell is read with `parse_tags`.
    Every tag is shown as `display`, except one named in `by_key`, which that overrides.
    """
    by_key = by_key or {}
    return ", ".join(
        f"{k}: {format_tag_value(v, display.overridden_by(by_key[k]) if k in by_key else display)}"
        for k, v in parse_tags(raw_tags).items()
    )


NUMBER_FORMATTER: Final = {
    "function": "params.value == null ? '' : params.value % 1 === 0 ? params.value.toFixed(0)"
    " : Math.abs(params.value) >= 1 ? params.value.toFixed(3) : params.value.toPrecision(4) * 1"
}
"""
How a numeric cell is shown: whole numbers as they are, otherwise three decimals from 1 up and four
significant digits below it (so a learning rate of 1e-4 is not `0.000`).

Written with only what dash-ag-grid's own expression parser evaluates (`Number.isInteger(...)` or
`String(...)` is silently ignored, leaving the raw value); `browser_test.py` checks it in a real grid.
"""


def column_def(name: str, dtype: ColumnDType, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    """Build one ag-grid `columnDefs` entry, `**extra` merged in last so callers can override any key."""
    base: dict[str, Any] = {"field": name, "headerName": name, "sortable": True, "filter": True}
    if dtype == NUMERIC:
        base["filter"] = "agNumberColumnFilter"
        base["valueFormatter"] = NUMBER_FORMATTER
    return {**base, **extra}


DEFAULT_TABLE_FONT_SIZE = "var(--mantine-font-size-sm)"
"""Default cell/header font size — matches the rest of the charts, which sit noticeably smaller
than ag-grid's default text."""


def themed_grid_kwargs(*, font_size: str = DEFAULT_TABLE_FONT_SIZE, class_name: str = "") -> dict[str, Any]:
    """
    CSS-var-driven style/className kwargs so an `AgGrid` matches the Mantine theme (incl. dark mode).

    Spread into a `dash_ag_grid.AgGrid(...)` call: `AgGrid(..., **themed_grid_kwargs())`. `class_name`
    adds a class of the caller's own, for its stylesheet to pick the grid out by.
    """
    return {
        "className": f"ag-theme-quartz {class_name}".strip(),
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
