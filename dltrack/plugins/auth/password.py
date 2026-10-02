"""
Username/password sign-in, for hosting dltrack for other people without an external identity provider.

Swap it in for `anonymous` (see `dltrack.plugins.PASSWORD_AUTH`). It needs `DLTRACK_SECRET_KEY`,
like every identity-verifying provider, and is configured by `DLTRACK_PASSWORD_*` env vars (see
`PasswordSettings`). Its pages are plain server-rendered forms rather than Dash pages, so nothing
of the app itself is served to someone who hasn't signed in.

The first admin is made with `dltrack users set-password <name> --admin`, or by naming them in
`DLTRACK_ADMIN_USERS` before they sign up. Passwords are hashed with scrypt; failed sign-ins are
throttled per username, per server process -- put a rate-limiting reverse proxy in front of any
deployment exposed to the internet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, NamedTuple
from urllib.parse import quote

import pendulum
from flask import abort, redirect, render_template_string, request, session
from pydantic_settings import BaseSettings, SettingsConfigDict

from dltrack import models
from dltrack.serve import (
    AppSlot,
    add_public_route,
    get_auth_settings,
    get_current_user,
    get_system_data_store,
    safe_next_path,
    set_auth_provider,
    sign_in,
    start_session,
)

if TYPE_CHECKING:
    from dash import Dash
    from werkzeug.wrappers import Response

    from dltrack.models import DataStore, User

PASSWORD_ISSUER: Final = "password"


class SignupPolicy(StrEnum):
    """Who may create an account."""

    ADMIN_CREATES = "admin_creates"
    """Nobody signs up: an admin creates every account (the admin page, or `dltrack users set-password`)."""

    APPROVAL = "approval"
    """Anyone can sign up, but the account stays disabled until an admin enables it."""

    OPEN = "open"
    """Anyone can sign up and start using the server straight away."""


class PasswordSettings(BaseSettings):
    """The password provider's settings, from `DLTRACK_PASSWORD_*` env vars."""

    model_config = SettingsConfigDict(env_prefix="DLTRACK_PASSWORD_")

    signup: SignupPolicy = SignupPolicy.ADMIN_CREATES
    min_length: int = 12


# -- Hashing --------------------------------------------------------------------------------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    """A salted scrypt hash of `password`, self-describing so its parameters can change later."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    encode = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encode(salt).decode()}${encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    """Whether `password` matches `encoded` (a `hash_password` result)."""
    _scheme, n, r, p, salt, digest = encoded.split("$")
    salt_bytes, expected = base64.b64decode(salt), base64.b64decode(digest)
    actual = hashlib.scrypt(
        password.encode(), salt=salt_bytes, n=int(n), r=int(r), p=int(p), dklen=len(expected)
    )
    return hmac.compare_digest(actual, expected)


_DECOY_HASH: Final = hash_password(secrets.token_urlsafe())
"""Checked against when a username doesn't exist, so a miss takes as long as a wrong password.

Hashed once at import: computing it lazily would make the first miss pay for two hashes, which is itself
a timing tell."""


def set_password(store: DataStore[...], user: User, password: str) -> User:
    """Set `user`'s password, signing them out of every existing session."""
    store.set_password_credential(
        models.PasswordCredential(user_id=user.id, password_hash=hash_password(password))
    )
    return store.update_user(user.model_copy(update={"session_epoch": user.session_epoch + 1}))


def create_or_reset_user(store: DataStore[...], username: str, password: str, *, admin: bool = False) -> User:
    """Create a password user (or reset an existing one's password), optionally making them an admin."""
    user = store.get_or_create_user(_principal(username))
    if admin and models.Scope.ALL not in user.scopes:
        user = store.update_user(user.model_copy(update={"scopes": [*user.scopes, models.Scope.ALL]}))
    return set_password(store, user, password)


def _principal(username: str) -> models.Principal:
    return models.Principal(issuer=PASSWORD_ISSUER, subject=username, username=username)


# -- Throttling -----------------------------------------------------------------------------------


class _Throttle:
    """Refuses a username's sign-ins for a while after too many recent failures."""

    max_failures: Final = 5
    window_seconds: Final = 15 * 60

    def __init__(self) -> None:
        self._failures: defaultdict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _recent(self, username: str) -> list[float]:
        cutoff = time.monotonic() - self.window_seconds
        recent = [t for t in self._failures[username.lower()] if t > cutoff]
        self._failures[username.lower()] = recent
        return recent

    def locked(self, username: str) -> bool:
        with self._lock:
            return len(self._recent(username)) >= self.max_failures

    def fail(self, username: str) -> None:
        with self._lock:
            self._recent(username).append(time.monotonic())

    def clear(self, username: str) -> None:
        with self._lock:
            self._failures.pop(username.lower(), None)


# Per app, not per process: every app gets its own (as every test building one does).
_THROTTLE: AppSlot[_Throttle] = AppSlot("password sign-in throttle")


# -- Pages ----------------------------------------------------------------------------------------


_PAGE: Final = Path(__file__).with_name("password_form.html").read_text()


class _Field(NamedTuple):
    name: str
    label: str
    input_type: str
    autocomplete: str


class _Footer(NamedTuple):
    text: str
    href: str
    link_text: str


_USERNAME: Final = _Field("username", "Username", "text", "username")
_PASSWORD: Final = _Field("password", "Password", "password", "current-password")
_NEW_PASSWORD: Final = _Field("password", "New password", "password", "new-password")
_CONFIRM: Final = _Field("confirm", "Confirm password", "password", "new-password")


def _csrf_token() -> str:
    token = session.get("csrf")
    if token is None:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return str(token)


def _require_csrf() -> None:
    if not hmac.compare_digest(request.form.get("csrf", ""), _csrf_token()):
        abort(400)


@dataclass(frozen=True)
class _Form:
    """One of this provider's pages: a single form, re-rendered with an error or a message after a POST."""

    title: str
    fields: list[_Field]
    submit: str
    footer: _Footer | None = None

    def render(self, *, error: str | None = None, message: str | None = None) -> str:
        return render_template_string(
            _PAGE,
            title=self.title,
            fields=self.fields,
            submit=self.submit,
            footer=self.footer,
            error=error,
            message=message,
            csrf=_csrf_token(),
        )


def new_password_problem(password: str, confirm: str) -> str | None:
    """Why `password` can't be used, or `None` if it can."""
    if password != confirm:
        return "The passwords don't match."
    min_length = PasswordSettings().min_length
    if len(password) < min_length:
        return f"Use at least {min_length} characters."
    return None


def _login() -> str | Response:
    open_signup = PasswordSettings().signup is not SignupPolicy.ADMIN_CREATES
    form = _Form(
        "Sign in",
        [_USERNAME, _PASSWORD],
        "Sign in",
        _Footer("No account yet?", "/signup", "Create one") if open_signup else None,
    )
    if request.method == "GET":
        return form.render()
    _require_csrf()
    username, password = request.form.get("username", "").strip(), request.form.get("password", "")
    throttle = _THROTTLE.get()
    if throttle.locked(username):
        return form.render(error="Too many attempts. Try again later.")
    store = get_system_data_store()
    user = store.find_user(username)
    credential = store.get_password_credential(user.id) if user and user.issuer == PASSWORD_ISSUER else None
    matches = verify_password(password, credential.password_hash if credential else _DECOY_HASH)
    if credential is None or not matches:
        throttle.fail(username)
        return form.render(error="Wrong username or password.")
    signed_in = sign_in(store, _principal(username), get_auth_settings())
    if signed_in is None:
        return form.render(error="This account is disabled, or waiting for an admin to approve it.")
    throttle.clear(username)
    start_session(signed_in)
    return redirect(safe_next_path(request.args.get("next")))


def _signup() -> str | Response:
    policy = PasswordSettings().signup
    if policy is SignupPolicy.ADMIN_CREATES:
        abort(404)
    form = _Form(
        "Create an account",
        [_USERNAME, _NEW_PASSWORD, _CONFIRM],
        "Create account",
        _Footer("Already have an account?", "/login", "Sign in"),
    )
    if request.method == "GET":
        return form.render()
    _require_csrf()
    username, password = request.form.get("username", "").strip(), request.form.get("password", "")
    store = get_system_data_store()
    problem = new_password_problem(password, request.form.get("confirm", ""))
    if not username or store.find_user(username) is not None:
        problem = "That username isn't available."
    if problem is not None:
        return form.render(error=problem)
    user = create_or_reset_user(store, username, password)
    if policy is SignupPolicy.APPROVAL:
        store.update_user(user.model_copy(update={"disabled_at": pendulum.now(pendulum.UTC)}))
        return form.render(message="Account created. You can sign in once an admin approves it.")
    signed_in = sign_in(store, _principal(username), get_auth_settings())
    if signed_in is not None:
        start_session(signed_in)
    return redirect("/")


def _change_password() -> str | Response:
    form = _Form(
        "Change password",
        [_Field("current", "Current password", "password", "current-password"), _NEW_PASSWORD, _CONFIRM],
        "Change password",
        _Footer("", "/", "Back to dltrack"),
    )
    if request.method == "GET":
        return form.render()
    _require_csrf()
    store, user = get_system_data_store(), get_current_user()
    credential = store.get_password_credential(user.id)
    if credential is None or not verify_password(request.form.get("current", ""), credential.password_hash):
        return form.render(error="Your current password is wrong.")
    password = request.form.get("password", "")
    problem = new_password_problem(password, request.form.get("confirm", ""))
    if problem is not None:
        return form.render(error=problem)
    start_session(set_password(store, user, password))  # keeps this browser signed in; signs out the rest
    return form.render(message="Password changed.")


def _manage_users() -> str | Response:
    if not models.has_scope(get_current_user(), models.Scope.USER_MANAGE):
        abort(403)
    form = _Form(
        "Add a user or reset a password",
        [_USERNAME, _NEW_PASSWORD, _CONFIRM, _Field("admin", "Make them an admin", "checkbox", "off")],
        "Save",
        _Footer("", "/admin", "Back to admin"),
    )
    if request.method == "GET":
        return form.render()
    _require_csrf()
    username, password = request.form.get("username", "").strip(), request.form.get("password", "")
    store = get_system_data_store()
    existing = store.find_user(username)
    problem = new_password_problem(password, request.form.get("confirm", ""))
    if not username or (existing is not None and existing.issuer != PASSWORD_ISSUER):
        problem = "That username belongs to a user who doesn't sign in with a password."
    if problem is not None:
        return form.render(error=problem)
    create_or_reset_user(store, username, password, admin=request.form.get("admin") == "on")
    return form.render(message=f"{'Password reset' if existing else 'User created'} for {username}.")


class PasswordAuthProvider:
    """Signs people in with a username and password, then keeps them signed in with a session."""

    display_name: ClassVar[str] = "Password"
    verifies_identity: ClassVar[bool] = True
    manage_url: ClassVar[str | None] = "/password"

    def authenticate(self) -> None:
        """Only ever a session (started by the login form) -- never anything on the request itself."""

    def login_url(self, next_path: str) -> str:
        """The login form, returning to `next_path` once signed in."""
        return f"/login?next={quote(next_path)}"

    @classmethod
    def get_or_create(cls) -> PasswordAuthProvider:
        """Initialize the provider. Stateless, so this is just construction."""
        return cls()


def plug(app: Dash) -> None:
    """Register the provider and its forms."""
    set_auth_provider(app, PasswordAuthProvider.get_or_create())
    _THROTTLE.set(app, _Throttle())
    add_public_route(app, "login", _login, ["GET", "POST"])
    add_public_route(app, "signup", _signup, ["GET", "POST"])
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    for rule, view in (("password", _change_password), ("password/admin", _manage_users)):
        app.server.add_url_rule(
            prefix + rule, endpoint=prefix + rule, view_func=view, methods=["GET", "POST"]
        )
