"""
A run whose process died without a word -- `kill -9`, the OOM killer, a lost node -- stops reading "running".

The server hears from a running run every few seconds (the batches it ships) and every minute at the least (its
heartbeat, `dlboard.client._heartbeat`). A background task, once a minute, gives up as lost each run still running that
has not been heard from in `DLBOARD_LOST_RUN_AFTER_S`: it is marked `UNKNOWN`, ended when it was last seen, with a
`lost_at` that the run list shows. Heard from again (a network partition healed, a suspended job resumed), it runs again.

A client from before heartbeats is heard from only through its batches, so one that goes quiet for longer (a very long
evaluation) is marked lost, and running again with its next batch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pendulum
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard._compat import StrEnum
from dlboard.serve import get_system_data_store
from dlboard.serve._custodian import Task, register_task

if TYPE_CHECKING:
    from dash import Dash

    from dlboard.serve._custodian import TaskContext

_PER_BATCH: Final = 500
"""How many runs one batch marks: the first run after an upgrade marks every long-dead run at once."""


class LostRunSettings(BaseSettings):
    """`DLBOARD_LOST_RUN_AFTER_S`: how long a running run may go unheard from before it is given up as lost."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    lost_run_after_s: int = Field(default=600, ge=180)
    """Seconds without a batch or a heartbeat; at least three heartbeats' worth, so a slow network loses none."""


class LostRunTask(StrEnum):
    """The plugin's task."""

    MARK = "lost_runs"


def mark_lost_runs(ctx: TaskContext) -> None:
    """Give up as lost, a batch at a time, every running run not heard from in `DLBOARD_LOST_RUN_AFTER_S`."""
    store = get_system_data_store(ctx.app)
    quiet_since = pendulum.now(pendulum.UTC) - pendulum.duration(seconds=LostRunSettings().lost_run_after_s)
    marked = 0
    while not ctx.should_stop():
        with ctx.batch():
            batch = store.mark_lost_runs(quiet_since, limit=_PER_BATCH)
        marked += batch
        if batch < _PER_BATCH:
            break
    ctx.report(marked=marked)


def plug(app: Dash) -> None:
    """Register the task that gives up as lost the runs that went quiet, once a minute."""
    register_task(
        app, Task(name=LostRunTask.MARK, run_once=mark_lost_runs, every=pendulum.duration(minutes=1))
    )
