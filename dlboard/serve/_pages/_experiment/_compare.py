"""
Starting a comparison from runs, wherever they are listed: the menus that offer it, and what they do.

From any run list (an experiment's, a project's pinned runs, another comparison's) the selected runs can start a comparison or
be added to one, and every run of the project pinned in a colour can be compared in one step. Each ends on the comparison
itself, so what comes after pinning some runs is looking at them together.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc

from dlboard import models
from dlboard.models import PinColor
from dlboard.serve import Icon, get_current_user, get_data_store, icon
from dlboard.serve._backend._comparison_settings import ComparisonSettings
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment._page_repository import unique_comparison_name
from dlboard.serve._pages._pins._pin_view import pin_glyph
from dlboard.serve._pages._run_list import RunAction, RunActionRequest, RunListSurface
from dlboard.serve._pages._run_list._ids import action_button
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from dash.development.base_component import Component

    from dlboard.models import ComparisonSummary, DataStore

OFFERED_COMPARISONS: typing.Final = 10
"""How many comparisons the menu offers to add runs to: the first few, the rest being a click on the project page away."""


def compare_menu(
    surface: RunListSurface,
    addable: Sequence[ComparisonSummary],
    *,
    current: int | None = None,
    here: bool = False,
) -> Component:
    """
    A menu that starts a comparison of the selected runs, or adds them to one of `addable` (not `current`, the one on screen).

    `here` (the page is an editable comparison) makes the first item add to `current` rather than start a new one: a view is
    not forked by a change to the runs it draws. Offers a bounded few of the others: a project can have as many comparisons
    as its people make.
    """
    buttons = action_button(surface)
    others = [c for c in addable if c.id != current][:OFFERED_COMPARISONS]
    return dmc.Menu(
        [
            dmc.MenuTarget(
                dmc.Button(
                    "Compare",
                    variant="light",
                    color="gray",
                    size="compact-xs",
                    leftSection=icon(Icon.COMPARE),
                    rightSection=icon(Icon.CHEVRON_DOWN),
                )
            ),
            dmc.MenuDropdown(
                [
                    dmc.MenuItem(
                        "Add to this comparison" if here else "New comparison",
                        id=buttons.of(RunAction.COMPARE_NEW),
                        leftSection=icon(Icon.ADD),
                    ),
                    *(
                        [
                            dmc.MenuDivider(),
                            dmc.MenuLabel("Add to"),
                            *(
                                dmc.MenuItem(
                                    c.name
                                    + ("" if c.owner_id == get_current_user().id else f" · {c.owner_name}"),
                                    id=buttons.of(RunAction.COMPARE_ADD, comparison_id=c.id),
                                )
                                for c in others
                            ),
                        ]
                        if others
                        else []
                    ),
                ]
            ),
        ],
        position="bottom-start",
        withinPortal=True,
    )


def compare_pins_menu(
    surface: RunListSurface, counts: Mapping[PinColor, int], *, here: bool = False
) -> Component | None:
    """
    A menu that compares every run of the project pinned in a colour: each colour a pin, with how many runs it holds.

    Needs no runs selected, since what it compares is what has been pinned. `here` adds those runs to the comparison on
    screen instead of starting one. `None` when nothing is pinned.
    """
    if not counts:
        return None
    buttons = action_button(surface)
    return dmc.Menu(
        [
            dmc.MenuTarget(
                dmc.Button(
                    "Compare pins",
                    variant="subtle",
                    color="gray",
                    size="compact-xs",
                    leftSection=icon(Icon.PIN),
                    rightSection=icon(Icon.CHEVRON_DOWN),
                )
            ),
            dmc.MenuDropdown(
                [
                    dmc.MenuItem(
                        dmc.Group([pin_glyph(color), dmc.Text(str(counts[color]), size="sm")], gap="xs"),
                        id=buttons.of(RunAction.COMPARE_PINS, color=color),
                        **typing.cast(
                            "dict[str, Any]",
                            {
                                "aria-label": f"{'Add' if here else 'Compare'} {color.value} pins"
                                + (" to this comparison" if here else "")
                            },
                        ),
                    )
                    for color in PinColor
                    if color in counts
                ]
            ),
        ],
        position="bottom-start",
        withinPortal=True,
    )


class Compared(typing.NamedTuple):
    """What an action to compare did: where to go to see it, if anywhere, and a notice if it has one to give."""

    destination: str | None
    notice: list[dict[str, Any]] | None
    changed: bool = False
    """Whether the runs of the comparison on screen changed, so its list is drawn again (`here`, no navigation)."""


def _add_here(run_ids: Collection[int], comparison_id: int) -> Compared:
    """Add `run_ids` to the comparison on screen, which stays where it is."""
    try:
        added = get_data_store().add_comparison_runs(
            comparison_id, frozenset(run_ids), added_by=get_current_user().id
        )
    except PermissionError:
        return Compared(
            None,
            _notice(
                "Not yours to change",
                "Only its owner, or an editor of a shared comparison, can add runs to it.",
                color="red",
            ),
        )
    except ValueError as refused:
        return Compared(None, _notice("Not added", str(refused), color="red"))
    if not added:
        return Compared(None, _notice("Nothing new", "Those runs are already in it.", color="yellow"))
    message = f"Added {added} run{'s' if added != 1 else ''}."
    return Compared(None, _notice("Runs added", message, color="green"), changed=True)


def _notice(title: str, message: str, *, color: str) -> list[dict[str, Any]]:
    return [
        {
            "action": "show",
            "id": f"compare-{title}",
            "title": title,
            "message": message,
            "color": color,
            "autoClose": 6_000,
        }
    ]


def _own_comparison_of(
    store: DataStore[...], project_id: int, me: int, run_ids: frozenset[int]
) -> int | None:
    """The id of a comparison of the viewer's in the project with exactly `run_ids` as its runs, if there is one."""
    for summary in store.list_comparisons(project_id, me):
        if summary.owner_id != me or summary.run_count != len(run_ids):
            continue
        if frozenset(run.id for run in store.comparison_runs(summary.id)) == run_ids:
            return summary.id
    return None


def _start(
    run_ids: Collection[int], project_id: int, layout: page_model.BasicExperimentPage | None
) -> Compared:
    """
    A new comparison of `run_ids`, named for the viewer, starting from `layout`'s panels and columns (never its runs).

    Unless the viewer already has one of exactly these runs: a click on a compare that is repeated, or made again while the
    comparison it opened is being adjusted, opens that one, rather than another copy of it.
    """
    store, me = get_data_store(), get_current_user().id
    existing = _own_comparison_of(store, project_id, me, frozenset(run_ids))
    if existing is not None:
        return Compared(
            relative_path(f"/comparison/{existing}"),
            _notice("Already compared", "Opened your comparison of these runs.", color="blue"),
        )
    try:
        started = store.create_comparison(
            page_model.BasicExperimentPage,
            models.NewPage[Any, Any](
                project_id=project_id,
                owner_id=me,
                name=unique_comparison_name(store, project_id, me),
                panels=[] if layout is None else layout.panels,
                page_settings=page_model.DocumentSettings(
                    hparam_columns=[] if layout is None else layout.document.hparam_columns
                ).merged_into({}),
            ),
            frozenset(run_ids),
        )
    except ValueError as refused:
        return Compared(None, _notice("Not started", str(refused), color="red"))
    return Compared(relative_path(f"/comparison/{started.id}"), None)


def _add(run_ids: Collection[int], comparison_id: int) -> Compared:
    """Add `run_ids` to the comparison, and go to it."""
    store = get_data_store()
    try:
        store.add_comparison_runs(comparison_id, frozenset(run_ids), added_by=get_current_user().id)
    except PermissionError:
        return Compared(
            None,
            _notice(
                "Not yours to change",
                "Only its owner, or an editor of a shared comparison, can add runs to it.",
                color="red",
            ),
        )
    except ValueError as refused:
        return Compared(None, _notice("Not added", str(refused), color="red"))
    return Compared(relative_path(f"/comparison/{comparison_id}"), None)


def compare(
    request: RunActionRequest,
    *,
    project_id: int,
    layout: page_model.BasicExperimentPage | None,
    here: int | None = None,
) -> Compared:
    """
    Do what `request` asks to compare, in the project `project_id`.

    `layout` is the page the request was made on, whose panels and columns a new comparison starts with; `None` where there
    is none (the project page). `here` is the editable comparison on screen, if the request was made on one: then what is
    compared is added to it, and no new comparison (a view with new runs in it) is made.
    """
    match request.action:
        case RunAction.COMPARE_NEW if here is not None:
            return _add_here(request.run_ids, here)
        case RunAction.COMPARE_NEW:
            return _start(request.run_ids, project_id, layout)
        case RunAction.COMPARE_ADD:
            if request.comparison_id is None:
                msg = f"{request.action} takes a comparison"
                raise ValueError(msg)
            return _add(request.run_ids, request.comparison_id)
        case RunAction.COMPARE_PINS:
            if request.color is None:
                msg = f"{request.action} takes a colour"
                raise ValueError(msg)
            pinned = get_data_store().pinned_run_ids(
                project_id,
                colors=frozenset({request.color}),
                limit=ComparisonSettings().max_comparison_runs + 1,
            )
            if here is not None:
                return _add_here(pinned, here)
            return _start(pinned, project_id, layout)
        case (
            RunAction.DRAW
            | RunAction.HIDE
            | RunAction.DIFF
            | RunAction.DELETE
            | RunAction.PIN
            | RunAction.UNPIN
            | RunAction.REMOVE
        ):
            msg = f"{request.action} is not a comparison"
            raise ValueError(msg)
