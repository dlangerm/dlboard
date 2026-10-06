"""
Best-effort resolution of "who is running this" for attribution purposes.

This is an identity helper, not authentication -- nothing here is a security boundary, and nothing
here proves a caller actually is who they claim. It exists so a local, single-user dlboard install
can attribute projects/experiments/runs to *someone* with zero configuration, the same way `git`
picks up `user.name` from the environment. Used by both the client (`dlboard.client`, to label
what it logs) and the server (schema migrations, to pick a name for the one-time bootstrap admin).
"""

from __future__ import annotations

import getpass

from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

_log = get_logger(__name__)

ANONYMOUS: str = "anonymous"


class _IdentitySettings(BaseSettings):
    """Reads the `DLBOARD_USER` env var `resolve_username` checks first."""

    dlboard_user: str = ""


def resolve_username() -> str:
    """
    Resolve a username via a best-effort chain: the `DLBOARD_USER` env var, then the OS login name, then `"anonymous"`.

    Every step is wrapped so a failure (e.g. `getpass.getuser()` raising in a sandboxed container
    with no passwd entry) falls through to the next step instead of raising.
    """
    env_value = _IdentitySettings().dlboard_user.strip()
    if env_value:
        return env_value

    try:
        os_username = getpass.getuser().strip()
    except Exception:  # noqa: BLE001
        _log.debug("Could not resolve an OS username, falling back to %s", ANONYMOUS)
    else:
        if os_username:
            return os_username

    return ANONYMOUS
