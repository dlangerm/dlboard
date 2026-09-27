"""
Defines the protocol for pluggable authentication providers.

An `AuthProvider` only ever answers one question -- `resolve_identity()`, "who is making this
request/callback" -- so every other piece of generic app code (REST handlers, Dash callbacks) can
go through a single `dltrack.serve.get_current_user` instead of each re-implementing its own
best-effort identity resolution. Everything else -- login pages, session cookies, signup/approval
queues, SSO redirects -- is deliberately left out of this protocol: a provider registers whatever
routes and request gating it needs via its own `PluginProtocol.plug()`, exactly like a `DataStore`
plugin registers its own migrations. This keeps the core app free of opinions about *how* a caller
proves who they are.
"""

from __future__ import annotations

import typing


class AuthProvider[**P](typing.Protocol):
    """Resolves who is making the current request/callback."""

    display_name: typing.ClassVar[str]
    """How the app names this sign-in mechanism to a person, e.g. "Anonymous" or "Google SSO"."""

    @classmethod
    def get_or_create(cls, *args: P.args, **kwargs: P.kwargs) -> typing.Self:
        """Initialize an auth provider."""
        ...

    def resolve_identity(self) -> str | None:
        """The authenticated caller's username for the in-flight request/callback, or `None` if there isn't one."""
        ...
