"""
The default, zero-friction auth provider for a local single-user deployment.

Registers no routes and turns nobody away: every request is attributed to a best-effort resolved
identity, and since none of it is proven (`verifies_identity = False`), project access control stays
off -- everyone can see and edit everything, like a local single-user tool. A deployment that wants
real sign-in swaps this plugin out for a verifying `AuthProvider` (e.g. `password`); nothing else
in the app needs to change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from flask import has_request_context, request
from structlog.stdlib import get_logger

from dltrack._identity import resolve_username
from dltrack.models import Principal
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

    The header covers the client's REST calls; the server's own best-effort environment/OS-login
    chain covers the browser, which never sends one.
    """

    display_name: ClassVar[str] = "Anonymous"
    verifies_identity: ClassVar[bool] = False
    manage_url: ClassVar[str | None] = None
    admin_url: ClassVar[str | None] = None

    def authenticate(self) -> Principal:
        """Resolve identity from the request header, falling back to the server's own environment."""
        header_value = (
            (request.headers.get(DLTRACK_USER_HEADER) or "").strip() if has_request_context() else ""
        )
        return Principal.unverified(header_value or resolve_username())

    def login_url(self, next_path: str) -> None:
        """Nobody ever signs in -- `authenticate` always resolves someone."""

    @classmethod
    def get_or_create(cls) -> AnonymousAuthProvider:
        """Initialize the provider. Stateless, so this is just construction."""
        return cls()


def plug(app: Dash) -> None:
    """Plugin content."""
    set_auth_provider(app, AnonymousAuthProvider.get_or_create())
