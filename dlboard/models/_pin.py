"""Pins: a flag on a run that says it matters, who flagged it, and in which colour."""

from __future__ import annotations

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dlboard._compat import StrEnum
from dlboard.models._experiment import Experiment
from dlboard.models._run import Run


class PinColor(StrEnum):
    """
    Which of a user's pins on a run this is: plain, or one of nine colours that keep teams' sets apart.

    Written to a database column as the plain string value, so a value is never renamed or removed -- see
    `docs/compatibility.md`. The values happen to be Mantine colour names, but the UI turns a value into a colour in one
    place (`pin_css_color`): if Mantine or a later UI renames a colour, that mapping changes and
    the stored value does not. So never "fix" a value to follow a palette.
    """

    PLAIN = "plain"
    RED = "red"
    ORANGE = "orange"
    YELLOW = "yellow"
    GREEN = "green"
    TEAL = "teal"
    BLUE = "blue"
    VIOLET = "violet"
    PINK = "pink"
    GRAY = "gray"


class NewRunPin(BaseModel, frozen=True, extra="forbid"):
    """A run about to be pinned."""

    run_id: int
    experiment_id: int
    """The run's experiment, which never changes for a run: what finds a project's pins without reading its runs."""

    pinned_by: int
    """The user who pinned it. One user can pin a run once in each colour."""

    color: PinColor = PinColor.PLAIN

    pinned_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))


class RunPin(NewRunPin, frozen=True, extra="forbid"):
    """A pin on a run."""

    id: int


class NamedPin(BaseModel, frozen=True, extra="forbid"):
    """A pin and the name of who pinned it, read together so that naming a pin never reads the users."""

    pin: RunPin
    username: str


class Pinner(BaseModel, frozen=True, extra="forbid"):
    """Someone who has pinned a run of a project."""

    user_id: int
    username: str


class PinnedRun(BaseModel, frozen=True, extra="forbid"):
    """A run with every pin on it, and its experiment: a row of a project's list of pinned runs."""

    run: Run
    experiment: Experiment
    pins: list[NamedPin]
    """Every pin on the run, oldest first, whether or not it matched the list's filters: the run's own colours."""


class PinnedRuns(BaseModel, frozen=True, extra="forbid"):
    """A page of a project's pinned runs, and how many runs matched in all, so a pager can say where it is."""

    total: int
    runs: list[PinnedRun]
