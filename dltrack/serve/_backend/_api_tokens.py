"""
API tokens: how a training script authenticates, independent of how its owner signs in to the UI.

A token is `dlt_<token_id>_<secret>`. `token_id` is stored as-is so the token can be looked up;
only a SHA-256 of `secret` is stored, so a leaked database doesn't leak working tokens. The secret
is 256 random bits, so a slow password hash would buy nothing over SHA-256 here.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import TYPE_CHECKING, Final

import pendulum

from dltrack import models

if TYPE_CHECKING:
    from pydantic import AwareDatetime

    from dltrack.models import DataStore, User

API_TOKEN_PREFIX: Final = "dlt_"
_ID_BYTES: Final = 8
_SECRET_BYTES: Final = 32
_URLSAFE_SECRET_LENGTH: Final = 43
"""`secrets.token_urlsafe(_SECRET_BYTES)` is unpadded base64: 4 characters per 3 bytes, rounded up."""
_TOKEN_SHAPE: Final = re.compile(
    rf"{API_TOKEN_PREFIX}(?P<token_id>[0-9a-f]{{{2 * _ID_BYTES}}})_(?P<secret>[A-Za-z0-9_-]{{{_URLSAFE_SECRET_LENGTH}}})"
)
_TOUCH_INTERVAL: Final = pendulum.duration(minutes=1)
"""How stale `ApiToken.last_used_at` may get -- so a busy training job doesn't write on every request."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def is_api_token(raw: str) -> bool:
    """Whether `raw`, in full, is shaped like a dltrack API token (as opposed to some other bearer credential)."""
    return _TOKEN_SHAPE.fullmatch(raw) is not None


def mint_api_token(
    store: DataStore[...], user: User, name: str, expires_at: AwareDatetime | None = None
) -> tuple[models.ApiToken, str]:
    """Create a token for `user`, returning it alongside the only copy of its full secret string."""
    token_id, secret = secrets.token_hex(_ID_BYTES), secrets.token_urlsafe(_SECRET_BYTES)
    token = store.create_api_token(
        models.NewApiToken(
            user_id=user.id, name=name, token_id=token_id, secret_hash=_hash(secret), expires_at=expires_at
        )
    )
    return token, f"{API_TOKEN_PREFIX}{token_id}_{secret}"


def user_for_token(store: DataStore[...], raw: str) -> User | None:
    """The (enabled) user an unexpired, unrevoked token `raw` authenticates as, else `None`."""
    shape = _TOKEN_SHAPE.fullmatch(raw)
    if shape is None:
        return None
    token = store.get_api_token(shape["token_id"])
    now = pendulum.now(pendulum.UTC)
    if (
        token is None
        or not hmac.compare_digest(token.secret_hash, _hash(shape["secret"]))
        or token.revoked_at is not None
        or (token.expires_at is not None and token.expires_at <= now)
    ):
        return None
    user = store.get_user(token.user_id)
    if user is None or user.disabled_at is not None:
        return None
    if token.last_used_at is None or token.last_used_at < now - _TOUCH_INTERVAL:
        store.touch_api_token(token.id)
    return user
