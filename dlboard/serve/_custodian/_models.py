"""
What the database holds about custodial work: each task's state, each custodian process's heartbeat, and the work items.

The database is the source of truth for what is running where: a web process reads these to say how background work
stands (the admin page, `/healthz/custodian`), and every custodian reads them to share the work.
"""

from __future__ import annotations

from typing import Any

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dlboard._compat import StrEnum


class TaskOutcome(StrEnum):
    """
    How a task's last run ended. Written to a database column, so a value is never renamed or removed.

    `HUNG` is a run that passed its deadline: its custodian recorded it and exited, to be started again clean.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    HUNG = "hung"


class TaskState(BaseModel, frozen=True, extra="forbid"):
    """One task's row: who holds it, when it last ran and how that went, and what it last said it was doing."""

    name: str
    holder: str | None = None
    """The custodian holding this task's lease (`host:pid:tag`), for a task that runs one at a time."""
    lease_until: AwareDatetime | None = None
    last_started_at: AwareDatetime | None = None
    last_finished_at: AwareDatetime | None = None
    last_succeeded_at: AwareDatetime | None = None
    last_outcome: TaskOutcome | None = None
    last_error: str | None = None
    requested_at: AwareDatetime | None = None
    """Set when a run is asked for (`request_task_run`), and cleared when a run starts: that run answers it."""
    progress: dict[str, Any] = {}
    """What the task last said about itself (how much it has left, say): its own, as JSON."""

    id: int


class Custodian(BaseModel, frozen=True, extra="forbid"):
    """One custodian process, as it last said it was alive."""

    holder: str
    """`host:pid:tag`: unique to one start of one process."""
    host: str
    pid: int
    started_at: AwareDatetime
    heartbeat_at: AwareDatetime

    id: int


class NewWorkItem(BaseModel, frozen=True, extra="forbid"):
    """
    One piece of work for a task to claim: a blob to delete, a project to check. Unique by `kind` and `key`.

    Enqueuing an item already queued does nothing, so whatever finds work to do can say so as often as it finds it.
    """

    kind: str
    key: str
    due_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    payload: dict[str, Any] = {}


class WorkItem(NewWorkItem, frozen=True, extra="forbid"):
    """A queued work item, and who has claimed it until when."""

    claimed_by: str | None = None
    claimed_until: AwareDatetime | None = None
    attempts: int = 0
    last_error: str | None = None

    id: int
