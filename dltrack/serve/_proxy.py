"""Trusting a reverse proxy's `X-Forwarded-*` headers -- opt-in via `DLTRACK_TRUSTED_PROXIES`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import NonNegativeInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from werkzeug.middleware.proxy_fix import ProxyFix

if TYPE_CHECKING:
    from dash import Dash


class ProxySettings(BaseSettings):
    """`DLTRACK_*` environment variables for running behind a reverse proxy."""

    model_config = SettingsConfigDict(env_prefix="DLTRACK_")

    trusted_proxies: NonNegativeInt = 0
    """
    How many reverse-proxy hops in front of this server to trust `X-Forwarded-*` headers from.

    `0` (the default) ignores them entirely -- every request looks like it came straight from
    whatever connected over TCP, which is correct with no proxy in front. Behind one, this needs
    to be at least `1`: without it, Flask sees the proxy's own plain-HTTP connection as the
    request's scheme, which both reports every client as talking HTTP and breaks `DLTRACK_SECURE_COOKIES`
    sessions (a cookie marked `Secure` is never sent back over what Flask believes is plain HTTP).
    Each hop beyond this count is untrusted -- only the proxy closest to this server should ever
    set these headers itself; anything further out must be appending, not replacing, them.
    """


def apply_proxy_fix(app: Dash) -> None:
    """Wrap `app`'s WSGI app in `ProxyFix` if `DLTRACK_TRUSTED_PROXIES` says to trust any hops."""
    count = ProxySettings().trusted_proxies
    if not count:
        return
    app.server.wsgi_app = ProxyFix(
        app.server.wsgi_app, x_for=count, x_proto=count, x_host=count, x_port=count
    )
