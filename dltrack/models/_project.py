"""A project."""

from pydantic import BaseModel


class NewProject(BaseModel, frozen=True, extra="forbid"):
    """A deep learning project."""

    name: str
    """The name of the project."""

    description: str
    """A description of the project."""


class Project(NewProject, frozen=True, extra="forbid"):
    """A project stored in the database."""

    id: int
    """The ID for a project if it exists in the databse, otherwise none."""
