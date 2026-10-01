"""
Stored credentials: API tokens (for training scripts) and password hashes (for the password provider).

Only ever hashes -- a token's secret half is shown to its owner once, at creation, and never stored.
Kept out of `User` entirely, so a `User` handed to any page or callback can never carry one.
"""

from __future__ import annotations

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewApiToken(BaseModel, frozen=True, extra="forbid"):
    """An API token about to be stored."""

    user_id: int
    """Who the token authenticates as."""

    name: str
    """The owner's label for it, e.g. "cluster jobs"."""

    token_id: str
    """The public, unique half of the token, used to look it up."""

    secret_hash: str
    """SHA-256 of the secret half. The secret is random and high-entropy, so a slow hash buys nothing."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))

    expires_at: AwareDatetime | None = None
    """When the token stops working, or `None` for never."""


class ApiToken(NewApiToken, frozen=True, extra="forbid"):
    """An API token stored in the database."""

    id: int

    last_used_at: AwareDatetime | None = None
    """Roughly when the token last authenticated a request (updated at most once a minute)."""

    revoked_at: AwareDatetime | None = None
    """When the token was revoked, if at all."""


class PasswordCredential(BaseModel, frozen=True, extra="forbid"):
    """A user's password hash."""

    id: int | None = None
    user_id: int
    password_hash: str
    updated_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
