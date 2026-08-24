"""A project."""

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewProject(BaseModel, frozen=True, extra="forbid"):
    """A deep learning project."""

    name: str
    """The name of the project."""

    description: str
    """A description of the project."""

    created_by: int | None = None
    """The user who created this project, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this project was created."""


class Project(NewProject, frozen=True, extra="forbid"):
    """A project stored in the database."""

    id: int
    """The ID for a project if it exists in the databse, otherwise none."""

    deleted_by: int | None = None
    """The user who soft-deleted this project, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """
    When this project was soft-deleted, if at all.

    Deliberately absent from `NewProject`: a project can never be created already-deleted, only
    transitioned into that state via `DataStore.delete_project`.
    """
