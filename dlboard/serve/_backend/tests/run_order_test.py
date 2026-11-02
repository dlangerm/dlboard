"""The order runs are listed in is total: runs created at the same instant come out newest first, the same way every time."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import sqlalchemy as sa

from dlboard import models

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_runs_created_at_the_same_instant_list_newest_id_first(
    store: SQLLiteStore, experiment_id: int
) -> None:
    ids = [store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(5)]
    runs = store._tables[models.Run]
    with store._engine.begin() as conn:
        conn.execute(
            sa.update(runs)
            .where(runs.c.experiment_id == experiment_id)
            .values(created_at=pendulum.datetime(2026, 1, 1, tz="UTC"))
        )

    listed = [run.id for run in store.get_runs(experiment_id, limit=None, run_ids=None)]

    assert listed == sorted(ids, reverse=True)
