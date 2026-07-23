"""A project."""

import typing

from pydantic import BaseModel

type ProjectID = int


class NewProject(BaseModel, frozen=True, extra="forbid"):
    """A deep learning project."""

    name: str
    """The name of the project."""

    description: str
    """A description of the project."""


class Project(NewProject, frozen=True, extra="forbid"):
    """A project stored in the database."""

    id: ProjectID | None = None
    """The ID for a project if it exists in the databse, otherwise none."""

    def with_id(self, id: ProjectID) -> typing.Self:
        return self.model_copy(update={"id": id})
