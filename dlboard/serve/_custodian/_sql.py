"""
`TaskStore` on SQLAlchemy Core: a mixin of `SQLStoreBase`, so every SQL store keeps its custodial state in its own tables.

Every write is one statement (a conditional `UPDATE` claims a lease or a batch of items atomically), so custodians never
hold a lock across statements, and a batch of items is claimed in one round trip, never one row at a time.
"""

from __future__ import annotations

import itertools
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Final

import pendulum
import sqlalchemy as sa
from pydantic import BaseModel

from dlboard.serve import sql
from dlboard.serve._custodian._models import (
    Custodian,
    NewWorkItem,
    TaskOutcome,
    TaskState,
    WorkItem,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping
    from datetime import datetime

_BATCH: Final = 500
"""How many rows or ids one statement binds."""
_FORGOTTEN_AFTER: Final = pendulum.duration(days=7)
"""How long a custodian that stopped beating is listed for."""


class SQLTaskStore:
    """The `TaskStore` methods, over the tables `TASK_TABLES` adds to a store's schema."""

    @property
    @abstractmethod
    def tables(self) -> Mapping[type[BaseModel], sa.Table]: ...

    @abstractmethod
    def _execute(self, statement: sa.Executable, *, uncached: bool = False) -> list[Any]: ...

    @abstractmethod
    def _insert_ignoring_conflicts(self, table: sa.Table) -> sa.Insert: ...

    @abstractmethod
    def _fetch[RowT: BaseModel](
        self, model: type[RowT], statement: sa.Executable, *, no_validate: bool = False
    ) -> list[RowT]: ...

    def ensure_tasks(self, names: Collection[str]) -> None:
        """Give each task a row, once, as a custodian starts: every write after is a single `UPDATE` of it."""
        states = self.tables[TaskState]
        self._execute(
            self._insert_ignoring_conflicts(states).values([{"name": name, "progress": {}} for name in names])
        )

    def claim_task(self, name: str, holder: str, *, until: datetime) -> bool:
        """One conditional `UPDATE`: of custodians asking at once, only one gets a lease no one holds."""
        states = self.tables[TaskState]
        claimed = self._execute(
            sa.update(states)
            .where(
                states.c.name == name,
                sa.or_(
                    states.c.holder.is_(None),
                    states.c.holder == holder,
                    states.c.lease_until <= pendulum.now(pendulum.UTC),
                ),
            )
            .values({states.c.holder: holder, states.c.lease_until: until})
            .returning(states.c.id)
        )
        return bool(claimed)

    def release_task(self, name: str, holder: str) -> None:
        states = self.tables[TaskState]
        self._execute(
            sa.update(states)
            .where(states.c.name == name, states.c.holder == holder)
            .values({states.c.lease_until: pendulum.now(pendulum.UTC)})
        )

    def start_task_run(self, name: str) -> None:
        """Record the start of a run: a request made before it is answered by it, so it is cleared."""
        states = self.tables[TaskState]
        self._execute(
            sa.update(states)
            .where(states.c.name == name)
            .values({states.c.last_started_at: pendulum.now(pendulum.UTC), states.c.requested_at: None})
        )

    def finish_task_run(
        self, name: str, outcome: TaskOutcome, *, error: str | None, progress: dict[str, Any] | None
    ) -> None:
        """Record the end of a run."""
        states = self.tables[TaskState]
        now = pendulum.now(pendulum.UTC)
        values: dict[sa.Column[Any], Any] = {
            states.c.last_finished_at: now,
            states.c.last_outcome: outcome.value,
            states.c.last_error: error,
        }
        if outcome is TaskOutcome.SUCCESS:
            values[states.c.last_succeeded_at] = now
        if progress is not None:
            values[states.c.progress] = progress
        self._execute(sa.update(states).where(states.c.name == name).values(values))

    def record_task_progress(self, name: str, progress: dict[str, Any]) -> None:
        states = self.tables[TaskState]
        self._execute(sa.update(states).where(states.c.name == name).values({states.c.progress: progress}))

    def request_task_run(self, name: str) -> None:
        """Ask for a run: from a web process, which may ask before any custodian has given the task its row."""
        self.ensure_tasks([name])
        states = self.tables[TaskState]
        self._execute(
            sa.update(states)
            .where(states.c.name == name)
            .values({states.c.requested_at: pendulum.now(pendulum.UTC)})
        )

    def task_states(self) -> list[TaskState]:
        states = self.tables[TaskState]
        return self._fetch(TaskState, sa.select(states).order_by(states.c.name))

    def beat(self, holder: str, *, host: str, pid: int, started_at: datetime) -> None:
        """One statement a beat; a custodian's first adds its row, and forgets the custodians gone a week."""
        custodians = self.tables[Custodian]
        now = pendulum.now(pendulum.UTC)
        if self._execute(
            sa.update(custodians)
            .where(custodians.c.holder == holder)
            .values({custodians.c.heartbeat_at: now})
            .returning(custodians.c.id)
        ):
            return
        self._execute(
            self._insert_ignoring_conflicts(custodians).values(
                holder=holder, host=host, pid=pid, started_at=started_at, heartbeat_at=now
            )
        )
        # One row a start of a process: those not seen for a week are history nobody reads.
        self._execute(sa.delete(custodians).where(custodians.c.heartbeat_at < now - _FORGOTTEN_AFTER))

    def custodians(self, *, seen_since: datetime) -> list[Custodian]:
        custodians = self.tables[Custodian]
        return self._fetch(
            Custodian,
            sa.select(custodians)
            .where(custodians.c.heartbeat_at >= seen_since)
            .order_by(custodians.c.heartbeat_at.desc()),
        )

    def enqueue_work(self, items: Iterable[NewWorkItem]) -> None:
        work = self.tables[WorkItem]
        for batch in itertools.batched((sql.row_values(item) for item in items), _BATCH):
            self._execute(self._insert_ignoring_conflicts(work).values(list(batch)))

    def claim_work(self, kind: str, holder: str, *, limit: int, until: datetime) -> list[WorkItem]:
        """
        Claim a batch in one `UPDATE` of the ids its subquery picks.

        On Postgres the subquery is `FOR UPDATE SKIP LOCKED`, so a claimer passes over rows another is claiming instead of
        waiting; on SQLite, which has one writer, the statement is atomic anyway.
        """
        work = self.tables[WorkItem]
        now = pendulum.now(pendulum.UTC)
        due = (
            sa.select(work.c.id)
            .where(
                work.c.kind == kind,
                work.c.due_at <= now,
                sa.or_(work.c.claimed_until.is_(None), work.c.claimed_until < now),
            )
            .order_by(work.c.due_at, work.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        claimed = self._fetch(
            WorkItem,
            sa.update(work)
            .where(work.c.id.in_(due))
            .values(
                {work.c.claimed_by: holder, work.c.claimed_until: until, work.c.attempts: work.c.attempts + 1}
            )
            .returning(work),
        )
        return sorted(claimed, key=lambda w: (w.due_at, w.id))

    def renew_work(self, holder: str, *, until: datetime) -> None:
        """Extend every claim `holder` still holds: one `UPDATE` a beat while it works, so a long run is not claimed twice."""
        work = self.tables[WorkItem]
        self._execute(
            sa.update(work)
            .where(work.c.claimed_by == holder, work.c.claimed_until > pendulum.now(pendulum.UTC))
            .values({work.c.claimed_until: until})
        )

    def complete_work(self, item_ids: Collection[int]) -> None:
        work = self.tables[WorkItem]
        for batch in itertools.batched(sorted(item_ids), _BATCH):
            self._execute(sa.delete(work).where(work.c.id.in_(batch)))

    def retry_work(self, item_id: int, *, error: str, due_at: datetime) -> None:
        work = self.tables[WorkItem]
        self._execute(
            sa.update(work)
            .where(work.c.id == item_id)
            .values(
                {
                    work.c.claimed_by: None,
                    work.c.claimed_until: None,
                    work.c.last_error: error,
                    work.c.due_at: due_at,
                }
            )
        )

    def count_work(self, kind: str) -> int:
        work = self.tables[WorkItem]
        ((count,),) = self._execute(sa.select(sa.func.count()).select_from(work).where(work.c.kind == kind))
        return count


TASK_TABLES: Final[tuple[type[BaseModel], ...]] = (TaskState, Custodian, WorkItem)
"""The tables `SQLTaskStore` keeps its state in, added to every SQL store's schema (`TABLES`)."""

TASK_UNIQUE_COLUMNS: Final[dict[type[BaseModel], list[tuple[str, ...]]]] = {
    TaskState: [("name",)],
    Custodian: [("holder",)],
    WorkItem: [("kind", "key")],
}
