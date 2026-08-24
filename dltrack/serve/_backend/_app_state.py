"""
Internal singleton table used to track cross-process/cross-startup bootstrap state.

Not part of the public `dltrack.models` package -- this exists purely so
`SQLStoreBase`/migrations can answer "has the one-time bootstrap admin grant already been
claimed?" without racing multiple processes into granting `Scope.ALL` more than once.
"""

from __future__ import annotations

from pydantic import BaseModel

APP_STATE_ROW_ID: int = 1
"""`AppState` is a single-row table; this is that row's fixed id."""


class AppState(BaseModel, frozen=True, extra="forbid"):
    """A single-row table holding process-wide bootstrap flags."""

    id: int
    bootstrap_admin_assigned: bool = False
