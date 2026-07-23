"""Functions to use with a backing sqllite store."""

from __future__ import annotations

import sqlite3
import time
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from structlog.stdlib import get_logger

from dltrack import models
from dltrack.plugins.utilities._data_store import set_data_store
from dltrack.serve import sql

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash


_log = get_logger(__name__)


class SQLLiteStore(models.DataStore[Path]):
    """Use a sqllite database as the data store."""

    def __init__(self, location: Path) -> None:
        """Initialize."""
        self._location = location
        location.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(location) as conn:
            cur = conn.cursor()
            for table in {models.Experiment, models.Project, models.UnderlyingMetricTableEntry}:
                cur.execute(sql.create_table_sql(table))

        _log.info("initialized sqlite store at %s", location)

    @classmethod
    def get_or_create(cls, loc: Path) -> SQLLiteStore:
        """Create."""
        return cls(location=loc)

    def create_project(self, project: models.NewProject) -> models.Project:
        """create_project."""
        _log.info("Creating project with name %s", project.name)
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(sql.insert(models.Project, project)).fetchone()
        return sql.construct(models.Project, row)

    def get_project(self, database_id: int) -> models.Project:
        """Get project."""
        _log.info("getting project %s", database_id)
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(sql.get_by_id(models.Project, database_id)).fetchone()
        return sql.construct(models.Project, row)

    def get_projects(self) -> Iterator[models.Project]:
        """create_project."""
        _log.info("getting projects")
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            for row in cur.execute(sql.get_all(models.Project)):
                yield sql.construct(models.Project, row)

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        _log.info("Creating experiment for project %s", experiment.project_id)
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(sql.insert(models.Experiment, experiment)).fetchone()
        return sql.construct(models.Experiment, row)

    def get_experiment(self, database_id: int) -> models.Experiment:
        """Tfdsafs."""
        _log.info("Getting experiment id %s", database_id)
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(sql.get_by_id(models.Experiment, database_id)).fetchone()
        return sql.construct(models.Experiment, row)

    def get_experiments(self, project_id: int) -> Iterator[models.Experiment]:
        """Tfdsafs."""
        _log.info("Get experiments for project %s", project_id)
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            for row in cur.execute(sql.get_all_by_field(models.Experiment, "project_id", project_id)):
                yield sql.construct(models.Experiment, row)

    def get_all_experiments(self) -> Iterator[models.Experiment]:
        """Tfdsafs."""
        _log.info("getting all experiments for all projects")
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            for row in cur.execute(sql.get_all(models.Experiment)):
                yield sql.construct(models.Experiment, row)

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Tfdsafs."""
        _log.info("Logging metrics batch")
        t0 = time.perf_counter()
        underlying_values = chain(*(m.to_underlying() for m in metric))
        query, values = sql.insert_many(models.UnderlyingMetricTableEntry, underlying_values)
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            cur.executemany(query, values)
        t1 = time.perf_counter()
        _log.info("transaction took %.3f seconds", t1 - t0)

    def fetch_metrics(
        self,
        experiment_id: int,
        metric_name_match: str | None = None,
        step_range: slice[Any, Any, Any] | None = None,
    ) -> Iterator[models.LoggedMetrics]:
        """create_project."""
        if metric_name_match is not None or step_range is not None:
            raise NotImplementedError

        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            yield from models.LoggedMetrics.from_underlying(
                (
                    sql.construct(models.UnderlyingMetricTableEntry, m)
                    for m in cur.execute(
                        sql.get_all_by_field(
                            models.UnderlyingMetricTableEntry,
                            "experiment_id",
                            experiment_id,
                            order_by=["step"],
                        )
                    )
                )
            )


def get_plugin(store_location: Path) -> models.PluginProtocol:
    """Get the underlying plugin."""

    class Plugin:
        @classmethod
        def plug(cls, app: Dash) -> None:
            """Plugin content."""
            set_data_store(app, SQLLiteStore.get_or_create(store_location))

    return Plugin
