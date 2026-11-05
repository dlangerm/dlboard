"""
Keeping runs from a run list: the bar's Keep and Release, and the lock after a kept run's name, patched in place.

Keeping takes a run out of retention's reach, so it is an editor's; releasing it, an owner's (`AuthorizingDataStore`). Its
answer is the runs' rows with their lock on or off (`_rows.RowUpdates`), as pinning's is, so that it never draws the list
again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from dlboard.serve import Icon, get_current_user, get_data_store
from dlboard.serve._pages._run_list._bar import simple_action
from dlboard.serve._pages._run_list._columns import KEPT_FIELD
from dlboard.serve._pages._run_list._model import RunAction
from dlboard.serve._pages._run_list._rows import RowUpdate, RowUpdates

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.serve._pages._run_list._ids import RunListSurface
    from dlboard.serve._pages._run_list._model import RunActionRequest


class Kept(NamedTuple):
    """What keeping or releasing some runs changed: their rows, and a notice that says so (or why not)."""

    rows: dict[str, Any] | None
    notice: list[dict[str, Any]]


def _notice(title: str, message: str, *, color: str) -> list[dict[str, Any]]:
    return [
        {
            "action": "show",
            "id": title,
            "title": title,
            "message": message,
            "color": color,
            "autoClose": 4_000,
        }
    ]


def keep_or_release(request: RunActionRequest, grid: str) -> Kept:
    """Keep the requested runs, or release them, as the viewer; refused (with a notice) if their role does not allow it."""
    store, me = get_data_store(), get_current_user()
    count = len(request.run_ids)
    runs = f"{count} run{'s' if count != 1 else ''}"
    match request.action:
        case RunAction.KEEP:
            act, kept, done = store.keep_runs, True, (f"Kept {runs}", "Retention never deletes a kept run.")
            refused = "Keeping a run is an editor's to do."
        case RunAction.RELEASE:
            act, kept, done = (
                store.release_runs,
                False,
                (f"Released {runs}", "Retention may delete them again."),
            )
            refused = "Only an owner of the project releases a kept run."
        case (
            RunAction.DRAW
            | RunAction.HIDE
            | RunAction.DIFF
            | RunAction.DELETE
            | RunAction.PIN
            | RunAction.UNPIN
            | RunAction.REMOVE
            | RunAction.COMPARE_NEW
            | RunAction.COMPARE_ADD
            | RunAction.COMPARE_PINS
        ):
            msg = f"{request.action} is not keeping a run"
            raise ValueError(msg)
    try:
        act(request.run_ids, me)
    except PermissionError:
        return Kept(None, _notice("Not yours to change", refused, color="red"))
    rows = RowUpdates(
        grid=grid, rows=[RowUpdate(run_id=run, values={KEPT_FIELD: kept}) for run in sorted(request.run_ids)]
    )
    return Kept(rows.dump(), _notice(*done, color="gray"))


def keep_actions(surface: RunListSurface) -> list[Component]:
    """The bar's Keep and Release buttons for `surface`'s list."""
    return [
        simple_action(surface, RunAction.KEEP, "Keep", icon_name=Icon.KEPT),
        simple_action(surface, RunAction.RELEASE, "Release", icon_name=Icon.RELEASED),
    ]
