"""A project."""

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dlboard.models._access import ProjectRole


class NewProject(BaseModel, frozen=True, extra="ignore"):
    """A deep learning project. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    name: str
    """The name of the project."""

    description: str
    """A description of the project."""

    everyone_role: ProjectRole | None = None
    """
    The role every signed-in user has on this project, on top of any explicit grant -- `None` keeps it
    to its members. Only meaningful under an identity-verifying auth provider (see
    `AuthProvider.verifies_identity`); without one, everyone can already edit everything.
    """

    created_by: int | None = None
    """The user who created this project, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this project was created."""


class Project(NewProject, frozen=True, extra="ignore"):
    """A project stored in the database. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

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
