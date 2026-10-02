"""A run of an experiment."""

import coolname
import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewRun(BaseModel, frozen=True, extra="forbid"):
    """A run within an experiment."""

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


class Run(NewRun, frozen=True, extra="forbid"):
    """A run stored in the database."""

    id: int
    """The ID of the run."""

    deleted_by: int | None = None
    """The user who soft-deleted this run, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this run was soft-deleted, if at all. See `Project.deleted_at`."""
