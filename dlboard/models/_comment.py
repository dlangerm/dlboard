"""Notes on an experiment: a thread of comments, each optionally about some runs or people."""

from __future__ import annotations

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class NewComment(BaseModel, frozen=True, extra="forbid"):
    """A note about to be posted to an experiment's thread."""

    experiment_id: int
    author_id: int

    body: str = Field(min_length=1)
    """Plain text; shown as written (line breaks kept), never interpreted as markup."""

    run_ids: list[int] = []
    """Runs this note is about, shown as chips in each run's chart color."""

    mentioned_user_ids: list[int] = []
    """Users this note calls out."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))


class Comment(NewComment, frozen=True, extra="forbid"):
    """A posted note."""

    id: int
