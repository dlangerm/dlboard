"""
Whether background work is being done, as the database says: for `/healthz/custodian`, and the admin page.

Read by a web worker, which runs no task itself, so it answers whatever state the custodian is in, even when there is
none: a custodian that stopped beating, or a task that stopped succeeding, is what this is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pendulum

from dlboard.serve._custodian._models import TaskOutcome
from dlboard.serve._custodian._supervisor import CustodianSettings

if TYPE_CHECKING:
    from dlboard.serve._custodian._models import Custodian, TaskState
    from dlboard.serve._custodian._store import TaskStore
    from dlboard.serve._custodian._task import Task

_LATE: Final = 3
"""How many of its intervals a task (or beats, a custodian) may miss before it is called overdue (or gone)."""


@dataclass(frozen=True)
class CustodialHealth:
    """The custodians alive, and the tasks overdue: healthy while there is a custodian and nothing is overdue."""

    custodians: list[Custodian]
    states: dict[str, TaskState]
    overdue: list[str]

    @property
    def healthy(self) -> bool:
        return bool(self.custodians) and not self.overdue


def _overdue(task: Task, state: TaskState | None, now: pendulum.DateTime) -> bool:
    """Whether a task on an interval has gone three of them without succeeding, or last hung."""
    if state is not None and state.last_outcome is TaskOutcome.HUNG:
        return True
    if task.every is None or state is None or state.last_finished_at is None:
        return False  # it has not run yet: a new custodian's first runs are spread over its first poll
    last_success = state.last_succeeded_at
    return last_success is None or now - pendulum.instance(last_success) > task.every * _LATE


def custodial_health(store: TaskStore, tasks: list[Task]) -> CustodialHealth:
    """How the custodians and `tasks` stand, in two queries."""
    now = pendulum.now(pendulum.UTC)
    beat = pendulum.duration(seconds=CustodianSettings().beat_s)
    custodians = store.custodians(seen_since=now - beat * _LATE)
    states = {state.name: state for state in store.task_states()}
    overdue = [str(task.name) for task in tasks if _overdue(task, states.get(str(task.name)), now)]
    return CustodialHealth(custodians, states, overdue)
