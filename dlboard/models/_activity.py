"""At-a-glance activity for a project or experiment: how much is in it, and when it last changed."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel


class ActivityStats(BaseModel, frozen=True, extra="forbid"):
    """An experiment's (non-deleted) run count and most recent activity."""

    run_count: int = 0

    last_activity_at: AwareDatetime | None = None
    """See `Experiment.last_activity_at`; for a project, the latest across its experiments."""


class ProjectStats(ActivityStats, frozen=True, extra="forbid"):
    """A project's activity, plus how many (non-deleted) experiments it has."""

    experiment_count: int = 0
