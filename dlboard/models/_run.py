"""A run of an experiment."""

import coolname
import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dlboard._compat import StrEnum


class RunStatus(StrEnum):
    """
    Where a run is, as its client last reported it.

    Written to a database column and sent over the REST API as the plain string value, so a value is
    never renamed or removed -- see `docs/compatibility.md`.
    """

    RUNNING = "running"
    """Started, and not yet reported done -- including a run whose process died without a word."""
    FINISHED = "finished"
    """The client reported it ended normally."""
    FAILED = "failed"
    """The client reported it ended with an error."""
    UNKNOWN = "unknown"
    """Ended, but its client never said how: a script that exited without Lightning finalizing the logger."""


class NewRun(BaseModel, frozen=True, extra="ignore"):
    """A run within an experiment. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    experiment_id: int
    """The experiment ID to use for this run."""

    name: str | None = Field(default_factory=lambda: coolname.generate_slug(2))
    """
    A human-readable name for this run, e.g. "uptight-yak".

    Auto-generated (a two-word `coolname` slug) whenever a run is created without one -- this is a
    `default_factory`, not a plain default, so it only fires when the field is *omitted* at
    construction, never when it's explicitly passed as `None`. That distinction is what keeps a
    run row persisted before this field existed (where the database genuinely has `NULL`, not
    "omitted") from retroactively growing a name on the next read: `None` stays `None`, and the UI
    still falls back to `Run {id}` for those.
    """

    created_by: int | None = None
    """The user who created this run, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this run was created."""

    status: RunStatus | None = RunStatus.RUNNING
    """
    Where this run is. A new run is `RUNNING`; `None` only for a run stored before status was
    tracked, which is shown as having no status rather than guessed at (like `name`).
    """


class Run(NewRun, frozen=True, extra="ignore"):
    """A run stored in the database. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    id: int
    """The ID of the run."""

    ended_at: AwareDatetime | None = None
    """
    When its client last reported it was done (`DataStore.finish_run`), if it has.

    A later stage reporting done again (Lightning finalizes after `fit` and after `test`) moves it
    later. `None` while `RUNNING`, and for a run that never reported.
    """

    deleted_by: int | None = None
    """The user who soft-deleted this run, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this run was soft-deleted, if at all. See `Project.deleted_at`."""
