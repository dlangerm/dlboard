"""
A dltrack user, and the `Principal` an `AuthProvider` resolves a caller to before it becomes one.

A user is keyed by `(issuer, subject)` -- who vouched for it, and that issuer's stable id for it --
never by `username` alone: two identity providers (or one IdP reassigning a display name) can
both present the same username, and keying on it would let one account silently take over the
other's projects. `username` stays unique too, as the human-facing handle a project member is
added by and a note names its author with.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dltrack.models._scopes import Scope

if TYPE_CHECKING:
    from typing_extensions import Self

UNVERIFIED_ISSUER: Final = "unverified"
"""The issuer of an identity nobody proved -- the anonymous provider's (see `Principal.unverified`)."""


class Principal(BaseModel, frozen=True, extra="forbid"):
    """Who an `AuthProvider` says the in-flight caller is -- not yet a stored `User`."""

    issuer: str
    """Who vouched for this identity: `UNVERIFIED_ISSUER`, `"password"`, an OIDC issuer URL, ..."""

    subject: str
    """The issuer's stable id for this identity (an OIDC `sub`), unchanged across renames."""

    username: str
    """The human-facing handle a brand new user is created with."""

    email: str | None = None
    """The issuer's email for this identity, if it has one."""

    groups: frozenset[str] = frozenset()
    """The issuer's groups for this identity (an IdP's `groups` claim), matched against group grants."""

    @classmethod
    def unverified(cls, username: str) -> Self:
        """An identity resolved best-effort, with nothing proving it -- attribution, not authentication."""
        return cls(issuer=UNVERIFIED_ISSUER, subject=username, username=username)


class NewUser(BaseModel, frozen=True, extra="forbid"):
    """A user about to be created."""

    username: str
    """Human-facing handle, unique across every issuer."""

    issuer: str
    """See `Principal.issuer`."""

    subject: str
    """See `Principal.subject`. Unique together with `issuer` -- what a user is actually looked up by."""

    email: str | None = None
    """The issuer's email for this user, refreshed on every sign-in."""

    groups: list[str] = []
    """The issuer's groups for this user as of their last sign-in. See `Principal.groups`."""

    scopes: list[Scope] = []
    """Granted site-wide permissions. Empty by default: showing up under a new identity grants nothing."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this user was first seen."""

    session_epoch: int = Field(default_factory=lambda: secrets.randbits(31))
    """
    A session is only valid while its epoch matches this one, so bumping it signs the user out everywhere.

    It starts random, not at 0: a session cookie names a user id, and a recreated database hands the same
    ids out again -- a fixed starting epoch would let an old cookie sign in as whoever now holds its id.
    """

    @classmethod
    def from_principal(cls, principal: Principal) -> Self:
        """The user a never-before-seen `principal` is created as."""
        return cls(
            username=principal.username,
            issuer=principal.issuer,
            subject=principal.subject,
            email=principal.email,
            groups=sorted(principal.groups),
        )


class User(NewUser, frozen=True, extra="forbid"):
    """A user stored in the database."""

    id: int
    """The ID of the user."""

    disabled_at: AwareDatetime | None = None
    """When an admin disabled this user, if at all. A disabled user can't authenticate by any means."""
