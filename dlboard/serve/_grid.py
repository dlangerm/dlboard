"""
Shared dash-ag-grid theming and value formatting, for every grid in the app: the run tables and chart types alike.

The formatters a column def names (`NUMBER_FORMATTER`, `SCORE_FORMATTER`) are defined in `GRID_FORMATTERS_JS`,
which `app()` serves once for the whole app rather than each page or chart that builds a grid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal

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


GRID_FORMATTERS_JS: Final = Path(__file__).with_name("grid_formatters.js")
"""The grid formatters `NUMBER_FORMATTER` and `SCORE_FORMATTER` name, served once by `app()`."""

GRID_CELL_COMPONENTS_JS: Final = Path(__file__).with_name("grid_cell_components.js")
"""The cell components `SLIDER_RENDERER` and `KEY_PICKER_EDITOR` name, served once by `app()`."""

GRID_CELL_COMPONENTS_CSS: Final = Path(__file__).with_name("grid_cell_components.css")
"""How `KEY_PICKER_EDITOR` looks, served once by `app()` beside its script."""

SLIDER_RENDERER: Final = "DlboardSlider"
"""How a number with a range is shown in a cell: a slider across it, always there. Takes `cellRendererParams` of `min`, `max`, `step` and `fallback`."""

CHOICE_RENDERER: Final = "DlboardChoice"
"""How one of a few choices is picked in a cell: side by side, the current one marked, one click to change it. Takes `cellRendererParams` of `choices` and `fallback`."""

KEY_PICKER_EDITOR: Final = "DlboardKeyPicker"
"""How a name is edited in a cell: typed in, with the names it can be (`options` of `cellEditorParams`) suggested."""

PINS_RENDERER: Final = "DlboardPins"
"""How a run's pins are shown in a cell: one pin filled with the colours it is pinned in, and an editor to change yours. Takes `cellRendererParams` of `colors`."""

PINS_TOOLTIP: Final = "DlboardPinsTooltip"
"""How a run's pins are shown in a tooltip: a pin in each colour and who pinned the run in it."""

EYE_RENDERER: Final = "DlboardEye"
"""How a run is drawn or hidden in a cell: an eye, open if the cell's value is true. A click is reported as the grid's `cellRendererData`."""

RUN_LINK_RENDERER: Final = "DlboardRunLink"
"""How a run's name is shown in a cell: a link to the page of that run, which is its row's `_href`."""

NUMBER_FORMATTER: Final = {"function": "dlboardNumber(params)"}
"""How a numeric cell is shown: whole numbers as they are, small ones to four significant digits."""

SCORE_FORMATTER: Final = {"function": "dlboardScore(params)"}
"""How a file's score is shown: four significant digits."""


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
