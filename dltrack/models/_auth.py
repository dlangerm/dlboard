"""
Defines the protocol for pluggable authentication providers.

An `AuthProvider` answers how a *person* proves who they are. The rest is core, identical under
every provider: API tokens (how a training script proves it), sessions (how a browser stays signed
in once a provider has vouched for it), and authorization (what a signed-in user may see or do).
A provider registers whatever routes it needs -- a login form, an OIDC callback -- from its own
`PluginProtocol.plug()`, via `dltrack.serve.add_public_route` for anything a signed-out browser must reach.
"""

from __future__ import annotations

import typing

# Pre-3.12 `ParamSpec` style (the client's floor is 3.10) -- `_P` is `get_or_create`'s own
# constructor signature.
_P = typing.ParamSpec("_P")

if typing.TYPE_CHECKING:
    from typing_extensions import Self

    from dltrack.models._user import Principal


class AuthProvider(typing.Protocol[_P]):
    """Resolves who is making the current request."""

    display_name: typing.ClassVar[str]
    """How the app names this sign-in mechanism to a person, e.g. "Anonymous" or "Password"."""

    verifies_identity: typing.ClassVar[bool]
    """
    Whether this provider actually proves who a caller is.

    `False` (the anonymous provider) means any caller can claim any name, so per-user access control
    would only get in the way: every user can see and edit every project, exactly like a local
    single-user tool. `True` turns on project grants, sessions, and config-driven admins (see
    `AuthSettings`), and makes a missing `DLTRACK_SECRET_KEY` a startup error.
    """

    @property
    def manage_url(self) -> str | None:
        """Where a signed-in person manages how they sign in (e.g. changes their password), or `None` if there's nothing to manage."""
        ...

    @property
    def admin_url(self) -> str | None:
        """Where an admin adds users or resets their credentials, or `None` if users come from somewhere else (an external IdP)."""
        ...

    @classmethod
    def get_or_create(cls, *args: _P.args, **kwargs: _P.kwargs) -> Self:
        """Initialize an auth provider."""
        ...

    def authenticate(self) -> Principal | None:
        """
        Who the in-flight request is, from whatever this provider itself reads off it -- or `None`.

        Only consulted once core has found neither an API token nor a signed-in session on the
        request, so a provider whose sign-in is a login form (which starts a session via
        `dltrack.serve.start_session`) simply returns `None` here. A provider that vouches for every
        request on its own -- a header a trusted reverse proxy sets, say -- answers it here instead.
        """
        ...

    def login_url(self, next_path: str) -> str | None:
        """Where to send a signed-out browser (returning it to `next_path` after), or `None` if nobody ever signs in."""
        ...
