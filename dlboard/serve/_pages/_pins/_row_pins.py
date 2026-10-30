"""
A run's pins in a run list: the Pin column, and changing a pin in place.

A run list shows each run's pins as one pin filled with its colours (`DlboardPins`), and its editor changes yours. A change is
a `RunAction.PIN` or `UNPIN` like any other action on runs. Its answer is the run's new pins, patched into the row in place
(`_rows.RowUpdates`), so that pinning never draws the list again.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any

from dlboard.models import PinColor
from dlboard.serve import get_current_user, get_data_store
from dlboard.serve._grid import PINS_RENDERER, PINS_TOOLTIP
from dlboard.serve._pages._pins._pin_view import PinCell, pin_cells, pin_css_color
from dlboard.serve._pages._run_list import RunAction, RunActionRequest
from dlboard.serve._pages._run_list._rows import RowUpdate, RowUpdates

if TYPE_CHECKING:
    from collections.abc import Collection

    from dlboard.models import DataStore

PINS_FIELD: typing.Final = "_pins"
"""The run list's row field holding a run's `PinCell`s (reserved like `_swatch`: a metric named "pins" can't shadow it)."""


def pin_column_def() -> dict[str, Any]:
    """The Pin column of a run list."""
    return {
        "field": PINS_FIELD,
        "headerName": "",
        "headerTooltip": "Pins: click a run's pin to pin it, or take your pin back",
        "sortable": False,
        # Driven by the pin chips above the list, not by a menu of its own.
        "filter": "agTextColumnFilter",
        "filterParams": {"maxNumConditions": len(PinColor)},
        "filterValueGetter": {"function": "dlboardPinColors(params)"},
        "suppressHeaderFilterButton": True,
        "width": 36,
        "maxWidth": 36,
        "suppressSizeToFit": True,
        "cellRenderer": PINS_RENDERER,
        "cellRendererParams": {"colors": [{"color": c.value, "css": pin_css_color(c)} for c in PinColor]},
        "cellClass": "dl-pins-cell",
        "tooltipField": PINS_FIELD,
        "tooltipComponent": PINS_TOOLTIP,
        # Its value is a list, which ag-grid would infer an "object" type for and warn that it has no formatter.
        "cellDataType": False,
    }


def pin_cells_by_run(store: DataStore[...], run_ids: Collection[int], me: int) -> dict[int, list[PinCell]]:
    """The colours each run of `run_ids` that is pinned is pinned in, by run: the pins of each run's experiment, read once."""
    by_experiment: dict[int, set[int]] = {}
    for run_id, experiment_id in store.run_experiments(frozenset(run_ids)).items():
        by_experiment.setdefault(experiment_id, set()).add(run_id)
    return {
        run_id: pin_cells(pins, me)
        for experiment_id, runs in by_experiment.items()
        for run_id, pins in store.pins_of(experiment_id, run_ids=frozenset(runs)).items()
    }


class Pinned(typing.NamedTuple):
    """What pinning or unpinning some runs changed: their rows, and a notice that says so."""

    rows: dict[str, Any]
    notice: list[dict[str, Any]]


def pin_or_unpin(request: RunActionRequest, grid: str) -> Pinned:
    """
    Pin the requested runs as the viewer, or take the viewer's pin back, in the colour asked.

    Only the viewer's own pin is ever touched, whatever the request names: pinning is attribution.
    """
    store, me = get_data_store(), get_current_user().id
    color = request.color
    if color is None:
        msg = f"{request.action} takes a colour"
        raise ValueError(msg)
    match request.action:
        case RunAction.PIN:
            store.pin_runs(me, request.run_ids, color)
            verb, message = "Pinned", "Everyone in the project sees who pinned a run."
        case RunAction.UNPIN:
            store.unpin_runs(me, request.run_ids, color)
            verb, message = "Unpinned", "Only your own pin is taken back."
        case (
            RunAction.DRAW
            | RunAction.HIDE
            | RunAction.DIFF
            | RunAction.DELETE
            | RunAction.REMOVE
            | RunAction.COMPARE_NEW
            | RunAction.COMPARE_ADD
            | RunAction.COMPARE_PINS
        ):
            msg = f"{request.action} is not a pin"
            raise ValueError(msg)
    cells = pin_cells_by_run(store, request.run_ids, me)
    rows = RowUpdates(
        grid=grid,
        rows=[
            RowUpdate(
                run_id=run, values={PINS_FIELD: [c.model_dump(mode="json") for c in cells.get(run, [])]}
            )
            for run in sorted(request.run_ids)
        ],
    )
    count = len(request.run_ids)
    notice = {
        "action": "show",
        "id": f"{verb}-{request.color}-{count}",
        "title": f"{verb} {count} run{'s' if count != 1 else ''}",
        "message": message,
        # The notice is in the pin's own colour: that is how it says which.
        "color": "gray" if color is PinColor.PLAIN else color.value,
        "autoClose": 3_000,
    }
    return Pinned(rows.dump(), [notice])


def experiment_pin_cells(store: DataStore[...], experiment_id: int, me: int) -> dict[int, list[PinCell]]:
    """The colours each pinned run of an experiment is pinned in, by run."""
    return {run: pin_cells(pins, me) for run, pins in store.pins_of(experiment_id, run_ids=None).items()}
