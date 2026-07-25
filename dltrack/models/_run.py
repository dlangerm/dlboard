"""A run of an experiment."""

from pydantic import BaseModel


class NewRun(BaseModel, frozen=True, extra="forbid"):
    """A run within an experiment."""

    experiment_id: int
    """The experiment ID to use for this run."""


class Run(NewRun, frozen=True, extra="forbid"):
    """A run stored in the database."""

    id: int
    """The ID of the run."""
