"""
Background tasks: declared by plugins, run by the custodian process the server starts beside its web workers.

A plugin registers a `Task` in `plug()` (`register_task`) instead of starting a thread of its own. The server's master
starts one custodian process, which runs every registered task (`Supervisor`), and keeps it alive (`CustodianProcess`);
the database holds what every custodian is doing (`TaskStore`), which the admin page and `/healthz/custodian` read.
"""

from dlboard.serve._custodian._models import NewWorkItem, TaskOutcome, WorkItem
from dlboard.serve._custodian._role import in_custodian
from dlboard.serve._custodian._store import (
    TaskStore,
    WorkRule,
    get_system_task_store,
    get_task_store,
    register_work_kind,
)
from dlboard.serve._custodian._task import (
    Task,
    TaskContext,
    TaskMode,
    register_task,
    registered_tasks,
)

__all__ = [
    "NewWorkItem",
    "Task",
    "TaskContext",
    "TaskMode",
    "TaskOutcome",
    "TaskStore",
    "WorkItem",
    "WorkRule",
    "get_system_task_store",
    "get_task_store",
    "in_custodian",
    "register_task",
    "register_work_kind",
    "registered_tasks",
]
