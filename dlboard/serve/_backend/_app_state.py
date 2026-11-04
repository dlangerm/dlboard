"""
Internal singleton table used to track cross-process/cross-startup state: the bootstrap admin grant, a running purge.

Not part of the public `dlboard.models` package -- this exists purely so
`SQLStoreBase`/migrations can answer "has the one-time bootstrap admin grant already been
claimed?" without racing multiple processes into granting `Scope.ALL` more than once.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel

APP_STATE_ROW_ID: int = 1
"""`AppState` is a single-row table; this is that row's fixed id."""


class AppState(BaseModel, frozen=True, extra="forbid"):
    """A single-row table holding process-wide bootstrap flags."""

    id: int
    bootstrap_admin_assigned: bool = False
    artifact_purge_progress_at: AwareDatetime | None = None
    """
    When a purge last made progress, while one is running; `None` once it is done. Shared by every server process: a
    stamp that is recent means one is purging now, an old one that its process died (a purge to resume).
    """
    retention_lease_holder: str | None = None
    """
    Which server process enforces retention, while its lease lasts: of every process of every server sharing the
    database, only the one holding the lease deletes anything, so they never contend for the same rows.
    """
    retention_lease_until: AwareDatetime | None = None
    """When `retention_lease_holder`'s lease runs out, unless it renews it: after that, any process may take it."""
    retention_swept_at: AwareDatetime | None = None
    """When retention last checked every project."""
    trash_retention_since: AwareDatetime | None = None
    """
    When retention first ran on this database: the Trash is emptied of what has been in it `DLBOARD_TRASH_RETENTION_DAYS`
    counted from no earlier than this, so what was deleted long before an upgrade is not purged the moment it lands.
    """
