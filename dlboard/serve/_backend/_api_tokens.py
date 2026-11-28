"""
API tokens: how a training script authenticates, independent of how its owner signs in to the UI.

A token is `dlb_<token_id>_<secret>`. `token_id` is stored as-is so the token can be looked up;
only a SHA-256 of `secret` is stored, so a leaked database doesn't leak working tokens. The secret
is 256 random bits, so a slow password hash would buy nothing over SHA-256 here.

A run token, bound to one run (#718), is `dlr_...` instead: a server from before run tokens doesn't take it for a dlboard
token at all, rather than for its owner's full one.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard import models

if TYPE_CHECKING:
    from pydantic import AwareDatetime

    from dlboard.models import DataStore, User

API_TOKEN_PREFIX: Final = "dlb_"
RUN_TOKEN_PREFIX: Final = "dlr_"
TOKEN_PREFIXES: Final = (API_TOKEN_PREFIX, RUN_TOKEN_PREFIX)
_ID_BYTES: Final = 8
_SECRET_BYTES: Final = 32
_URLSAFE_SECRET_LENGTH: Final = 43
"""`secrets.token_urlsafe(_SECRET_BYTES)` is unpadded base64: 4 characters per 3 bytes, rounded up."""
_TOKEN_SHAPE: Final = re.compile(
    rf"(?P<prefix>{API_TOKEN_PREFIX}|{RUN_TOKEN_PREFIX})"
    rf"(?P<token_id>[0-9a-f]{{{2 * _ID_BYTES}}})_(?P<secret>[A-Za-z0-9_-]{{{_URLSAFE_SECRET_LENGTH}}})"
)
_TOUCH_INTERVAL: Final = pendulum.duration(minutes=1)
"""How stale `ApiToken.last_used_at` may get -- so a busy training job doesn't write on every request."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class RunTokenSettings(BaseSettings):
    """How long a run token works (`DLBOARD_RUN_TOKEN_*`)."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_RUN_TOKEN_", frozen=True)

    lifetime_hours: PositiveInt = 7 * 24
    """A run token stops working this long after it was minted: long enough for a job that waits in a queue, then trains."""


@dataclass(frozen=True)
class Bearer:
    """Who a token authenticates as, and the one run it is bound to if it is a run token."""

    user: User
    run_id: int | None


def mint_api_token(
    store: DataStore[...],
    user: User,
    name: str,
    expires_at: AwareDatetime | None = None,
    *,
    run_id: int | None = None,
) -> tuple[models.ApiToken, str]:
    """Create a token for `user` (bound to `run_id`, if given), returning it with the only copy of its full secret."""
    token_id, secret = secrets.token_hex(_ID_BYTES), secrets.token_urlsafe(_SECRET_BYTES)
    token = store.create_api_token(
        models.NewApiToken(
            user_id=user.id,
            name=name,
            token_id=token_id,
            secret_hash=_hash(secret),
            expires_at=expires_at,
            run_id=run_id,
        )
    )
    return token, f"{_prefix(run_id)}{token_id}_{secret}"


def _prefix(run_id: int | None) -> str:
    return API_TOKEN_PREFIX if run_id is None else RUN_TOKEN_PREFIX


def mint_run_token(store: DataStore[...], user: User, run_id: int) -> tuple[models.ApiToken, str]:
    """A token for `user` that logs to `run_id` alone, and expires after `RunTokenSettings.lifetime_hours`."""
    expires_at = pendulum.now(pendulum.UTC).add(hours=RunTokenSettings().lifetime_hours)
    return mint_api_token(store, user, f"run {run_id}", expires_at, run_id=run_id)


def _live(token: models.ApiToken, now: AwareDatetime) -> bool:
    return token.revoked_at is None and (token.expires_at is None or token.expires_at > now)


def has_api_key(store: DataStore[...], user: User) -> bool:
    """Whether `user` has a key a training script can log in with: a full token, neither revoked nor expired."""
    now = pendulum.now(pendulum.UTC)
    return any(_live(token, now) for token in store.list_api_tokens(user.id))


def idle(user: User, idle_for: pendulum.Duration | None, now: AwareDatetime) -> bool:
    """
    Whether `user`'s tokens no longer work: they haven't signed in through the auth provider within `idle_for`.

    Counted from when they were made, if they never have (`DLBOARD_TOKEN_IDLE_DAYS`); never idle, with no `idle_for`.
    """
    return idle_for is not None and (user.signed_in_at or user.created_at) < now - idle_for


def user_for_token(
    store: DataStore[...], raw: str, idle_for: pendulum.Duration | None = None
) -> Bearer | None:
    """
    The (enabled) user an unexpired, unrevoked token `raw` authenticates as, and its run, else `None`.

    Also `None` for a user `idle` for `idle_for`: a token stops with its owner's sign-ins, not only its own expiry.
    """
    shape = _TOKEN_SHAPE.fullmatch(raw)
    if shape is None:
        return None
    token = store.get_api_token(shape["token_id"])
    now = pendulum.now(pendulum.UTC)
    if (
        token is None
        or not hmac.compare_digest(token.secret_hash, _hash(shape["secret"]))
        or shape["prefix"] != _prefix(token.run_id)
        or not _live(token, now)
    ):
        return None
    user = store.get_user(token.user_id)
    if user is None or user.disabled_at is not None or idle(user, idle_for, now):
        return None
    if token.last_used_at is None or token.last_used_at < now - _TOUCH_INTERVAL:
        store.touch_api_token(token.id)
    return Bearer(user, token.run_id)
