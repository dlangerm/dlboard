"""
The default, zero-friction auth provider for a local single-user deployment.

Registers no routes and gates nothing: every request is let through, and attribution falls back to
a best-effort resolved identity -- exactly the behavior dltrack had before auth providers existed.
A deployment that wants real login/session/approval flows swaps this plugin out for a different
`AuthProvider`, e.g. a future password- or SSO-backed one; nothing else in the app needs to change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from flask import has_request_context, request
from structlog.stdlib import get_logger

from dltrack._identity import resolve_username
from dltrack.serve import set_auth_provider

if TYPE_CHECKING:
    from dash import Dash

_log = get_logger(__name__)

DLTRACK_USER_HEADER: Final = "X-Dltrack-User"
"""Carries the client's best-effort identity (see `dltrack._identity.resolve_username`).

Never trusted blindly -- a missing/blank header falls back to the same best-effort resolution the
client itself falls back to. This is attribution, not authentication: nothing here proves a caller
actually is who the header claims.
"""


class AnonymousAuthProvider:
    """
    Resolves identity from the `X-Dltrack-User` header or environment, and never rejects a request.

    The header covers REST calls; the server's own best-effort environment/OS-login chain covers
    in-process Dash callbacks, which have no header to read.
    """

    display_name: ClassVar[str] = "Anonymous"

    def resolve_identity(self) -> str | None:
        """Resolve identity from the request header, falling back to the server's own environment."""
        if has_request_context():
            header_value = (request.headers.get(DLTRACK_USER_HEADER) or "").strip()
            if header_value:
                return header_value
        return resolve_username()

    @classmethod
    def get_or_create(cls) -> AnonymousAuthProvider:
        """Initialize the provider. Stateless, so this is just construction."""
        return cls()


def plug(app: Dash) -> None:
    """Plugin content."""
    set_auth_provider(app, AnonymousAuthProvider.get_or_create())
