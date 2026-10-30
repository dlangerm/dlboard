"""What a run list is asked to do, as the browser sends it."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, model_validator

from dlboard._compat import StrEnum
from dlboard.models import PinColor


class RunAction(StrEnum):
    """One thing to do to some runs of a list."""

    DRAW = "draw"
    """Show the runs in the charts."""
    HIDE = "hide"
    """Take the runs out of the charts, leaving them in the list."""
    DIFF = "diff"
    """Open the A/B comparison of the runs' hyperparameters and metrics."""
    DELETE = "delete"
    """Delete the runs, after asking."""
    PIN = "pin"
    """Pin the runs as the viewer, in `color`."""
    UNPIN = "unpin"
    """Take the viewer's pin of `color` back from the runs."""
    REMOVE = "remove"
    """Take the runs out of the comparison they are listed in, leaving them in their experiments."""
    COMPARE_NEW = "compare_new"
    """Start a comparison of the runs, and go to it."""
    COMPARE_ADD = "compare_add"
    """Add the runs to the comparison whose id is `comparison_id`, and go to it."""
    COMPARE_PINS = "compare_pins"
    """Start a comparison of every run of the project pinned in `color`, whoever pinned it: it names no runs."""


class RunActionRequest(BaseModel, frozen=True, extra="forbid"):
    """An action on some runs, and when it was asked: so asking for the same one twice is two requests."""

    action: RunAction
    run_ids: frozenset[int]
    color: PinColor | None = None
    """The colour of the pin an action pins, unpins or compares the pinned runs of."""
    comparison_id: int | None = None
    """The comparison `COMPARE_ADD` adds the runs to: only that action takes one."""
    at: int

    @model_validator(mode="after")
    def _takes_the_colour_or_comparison_its_action_takes(self) -> Self:
        if not self.run_ids and self.action is not RunAction.COMPARE_PINS:
            msg = f"{self.action} needs a run to act on"
            raise ValueError(msg)
        match self.action:
            case (
                RunAction.DRAW
                | RunAction.HIDE
                | RunAction.DIFF
                | RunAction.DELETE
                | RunAction.REMOVE
                | RunAction.COMPARE_NEW
            ):
                if self.color is not None or self.comparison_id is not None:
                    msg = f"{self.action} takes no colour or comparison"
                    raise ValueError(msg)
            case RunAction.PIN | RunAction.UNPIN | RunAction.COMPARE_PINS:
                if self.color is None or self.comparison_id is not None:
                    msg = f"{self.action} takes a colour and no comparison"
                    raise ValueError(msg)
            case RunAction.COMPARE_ADD:
                if self.comparison_id is None or self.color is not None:
                    msg = f"{self.action} takes a comparison and no colour"
                    raise ValueError(msg)
        return self
