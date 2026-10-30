"""The ids of a run list, told apart by the surface it is drawn on: each page has its own request store and its own buttons."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from dlboard._compat import StrEnum
from dlboard.serve._component_ids import DivId, PatternId, StoreId

if TYPE_CHECKING:
    from dlboard.models import PinColor
    from dlboard.serve._pages._run_list._model import RunAction


class RunList:
    """Tag for the ids of a run list, which is on one page at a time."""


class RunListSurface(StrEnum):
    """Where a run list is drawn: what its rows are, and which of the actions on them it offers."""

    EXPERIMENT = "experiment"
    """The sidebar of an experiment page: the experiment's runs, drawn or not."""
    PROJECT = "project"
    """The sidebar of a project page: the runs pinned in the project, from any of its experiments."""
    COMPARISON = "comparison"
    """The sidebar of a comparison page: its member runs, from any experiments of the project, drawn or not."""


def request_store_id(surface: RunListSurface) -> StoreId[RunList]:
    """
    Where the browser writes what the run list was asked to do (a `RunActionRequest`), for the page's callback to read.

    In the page's static layout, not the run list's own tree: a server callback listening to something that mounts with
    the list is called as it mounts, though nobody asked anything.
    """
    return StoreId(f"run-list-request-{surface}")


def bar_id(surface: RunListSurface) -> DivId[RunList]:
    """The bar of actions on the selected rows, which shows once a row is selected."""
    return DivId(f"run-list-bar-{surface}")


def count_id(surface: RunListSurface) -> DivId[RunList]:
    """The bar's "N selected"."""
    return DivId(f"run-list-count-{surface}")


class ActionButton(PatternId[RunList]):
    """A family of buttons, one for each way to act on the selected rows: told apart by what they do, and what they name."""

    KEYS: ClassVar[tuple[str, ...]] = ("action", "color", "comparison_id")

    def of(
        self, action: RunAction, *, color: PinColor | None = None, comparison_id: int | None = None
    ) -> dict[str, str]:
        """The id of the button for `action`: the colour or comparison it names, or an empty string where it names none."""
        return {
            "type": self.type,
            "action": action,
            "color": "" if color is None else color.value,
            "comparison_id": "" if comparison_id is None else str(comparison_id),
        }


def action_button(surface: RunListSurface) -> ActionButton:
    return ActionButton(f"run-list-action-{surface}")


def navigate_id(surface: RunListSurface) -> StoreId[RunList]:
    """
    Where an action writes the address to go to when it is done (a comparison it started), for the browser to go to (`register_navigate`).

    In the page's static layout, like the request store.
    """
    return StoreId(f"run-list-navigate-{surface}")
