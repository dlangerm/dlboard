"""
Authentication: resolving every request to a `User` before anything else sees it.

One `before_request` gate (`install_request_gate`) covers every route on the app -- pages, Dash
callbacks (ordinary Flask POSTs), REST, artifact downloads -- so no route can forget to check. It
tries, in order: an API token (`Authorization: Bearer dlt_...`), a signed-in session, then the
`AuthProvider` itself. Whoever that resolves to is bound to the request, and `get_current_user()`
reads it back; a request it can't resolve never reaches its route at all.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from typing import TYPE_CHECKING, Annotated, Any, Final
from urllib.parse import urlsplit, urlunsplit

from flask import g, has_request_context, jsonify, redirect, request, session
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve._backend import _api_tokens
from dltrack.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash
    from werkzeug.wrappers import Response as BaseResponse

    from dltrack.models import AuthProvider, DataStore, User

_log = get_logger(__name__)

AUTH_PROVIDER: AppSlot[AuthProvider[...]] = AppSlot("auth provider")
PUBLIC_ENDPOINTS: AppSlot[set[str]] = AppSlot("public endpoints")
AUTH_SETTINGS: AppSlot[AuthSettings] = AppSlot("auth settings")

set_auth_provider = AUTH_PROVIDER.set
get_auth_provider = AUTH_PROVIDER.get
get_auth_settings = AUTH_SETTINGS.get

_USER_KEY: Final = "dltrack_user"
"""Where the request's resolved `User` lives on `flask.g`."""


class UnauthenticatedRequestError(RuntimeError):
    """`get_current_user()` ran outside a request, or on one that never went through the request gate."""


class _Mime(StrEnum):
    JSON = "application/json"
    HTML = "text/html"


SIGN_OUT_PATH: Final = "/sign-out"
"""Ends the browser's session, then returns it to the app (and so, signed out, to the provider's login)."""

_SESSION_USER_ID: Final = "uid"
_SESSION_EPOCH: Final = "epoch"


class AuthSettings(BaseSettings):
    """
    Deployment-wide auth settings, from `DLTRACK_*` env vars.

    Everything here only matters under an identity-verifying provider (see
    `AuthProvider.verifies_identity`) -- `dltrack serve local` needs none of it.
    """

    model_config = SettingsConfigDict(env_prefix="DLTRACK_")

    secret_key: SecretStr | None = None
    """Signs session cookies. Required under a verifying provider, and must match across every worker process."""

    session_lifetime_hours: int = 24 * 14
    """How long a browser stays signed in."""

    secure_cookies: bool = True
    """Only send the session cookie over HTTPS. Turn off only for plain-HTTP local testing."""

    admin_users: Annotated[list[str], NoDecode] = []
    """Comma-separated usernames granted `Scope.ALL` whenever they sign in -- how the first admin is made."""

    admin_groups: Annotated[list[str], NoDecode] = []
    """Comma-separated IdP groups whose members are granted `Scope.ALL` whenever they sign in."""

    @field_validator("admin_users", "admin_groups", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:  # noqa: ANN401 -- a `mode="before"` validator sees the raw input
        return [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value


def add_public_route(app: Dash, rule: str, view_func: Callable[..., Any], methods: list[str]) -> None:
    """
    Register a route a signed-out browser must be able to reach (a login form, an OIDC callback).

    Every other route on the app is behind the request gate. `rule` is prefix-relative, like
    every other plugin route (see `basic_rest_backend.plug`).
    """
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    endpoint = prefix + rule
    app.server.add_url_rule(endpoint, endpoint=endpoint, view_func=view_func, methods=methods)
    public = PUBLIC_ENDPOINTS.find(app)
    if public is None:
        public = set[str]()
        PUBLIC_ENDPOINTS.set(app, public)
    public.add(endpoint)


def safe_next_path(next_path: str | None) -> str:
    """
    `next_path` reduced to a same-site path and query, else `/` -- so a login form's `?next=` can't redirect off-site.

    Rebuilt from the parsed path and query alone, so nothing it carried (scheme, host, fragment) survives.
    Browsers read a backslash as a slash, so a path that opens with `/` and a backslash is a protocol-relative URL in disguise.
    """
    target = urlsplit(next_path or "")
    if target.scheme or target.netloc or "\\" in target.path or not target.path.startswith("/"):
        return "/"
    return urlunsplit(("", "", target.path, target.query, ""))


def bind_current_user(user: User) -> None:
    """Make `user` who `get_current_user()` resolves to for the rest of the in-flight request."""
    setattr(g, _USER_KEY, user)


def find_current_user() -> User | None:
    """The user the in-flight request was authenticated as, or `None` outside one (or on a public route)."""
    return g.get(_USER_KEY) if has_request_context() else None


def get_current_user() -> User:
    """The user the in-flight request was authenticated as (see `install_request_gate`)."""
    user = find_current_user()
    if user is None:
        msg = "No authenticated user for this request -- is it running behind the request gate?"
        raise UnauthenticatedRequestError(msg)
    return user


def sign_in(store: DataStore[...], principal: models.Principal, settings: AuthSettings) -> User | None:
    """
    The user `principal` is (created on first sign-in), or `None` if they've been disabled.

    Grants `Scope.ALL` to anyone `settings.admin_users`/`admin_groups` names. Only ever adds it --
    revoking an admin is done deliberately, from the admin page.
    """
    user = store.get_or_create_user(principal)
    is_configured_admin = user.username in settings.admin_users or bool(
        principal.groups & set(settings.admin_groups)
    )
    if is_configured_admin and models.Scope.ALL not in user.scopes:
        _log.info("Granting configured admin scopes to user %s (%s)", user.id, user.username)
        user = store.update_user(user.model_copy(update={"scopes": [*user.scopes, models.Scope.ALL]}))
    return None if user.disabled_at is not None else user


def start_session(user: User) -> None:
    """Sign the browser making this request in as `user`, for `AuthSettings.session_lifetime_hours`."""
    session.clear()
    session.permanent = True
    session[_SESSION_USER_ID] = user.id
    session[_SESSION_EPOCH] = user.session_epoch


def end_session() -> None:
    """Sign the browser making this request out."""
    session.clear()


def _session_user(store: DataStore[...]) -> User | None:
    user_id = session.get(_SESSION_USER_ID)
    if user_id is None:
        return None
    user = store.get_user(user_id)
    if user is None or user.disabled_at is not None or user.session_epoch != session.get(_SESSION_EPOCH):
        return None
    return user


def _authenticate(store: DataStore[...], provider: AuthProvider[...], settings: AuthSettings) -> User | None:
    auth = request.authorization
    if auth is not None and auth.type == "bearer" and _api_tokens.is_api_token(auth.token or ""):
        # A well-formed dltrack token that doesn't check out is a hard failure -- never quietly fall back to
        # some other identity for a caller that plainly meant to be this one.
        return _api_tokens.user_for_token(store, auth.token or "")
    if provider.verifies_identity and (user := _session_user(store)) is not None:
        return user
    principal = provider.authenticate()
    return sign_in(store, principal, settings) if principal is not None else None


def _challenge(provider: AuthProvider[...]) -> BaseResponse | tuple[BaseResponse, int]:
    """Send a signed-out browser to sign in; tell anything else (a callback, a script) it's a 401."""
    login = provider.login_url(request.full_path.rstrip("?"))
    # A browser *prefers* HTML; a script's `Accept: */*` merely tolerates it, and must get a plain 401
    # rather than be redirected to a login form it would happily follow to a 200.
    prefers_html = request.accept_mimetypes.best_match([_Mime.JSON, _Mime.HTML]) == _Mime.HTML
    if login is not None and request.method == HTTPMethod.GET and prefers_html:
        return redirect(login)
    return jsonify(error="Authentication required"), HTTPStatus.UNAUTHORIZED


def _sign_out() -> BaseResponse:
    end_session()
    return redirect("/")


def install_request_gate(app: Dash, store_for: Callable[[], DataStore[...]]) -> None:
    """
    Put every route on `app` (except `add_public_route` ones) behind authentication.

    `store_for` returns the unwrapped data store -- resolving who a caller is necessarily happens
    before there's anyone to authorize against.
    """
    provider = get_auth_provider(app)
    settings = AuthSettings()
    AUTH_SETTINGS.set(app, settings)
    if provider.verifies_identity:
        if settings.secret_key is None:
            msg = f"The {provider.display_name} auth provider needs DLTRACK_SECRET_KEY set to sign sessions"
            raise ValueError(msg)
        app.server.secret_key = settings.secret_key.get_secret_value()
        app.server.config.update(
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SECURE=settings.secure_cookies,
            SESSION_COOKIE_SAMESITE="Lax",
            PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.session_lifetime_hours),
        )
        add_public_route(app, SIGN_OUT_PATH.lstrip("/"), _sign_out, ["GET"])

    def gate() -> BaseResponse | tuple[BaseResponse, int] | None:
        public = PUBLIC_ENDPOINTS.find(app) or set[str]()
        if request.endpoint in public:
            return None
        user = _authenticate(store_for(), provider, settings)
        if user is None:
            return _challenge(provider)
        bind_current_user(user)
        return None

    # First, ahead of Dash's own `before_request` hook: that one renders the layout on the first
    # request, which (via the header's user menu) needs the user this gate resolves.
    app.server.before_request_funcs.setdefault(None, []).insert(0, gate)
