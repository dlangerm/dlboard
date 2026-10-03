"""
Project-level access: who may see or change a project and everything under it.

Experiments, runs, artifacts, metrics, views, and notes all inherit their project's access --
there are no per-experiment grants. Site-wide powers (purge, the audit log, user management) are
`Scope`s on the `User` instead, and a `Scope.ALL` admin implicitly has `ProjectRole.OWNER` everywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from dltrack._compat import StrEnum

if TYPE_CHECKING:
    from typing_extensions import Self


class ProjectRole(StrEnum):
    """A role on one project. Ordered: each role can do everything the ones before it can."""

    VIEWER = "viewer"
    """Read everything; post notes and save personal views."""

    EDITOR = "editor"
    """Log runs/metrics/artifacts, create experiments, edit the shared layout, soft-delete within the project."""

    OWNER = "owner"
    """Manage members and `Project.everyone_role`, rename or delete the project itself."""

    def includes(self, other: ProjectRole) -> bool:
        """Whether this role grants at least everything `other` does."""
        order = list(ProjectRole)
        return order.index(self) >= order.index(other)


class NewProjectGrant(BaseModel, frozen=True, extra="forbid"):
    """A role on a project, granted to exactly one user or one IdP group."""

    project_id: int
    role: ProjectRole
    user_id: int | None = None
    """The user this grants `role` to, or `None` for a group grant."""

    group: str | None = None
    """The IdP group (see `Principal.groups`) whose members this grants `role` to, or `None` for a user grant."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))

    @model_validator(mode="after")
    def _exactly_one_grantee(self) -> Self:
        if (self.user_id is None) == (self.group is None):
            msg = "A grant names exactly one of `user_id` or `group`"
            raise ValueError(msg)
        return self


class ProjectGrant(NewProjectGrant, frozen=True, extra="forbid"):
    """A grant stored in the database."""

    id: int


class AmbiguousProjectError(LookupError):
    """More than one project the caller can write to has the requested name -- they must pick one by id."""
