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


class RunActionRequest(BaseModel, frozen=True, extra="forbid"):
    """An action on some runs, and when it was asked: so asking for the same one twice is two requests."""

    action: RunAction
    run_ids: frozenset[int]
    color: PinColor | None = None
    """The colour an action pins, unpins or compares the pinned runs of: none of the actions here takes one yet."""
    comparison_id: int | None = None
    """The comparison an action adds the runs to: none of the actions here takes one yet."""
    at: int

    @model_validator(mode="after")
    def _takes_the_colour_or_comparison_its_action_takes(self) -> Self:
        match self.action:
            case RunAction.DRAW | RunAction.HIDE | RunAction.DIFF | RunAction.DELETE:
                if self.color is not None or self.comparison_id is not None:
                    msg = f"{self.action} takes no colour or comparison"
                    raise ValueError(msg)
        return self
