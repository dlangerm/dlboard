"""
`TaskStore`: what custodial work keeps in the database, kept apart from the `DataStore` and its data.

Every SQL store has one (`SQLTaskStore`, a mixin of `SQLStoreBase`): the custodian reads and writes it as the system,
and a request only ever reads it, through `AuthorizingTaskStore` (`get_task_store()`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, runtime_checkable

from flask import has_request_context

from dlboard.models import Scope, require_scope
from dlboard.serve._backend._app_slot import AppSlot, current_dash_app
from dlboard.serve._backend._auth import get_auth_provider, get_current_user
from dlboard.serve._backend._data_store import DATA_STORE

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping
    from datetime import datetime

    from dash import Dash

    from dlboard._compat import StrEnum
    from dlboard.models import User
    from dlboard.serve._custodian._models import Custodian, NewWorkItem, TaskOutcome, TaskState, WorkItem


@runtime_checkable
class TaskStore(Protocol):
    """The state of custodial work: tasks' leases and runs, custodians' heartbeats, and the queue of work items."""

    def ensure_tasks(self, names: Collection[str]) -> None:
        """Give each task its row, if it has none: a custodian does it as it starts, so that no write need check."""
        ...

    def claim_task(self, name: str, holder: str, *, until: datetime) -> bool:
        """Take (or renew) `name`'s lease until `until`, unless another holder holds it: whether it did."""
        ...

    def release_task(self, name: str, holder: str) -> None:
        """Let go of `name`'s lease now, if `holder` holds it, so that another custodian need not wait it out."""
        ...

    def start_task_run(self, name: str) -> None:
        """Record that a run of `name` started now."""
        ...

    def finish_task_run(
        self, name: str, outcome: TaskOutcome, *, error: str | None, progress: dict[str, Any] | None
    ) -> None:
        """Record how a run of `name` ended; a request it answered (made before it started) is cleared."""
        ...

    def record_task_progress(self, name: str, progress: dict[str, Any]) -> None:
        """Record what `name` says it is doing (its backlog, say), mid-run."""
        ...

    def request_task_run(self, name: str) -> None:
        """Ask for a run of `name` as soon as a custodian can: whichever one picks it up next."""
        ...

    def task_states(self) -> list[TaskState]:
        """Every task's row, by name."""
        ...

    def beat(self, holder: str, *, host: str, pid: int, started_at: datetime) -> None:
        """Record that the custodian `holder` is alive now."""
        ...

    def custodians(self, *, seen_since: datetime) -> list[Custodian]:
        """The custodians that said they were alive since `seen_since`, the most recent first."""
        ...

    def enqueue_work(self, items: Iterable[NewWorkItem]) -> None:
        """Queue `items`; one already queued under its `kind` and `key` is left as it is."""
        ...

    def queue_on_run_end(self, kinds: frozenset[str]) -> None:
        """
        From now on, queue one item of each of `kinds` for every run's end (`RunEnded`), in the transaction recording it.

        An end is a run's client reporting a status it didn't have (`finish_run`), or the run being given up as lost.
        """
        ...

    def claim_work(self, kind: str, holder: str, *, limit: int, until: datetime) -> list[WorkItem]:
        """
        Claim up to `limit` of `kind`'s due items for `holder` until `until`, the longest due first.

        One that is claimed is skipped, not waited for (`SKIP LOCKED`, on Postgres): custodians claiming at once each get
        their own items. A claim that ran out (its custodian died) can be claimed again.
        """
        ...

    def renew_work(self, holder: str, *, until: datetime) -> None:
        """Extend until `until` every claim `holder` holds that has not run out: its items are still being worked on."""
        ...

    def complete_work(self, item_ids: Collection[int]) -> None:
        """Take finished items off the queue."""
        ...

    def retry_work(self, item_id: int, *, error: str, due_at: datetime) -> None:
        """Let an item that failed go, to be claimed again at `due_at`."""
        ...

    def count_work(self, kind: str) -> int:
        """How many of `kind`'s items are queued, claimed or not: a task's backlog."""
        ...

    def queued_keys(self, kind: str, keys: Collection[str]) -> frozenset[str]:
        """Which of `keys` have an item of `kind` queued, claimed or not: found by the unique `(kind, key)`."""
        ...


@dataclass(frozen=True)
class WorkRule:
    """Who may queue one kind of work item from a request (an admin asking for a purge), and the task that does it."""

    kind: StrEnum
    scope: Scope
    task: StrEnum
    """Asked for as the work is queued, so it starts on the next poll rather than on its interval."""
    actor_field: str | None = None
    """
    The payload field that names who asked. The store sets it to the requesting user, whatever the request says, so the
    custodian never acts as someone who did not ask.
    """


WORK_RULES: AppSlot[dict[str, WorkRule]] = AppSlot("work rules")


def register_work_kind(app: Dash, rule: WorkRule) -> None:
    """Let a request queue `rule.kind` of work, as a user holding `rule.scope`: no other kind can be queued but by the system."""
    rules = WORK_RULES.setdefault(app, dict)
    if str(rule.kind) in rules:
        msg = f"Work of kind {rule.kind!r} is already registered"
        raise ValueError(msg)
    rules[str(rule.kind)] = rule


RUN_END_WORK: AppSlot[set[str]] = AppSlot("run end work")


def register_run_end_work(app: Dash, kind: StrEnum) -> None:
    """
    Queue an item of `kind` (its payload a `RunEnded`) for every run that ends, for a `SHARED` task of the plugin's.

    Queued in the transaction that records the end, so every end that commits is queued, once, and no other is.
    """
    RUN_END_WORK.setdefault(app, set).add(str(kind))


def connect_run_end_work(app: Dash) -> None:
    """Tell `app`'s store what every run's end queues: by `app()`, once every plugin has plugged, in whatever order."""
    if kinds := RUN_END_WORK.find(app):
        get_system_task_store(app).queue_on_run_end(frozenset(kinds))


def get_system_task_store(app: Dash | None = None) -> TaskStore:
    """The task store, as the system: for the custodian, and for health checks no user is behind."""
    store = DATA_STORE.get(app)
    if not isinstance(store, TaskStore):
        msg = f"{type(store).__name__} keeps no background task state (it is not a TaskStore)"
        raise TypeError(msg)
    return store


def _forbid(what: str) -> NoReturn:
    msg = f"Not allowed: {what} is the custodian's"
    raise PermissionError(msg)


class AuthorizingTaskStore(TaskStore):
    """
    The task store as a request may use it: read by whoever may see the Trash (an admin, where grants are enforced).

    Writing is the custodian's alone, through `get_system_task_store`: no request starts, finishes or claims work.
    """

    def __init__(
        self,
        inner: TaskStore,
        actor: User,
        *,
        enforce_grants: bool,
        rules: Mapping[str, WorkRule] | None = None,
    ) -> None:
        self._inner = inner
        self._actor = actor
        self._enforce = enforce_grants
        self._rules = rules or {}

    def _require_read(self) -> None:
        if self._enforce:
            require_scope(self._actor, Scope.RESTORE)

    def ensure_tasks(self, names: Collection[str]) -> None:  # noqa: ARG002
        _forbid("adding a task")

    def claim_task(self, name: str, holder: str, *, until: datetime) -> bool:  # noqa: ARG002
        _forbid("claiming a task")

    def release_task(self, name: str, holder: str) -> None:  # noqa: ARG002
        _forbid("releasing a task")

    def start_task_run(self, name: str) -> None:  # noqa: ARG002
        _forbid("running a task")

    def finish_task_run(
        self,
        name: str,  # noqa: ARG002
        outcome: TaskOutcome,  # noqa: ARG002
        *,
        error: str | None,  # noqa: ARG002
        progress: dict[str, Any] | None,  # noqa: ARG002
    ) -> None:
        _forbid("running a task")

    def record_task_progress(self, name: str, progress: dict[str, Any]) -> None:  # noqa: ARG002
        _forbid("running a task")

    def request_task_run(self, name: str) -> None:  # noqa: ARG002
        _forbid("asking for a task's run")

    def task_states(self) -> list[TaskState]:
        self._require_read()
        return self._inner.task_states()

    def beat(self, holder: str, *, host: str, pid: int, started_at: datetime) -> None:  # noqa: ARG002
        _forbid("a heartbeat")

    def custodians(self, *, seen_since: datetime) -> list[Custodian]:
        self._require_read()
        return self._inner.custodians(seen_since=seen_since)

    def enqueue_work(self, items: Iterable[NewWorkItem]) -> None:
        """Queue work of a kind a plugin lets a request queue (`register_work_kind`), holding its scope; ask for its task."""
        rules: dict[str, WorkRule] = {}
        queued: list[NewWorkItem] = []
        for item in items:
            if (rule := rules.get(item.kind)) is None:
                if (rule := self._rules.get(item.kind)) is None:
                    _forbid(f"queueing {item.kind} work")
                require_scope(self._actor, rule.scope)
                rules[item.kind] = rule
            if rule.actor_field is not None:
                item = item.model_copy(update={"payload": {**item.payload, rule.actor_field: self._actor.id}})
            queued.append(item)
        self._inner.enqueue_work(queued)
        for task in {rule.task for rule in rules.values()}:
            self._inner.request_task_run(task)

    def queue_on_run_end(self, kinds: frozenset[str]) -> None:  # noqa: ARG002
        _forbid("choosing what a run's end queues")

    def claim_work(self, kind: str, holder: str, *, limit: int, until: datetime) -> list[WorkItem]:  # noqa: ARG002
        _forbid("claiming work")

    def renew_work(self, holder: str, *, until: datetime) -> None:  # noqa: ARG002
        _forbid("claiming work")

    def complete_work(self, item_ids: Collection[int]) -> None:  # noqa: ARG002
        _forbid("finishing work")

    def retry_work(self, item_id: int, *, error: str, due_at: datetime) -> None:  # noqa: ARG002
        _forbid("retrying work")

    def count_work(self, kind: str) -> int:
        self._require_read()
        return self._inner.count_work(kind)

    def queued_keys(self, kind: str, keys: Collection[str]) -> frozenset[str]:
        self._require_read()
        return self._inner.queued_keys(kind, keys)


def get_task_store() -> TaskStore:
    """The task store, as the in-flight request's user may read it."""
    if not has_request_context():
        msg = "get_task_store() is per-request; the custodian uses get_system_task_store()"
        raise RuntimeError(msg)
    app = current_dash_app()
    return AuthorizingTaskStore(
        get_system_task_store(app),
        get_current_user(),
        enforce_grants=get_auth_provider(app).verifies_identity,
        rules=WORK_RULES.find(app),
    )
