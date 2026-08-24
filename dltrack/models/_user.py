"""
A dltrack user -- used for attribution and (eventually) fine-grained permissions.

This is an identity model, not an authentication model: usernames are resolved best-effort from
the environment (see `dltrack._identity`) and are never proven. `scopes` exists so permissions can
grow past a single admin/non-admin bit without another migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

if TYPE_CHECKING:
    from dltrack.models._scopes import Scope


class NewUser(BaseModel, frozen=True, extra="forbid"):
    """A user about to be created."""

    username: str
    """Human-chosen identifier, resolved best-effort -- never authenticated."""

    scopes: list[Scope] = []
    """Granted permissions. Empty by default: showing up under a new username grants nothing."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this user was first seen."""


class User(NewUser, frozen=True, extra="forbid"):
    """A user stored in the database."""

    id: int
    """The ID of the user."""
