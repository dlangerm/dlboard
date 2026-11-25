"""
Sign-in by a reverse proxy that has already authenticated the caller, which says who they are in headers (#800).

oauth2-proxy, Pomerium or Authelia, say, or an AWS ALB or Cloudflare Access in front of a proxy that forwards plain
headers. The proxy proves who the caller is; dlboard believes the headers that say so (the user, their email and groups) only
from the proxy itself: a peer in `DLBOARD_TRUSTED_HEADER_PROXIES`, by the connection's own address, never by an
`X-Forwarded-For` a client could write. A request from anywhere else that carries one of the headers is refused outright,
so a network that lets someone reach dlboard around the proxy shows up at once instead of letting them sign in as anyone.

An API token still works on its own, from anywhere, so a training script needs no proxy session. Signing out is the
proxy's to do: dlboard has no session of its own to end, since the headers sign the browser in again on its next request.
"""

from __future__ import annotations

from http import HTTPStatus
from ipaddress import ip_address
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, NamedTuple, Self

from flask import abort, request
from pydantic import Field, IPvAnyNetwork
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard.models import Principal
from dlboard.serve import CommaSeparated, comma_items, set_auth_provider

if TYPE_CHECKING:
    from dash import Dash

_PROXY_FIX_ORIGINAL: Final = "werkzeug.proxy_fix.orig"
"""Where Werkzeug's `ProxyFix` (`DLBOARD_TRUSTED_PROXIES`) keeps the environ values it replaced: the real peer's among them."""


class TrustedHeaderSettings(BaseSettings):
    """`DLBOARD_TRUSTED_HEADER_*`: which headers say who the caller is, and which peers may say it."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_TRUSTED_HEADER_")

    # The two it can't do without are named by their variables, which is how a startup error names them.
    issuer: Annotated[str, Field(validation_alias="DLBOARD_TRUSTED_HEADER_ISSUER", min_length=1)]
    """A stable name for the identity source (`sso.example.com`): with the user header, what a user is known by."""

    proxies: Annotated[
        CommaSeparated[list[IPvAnyNetwork]],
        Field(validation_alias="DLBOARD_TRUSTED_HEADER_PROXIES", min_length=1),
    ]
    """Comma-separated addresses or networks of the proxy: the only peers whose headers are believed."""

    user: str = "X-Forwarded-User"
    """The header with the caller's stable id, which is also their username."""

    email: str = "X-Forwarded-Email"
    """The header with their email, if the proxy sends one."""

    groups: str = "X-Forwarded-Groups"
    """The header with their comma-separated groups, if the proxy sends them."""


class _Claimed(NamedTuple):
    """What the identity headers of a request say, each empty if its header isn't there."""

    user: str
    email: str
    groups: frozenset[str]

    @classmethod
    def read(cls, settings: TrustedHeaderSettings) -> Self:
        def header(name: str) -> str:
            return request.headers.get(name, "").strip()

        return cls(
            header(settings.user), header(settings.email), frozenset(comma_items(header(settings.groups)))
        )


class TrustedHeaderAuthProvider:
    """Believes the identity headers a trusted proxy sets, from that proxy alone."""

    display_name: ClassVar[str] = "Single sign-on"
    verifies_identity: ClassVar[bool] = True

    def __init__(self, settings: TrustedHeaderSettings) -> None:
        """Believe the headers `settings` names, from the peers it names."""
        self._settings = settings

    @property
    def manage_url(self) -> str | None:
        """Nothing to manage here: the identity provider behind the proxy owns sign-in."""
        return None

    @property
    def admin_url(self) -> str | None:
        """Users come from the identity provider, not from dlboard's admin page."""
        return None

    def authenticate(self) -> Principal | None:
        """The caller the headers name, from the proxy; `None` with no user header; a 403 from anyone but the proxy."""
        settings = self._settings
        claimed = _Claimed.read(settings)
        if not any(claimed):
            return None
        connection: dict[str, Any] = request.environ.get(_PROXY_FIX_ORIGINAL, request.environ)
        peer = ip_address(str(connection["REMOTE_ADDR"]))
        if not any(peer in network for network in settings.proxies):
            abort(
                HTTPStatus.FORBIDDEN, f"{settings.user} is only believed from DLBOARD_TRUSTED_HEADER_PROXIES"
            )
        if not claimed.user:
            return None
        return Principal(
            issuer=settings.issuer,
            subject=claimed.user,
            username=claimed.user,
            email=claimed.email or None,
            groups=claimed.groups,
        )

    def login_url(self, next_path: str) -> None:
        """The proxy signs people in before a request ever gets here."""

    @classmethod
    def get_or_create(cls) -> TrustedHeaderAuthProvider:
        """Read `DLBOARD_TRUSTED_HEADER_*`: a missing issuer or proxy address fails startup, naming it."""
        return cls(TrustedHeaderSettings())


def plug(app: Dash) -> None:
    """Plugin content."""
    set_auth_provider(app, TrustedHeaderAuthProvider.get_or_create())
