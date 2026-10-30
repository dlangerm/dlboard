"""The grid of a run list: the same on every page, so the list reads the same wherever it is."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import dash_ag_grid as dag

from dlboard.models import MetricColumn
from dlboard.serve import series_swatch_class
from dlboard.serve._grid import themed_grid_kwargs
from dlboard.serve._pages._run_list._columns import FOCUSED_FIELD, ROW_ID_FIELD, SWATCH_FIELD

if TYPE_CHECKING:
    from collections.abc import Collection

ROW_HEIGHT: Final = 34
MAX_TABLE_HEIGHT: Final = "50vh"


def run_grid(
    table_id: str,
    column_defs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    picked: Collection[int],
    *,
    swatches: bool,
    **options: Any,  # noqa: ANN401 -- more `dashGridOptions`
) -> dag.AgGrid:
    """
    `rows` (a run each) as a grid of `column_defs` with a checkbox on each row, `picked` already selected.

    Sized to fit its rows (header included, and room for the empty-table overlay), up to a cap past which it scrolls -- and
    since it isn't `autoHeight`, ag-grid then only renders the rows actually in view, so a 500-run sweep costs the DOM what a
    15-run one does. A run drawn in the charts has its color there as a dot (`swatches`).
    """
    data = [
        {
            **row,
            ROW_ID_FIELD: str(row[MetricColumn.RUN_ID]),
            **({SWATCH_FIELD: series_swatch_class(int(row[MetricColumn.RUN_ID]))} if swatches else {}),
        }
        for row in rows
    ]
    grid = themed_grid_kwargs()
    grid["className"] = f"{grid['className']} dl-run-table"
    grid["style"] = {
        **grid["style"],
        # Less than ag-grid's default padding a side: in a sidebar this narrow it is a third of every column.
        "--ag-cell-horizontal-padding": "8px",
        "height": f"min({(max(len(rows), 2) + 1) * ROW_HEIGHT + 2}px, {MAX_TABLE_HEIGHT})",
    }
    return dag.AgGrid(
        id=table_id,
        columnDefs=column_defs,
        rowData=data,
        # A dedicated already-stringified field, not `params.data.run_id` directly -- dash-ag-grid's
        # string-prop JS parser only supports bare expressions (no function calls like `String(...)`),
        # so casting has to happen on the Python side instead.
        getRowId=f"params.data.{ROW_ID_FIELD}",
        # `getRowId` (and the numeric columns' `valueFormatter`, via `column_def`) are JS expression
        # strings -- dash-ag-grid silently no-ops any string-valued JS prop unless this is set. Safe
        # here: every such string is static and written by us, never derived from user/request data.
        dangerously_allow_code=True,
        selectedRows={
            "ids": [str(row[MetricColumn.RUN_ID]) for row in rows if row[MetricColumn.RUN_ID] in picked]
        },
        columnSize="responsiveSizeToFit",
        dashGridOptions={
            "rowHeight": ROW_HEIGHT,
            "headerHeight": ROW_HEIGHT,
            "rowSelection": {
                "mode": "multiRow",
                "checkboxes": True,
                "headerCheckbox": True,
                # With a filter on, the header checkbox is the runs it matches, not every run of a long experiment.
                "selectAll": "filtered",
            },
            "rowClassRules": {"dl-run-focused": f"params.data.{FOCUSED_FIELD}"},
            # The checkbox column takes no more room than its checkbox, so the run's name can have it.
            "selectionColumnDef": {"width": 32, "minWidth": 32, "maxWidth": 32, "resizable": False},
            "tooltipShowDelay": 300,
            **options,
        },
        **grid,
    )
