"""The admin page's Background tasks tab: which custodians are alive, and how each registered task has been doing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc
import pendulum

from dlboard.serve._custodian._health import custodial_health
from dlboard.serve._custodian._models import TaskOutcome

if TYPE_CHECKING:
    from datetime import datetime

    from dash.development.base_component import Component

    from dlboard.serve._custodian._models import TaskState
    from dlboard.serve._custodian._store import TaskStore
    from dlboard.serve._custodian._task import Task

_OUTCOME_COLORS: dict[TaskOutcome, str] = {
    TaskOutcome.SUCCESS: "teal",
    TaskOutcome.FAILURE: "red",
    TaskOutcome.HUNG: "orange",
}


def _ago(at: datetime | None) -> str:
    if at is None:
        return "never"
    return f"{pendulum.instance(at).diff_for_humans(pendulum.now('UTC'), absolute=True)} ago"


def _every(task: Task) -> str:
    return f"every {task.every.in_words()}" if task.every is not None else "when asked"


def _task_row(task: Task, state: TaskState | None, *, overdue: bool) -> Component:
    outcome = state.last_outcome if state is not None else None
    backlog = state.progress.get("backlog") if state is not None else None
    return dmc.TableTr(
        [
            dmc.TableTd(dmc.Text(str(task.name), ff="monospace", size="sm")),
            dmc.TableTd(f"{task.mode} · {_every(task)}", c="dimmed"),
            dmc.TableTd(
                dmc.Group(
                    [
                        dmc.Badge(outcome.value, color=_OUTCOME_COLORS[outcome], variant="light", size="sm")
                        if outcome is not None
                        else dmc.Text("not run yet", size="sm", c="dimmed"),
                        *([dmc.Badge("overdue", color="red", size="sm")] if overdue else []),
                    ],
                    gap=4,
                )
            ),
            dmc.TableTd(_ago(state.last_finished_at if state else None), c="dimmed"),
            dmc.TableTd(state.holder if state is not None and state.holder else "-", c="dimmed"),
            dmc.TableTd("-" if backlog is None else f"{backlog:,} left"),
            dmc.TableTd(
                dmc.Text(state.last_error.strip().splitlines()[-1], size="xs", c="red")
                if state is not None and state.last_error and outcome is not TaskOutcome.SUCCESS
                else "-"
            ),
        ]
    )


def render_background_tasks(store: TaskStore, tasks: list[Task]) -> Component:
    """The custodians alive, then every task the app's plugins registered, with how each last ran."""
    health = custodial_health(store, tasks)
    custodians = (
        dmc.Stack(
            [
                dmc.Text(
                    f"{c.holder} (pid {c.pid} on {c.host}): started {_ago(c.started_at)}, "
                    f"last seen {_ago(c.heartbeat_at)}",
                    size="sm",
                )
                for c in health.custodians
            ],
            gap=2,
        )
        if health.custodians
        else dmc.Alert(
            "No custodian process has checked in lately: no background task is running. The server starts one beside "
            "its workers (`dlboard serve`), unless DLBOARD_CUSTODIAN_ENABLED is off.",
            color="red",
            variant="light",
            title="No custodian",
        )
    )
    overdue = set(health.overdue)
    return dmc.Stack(
        [
            custodians,
            dmc.Table(
                [
                    dmc.TableThead(
                        dmc.TableTr(
                            [
                                dmc.TableTh(h)
                                for h in (
                                    "Task",
                                    "Runs",
                                    "Last run",
                                    "Finished",
                                    "Held by",
                                    "Backlog",
                                    "Error",
                                )
                            ]
                        )
                    ),
                    dmc.TableTbody(
                        [
                            _task_row(t, health.states.get(str(t.name)), overdue=str(t.name) in overdue)
                            for t in tasks
                        ]
                    ),
                ],
                striped=True,
                withTableBorder=True,
                verticalSpacing="xs",
            ),
        ],
        gap="md",
    )
