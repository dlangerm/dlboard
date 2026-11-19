"""
Marking runs from a run list: Keep and Release, and a tag box with Tag and Untag; the lock and the Tags column patched in place.

Keeping takes a run out of retention's reach, so it is an editor's; releasing it, an owner's. A tag is the project's, shared by
everyone in it, so putting one on or taking one off is an editor's (`AuthorizingDataStore`). The answer is the runs' rows with
their marks as they are now (`_rows.RowUpdates`), as pinning's is, so that it never draws the list again.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc

from dlboard.serve import Icon, get_current_user, get_data_store
from dlboard.serve._pages._dash_helpers import NoticeColor, notice
from dlboard.serve._pages._run_list._bar import TAG_PICKER_LIMIT, simple_action
from dlboard.serve._pages._run_list._columns import KEPT_FIELD, TAGS_FIELD
from dlboard.serve._pages._run_list._ids import tag_picker
from dlboard.serve._pages._run_list._model import RunAction
from dlboard.serve._pages._run_list._rows import Patched, RowUpdate, RowUpdates

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash.development.base_component import Component

    from dlboard.models import DataStore
    from dlboard.serve._pages._run_list._ids import RunListSurface
    from dlboard.serve._pages._run_list._model import RunActionRequest


def mark_runs(request: RunActionRequest, grid: str) -> Patched:
    """Keep, release, tag or untag the requested runs as the viewer; refused (with a notice) if their role does not allow it."""
    store, me = get_data_store(), get_current_user()
    count = len(request.run_ids)
    runs = f"{count} run{'s' if count != 1 else ''}"
    tag = frozenset({request.tag} if request.tag is not None else ())
    act: Callable[[], None]
    values: Callable[[], dict[int, dict[str, Any]]]
    match request.action:
        case RunAction.KEEP | RunAction.RELEASE:
            kept = request.action is RunAction.KEEP
            act = partial(store.keep_runs if kept else store.release_runs, request.run_ids, me)
            done = (
                (f"Kept {runs}", "Retention never deletes a kept run.")
                if kept
                else (f"Released {runs}", "Retention may delete them again.")
            )
            refused = (
                "Keeping a run is an editor's to do."
                if kept
                else "Only an owner of the project releases a kept run."
            )

            def values() -> dict[int, dict[str, Any]]:
                return {run: {KEPT_FIELD: kept} for run in request.run_ids}

        case RunAction.TAG | RunAction.UNTAG:
            if request.action is RunAction.TAG:
                act = partial(store.tag_runs, request.run_ids, tag, me)
                done = (f"Tagged {runs} {request.tag!r}", "Shared with everyone in the project.")
            else:
                act = partial(store.untag_runs, request.run_ids, tag)
                done = (f"Took {request.tag!r} off {runs}", "Shared with everyone in the project.")
            refused = "Tagging a run is an editor's to do."

            def values() -> dict[int, dict[str, Any]]:
                tags = store.run_tags(request.run_ids)
                return {run: {TAGS_FIELD: tags.get(run, [])} for run in request.run_ids}

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
            msg = f"{request.action} is not marking a run"
            raise ValueError(msg)
    try:
        act()
    except PermissionError:
        return Patched(None, notice("Not yours to change", refused, color=NoticeColor.RED))
    rows = RowUpdates(
        grid=grid, rows=[RowUpdate(run_id=run, values=v) for run, v in sorted(values().items())]
    )
    return Patched(rows.dump(), notice(*done, color=NoticeColor.GRAY))


def mark_actions(surface: RunListSurface, store: DataStore[...], project_id: int) -> list[Component]:
    """
    The bar's Keep and Release for `surface`'s list, and its tag box with Tag and Untag, which put what is typed on or off.

    The box offers the project's first tags, and searches the rest on the server as someone types (`register_run_list`).
    """
    return [
        simple_action(surface, RunAction.KEEP, "Keep", icon_name=Icon.KEPT),
        simple_action(surface, RunAction.RELEASE, "Release", icon_name=Icon.RELEASED),
        dmc.Autocomplete(
            id=tag_picker(surface).of(project_id),
            data=store.search_tags(project_id, limit=TAG_PICKER_LIMIT),
            limit=TAG_PICKER_LIMIT,
            placeholder="tag",
            size="xs",
            w=112,
            # The bar is drawn again whenever the list changes under it (someone else tagged a run): what is typed stays.
            persistence=True,
            persistence_type="memory",
        ),
        simple_action(surface, RunAction.TAG, "Tag", icon_name=Icon.TAG),
        simple_action(surface, RunAction.UNTAG, "Untag", icon_name=Icon.CLOSE),
    ]
