"""The columns and fields every run list has: an eye, the run's name as a link, and how it ended."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from dlboard import models
from dlboard.models import RUN_NAME_COLUMN, MetricColumn
from dlboard.serve._format import format_duration
from dlboard.serve._grid import EYE_RENDERER, RUN_LINK_RENDERER
from dlboard.serve._pages._dataframe_helpers import run_display_name

if TYPE_CHECKING:
    from dlboard.models import Run

ROW_ID_FIELD: Final = "_row_id"
SWATCH_FIELD: Final = "_swatch"
DRAWN_FIELD: Final = "_drawn"
HREF_FIELD: Final = "_href"
FOCUSED_FIELD: Final = "_focused"
STATUS_FIELD: Final = "_status"
DURATION_FIELD: Final = "_duration_s"
STATUS_TEXT_FIELD: Final = "_status_text"
STATUS_DETAIL_FIELD: Final = "_status_detail"
"""Reserved row fields (underscore-prefixed, like `_swatch`): a hyperparameter or metric named "status" can't shadow them."""

EYE_COLUMN_WIDTH: Final = 32
NAME_MIN_WIDTH: Final = 96
STATUS_MIN_WIDTH: Final = 74
"""Room for "✕ 1h 2m" in the narrow sidebar a run list lives in."""
STATUS_STYLES: Final[dict[models.RunStatus, str]] = {
    models.RunStatus.RUNNING: "var(--mantine-color-blue-5)",
    models.RunStatus.FINISHED: "var(--mantine-color-dimmed)",
    models.RunStatus.FAILED: "var(--mantine-color-red-6)",
    models.RunStatus.UNKNOWN: "var(--mantine-color-yellow-6)",
}


def _duration_seconds(run: Run) -> int | None:
    """How long the run took, if it has ended."""
    return int((run.ended_at - run.created_at).total_seconds()) if run.ended_at else None


def _status_text(run: Run) -> str:
    """The cell's text, short enough for the sidebar: "running", how long it took, marked if it failed or is unknown."""
    seconds = _duration_seconds(run)
    took = "" if seconds is None else format_duration(seconds)
    match run.status:
        case None:
            return ""
        case models.RunStatus.RUNNING:
            return "running"
        case models.RunStatus.FINISHED:
            return took
        case models.RunStatus.FAILED:
            return f"✕ {took}".strip()
        case models.RunStatus.UNKNOWN:
            return f"? {took}".strip()


def _status_detail(run: Run) -> str:
    """The cell's tooltip: how the run ended and how long it took, in words. Empty for a run with no status."""
    seconds = _duration_seconds(run)
    after = "" if seconds is None else f" after {format_duration(seconds)}"
    match run.status:
        case None:
            return ""
        case models.RunStatus.RUNNING:
            return "running"
        case models.RunStatus.FINISHED | models.RunStatus.FAILED:
            return f"{run.status.value}{after}"
        case models.RunStatus.UNKNOWN:
            return f"ended without saying how{after}"


def run_fields(run: Run) -> dict[str, Any]:
    """The fields of a run's row that every run list has: its id and name, and how it ended and how long it took."""
    return {
        MetricColumn.RUN_ID: run.id,
        RUN_NAME_COLUMN: run_display_name(run),
        STATUS_FIELD: run.status.value if run.status else None,
        DURATION_FIELD: _duration_seconds(run),
        STATUS_TEXT_FIELD: _status_text(run),
        STATUS_DETAIL_FIELD: _status_detail(run),
    }


def eye_column_def() -> dict[str, Any]:
    """Whether the run is drawn in the charts, as an eye to click. Sorts the drawn runs together."""
    return {
        "field": DRAWN_FIELD,
        "headerName": "",
        "headerTooltip": "Drawn in the charts",
        "cellRenderer": EYE_RENDERER,
        "cellClass": "dl-eye-cell",
        "sortable": True,
        "filter": False,
        "width": EYE_COLUMN_WIDTH,
        "maxWidth": EYE_COLUMN_WIDTH,
        "suppressSizeToFit": True,
    }


def name_column_def(*, swatch: bool) -> dict[str, Any]:
    """The run's name, as a link to the run (its row's `_href`); with its color in the charts as a dot, if it is drawn there."""
    return {
        "field": RUN_NAME_COLUMN,
        "headerName": "Run",
        "sortable": True,
        "filter": True,
        "cellRenderer": RUN_LINK_RENDERER,
        # The name is what the list is for: it takes what the narrow columns around it leave.
        "minWidth": NAME_MIN_WIDTH,
        # Each run's chart color as a dot before its name, so the list doubles as the legend.
        **({"cellClass": {"function": f"params.data.{SWATCH_FIELD}"}} if swatch else {}),
    }


def status_column_def() -> dict[str, Any]:
    """How the run ended and how long it took. Sorts by how long, and filters (as text) by its status."""
    return {
        "field": DURATION_FIELD,
        "headerName": "Status",
        "headerTooltip": "How the run ended, and how long it took",
        "sortable": True,
        "filter": True,
        "filterValueGetter": {"function": f"params.data.{STATUS_FIELD}"},
        "valueFormatter": {"function": f"params.data.{STATUS_TEXT_FIELD}"},
        "tooltipField": STATUS_DETAIL_FIELD,
        # The run name gets what is left, but this has to stay readable ("✕ 1h 2m") in a narrow sidebar.
        "minWidth": STATUS_MIN_WIDTH,
        "cellStyle": {
            "styleConditions": [
                {
                    "condition": f"params.data.{STATUS_FIELD} == '{status.value}'",
                    "style": {"color": color},
                }
                for status, color in STATUS_STYLES.items()
            ]
        },
    }
