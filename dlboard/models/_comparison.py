"""Comparisons: a dashboard of hand-picked runs from any experiments of one project."""

from __future__ import annotations

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewComparisonRun(BaseModel, frozen=True, extra="forbid"):
    """A run about to be added to a comparison, which is a `Page` scoped to a project."""

    page_id: int
    run_id: int
    added_by: int
    """The user who added it. Who may is the comparison's rule (`AuthorizingDataStore`), not recorded here."""

    added_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When it was added: "added by Alice, three days ago", or the newest additions first."""


class ComparisonRun(NewComparisonRun, frozen=True, extra="forbid"):
    """A run that is a member of a comparison."""

    id: int


class ComparisonSummary(BaseModel, frozen=True, extra="forbid"):
    """Just enough of a comparison to list it: in the project page's cards, and the menu of comparisons to add runs to."""

    id: int
    name: str
    owner_id: int
    owner_name: str
    """The username of `owner_id`, read with the comparison so that naming its owner never reads the users."""
    shared: bool
    run_count: int
    """How many of its runs are still there: a deleted run, or one of a deleted experiment, is not counted."""
