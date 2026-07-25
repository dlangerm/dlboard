"""Data models for rendering an experiment view."""

from pydantic import BaseModel


class ExperimentView(BaseModel, frozen=True, extra="forbid"):
    """A view of an experiment."""

    id: int
    """The ID of the experiment."""


class ProjectView(BaseModel, frozen=True, extra="forbid"):
    """A view of a project."""

    id: int
    """The ID of the project."""


class RunView(BaseModel, frozen=True, extra="forbid"):
    """A view of a run."""

    id: int
    """The ID of the run."""
