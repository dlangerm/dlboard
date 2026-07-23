"""Functions to use with a backing sqllite store."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Iterator

from dltrack import DataStore, Experiment, ExperimentID, Project
from dltrack._server.backend.queries import construct, create_table_sql, get_all, get_by_id, insert

if TYPE_CHECKING:
    from pathlib import Path

    from dltrack import NewExperiment, NewProject, ProjectID


class SQLLiteStore(DataStore):
    """Use a sqllite database as the data store."""

    def __init__(self, location: Path) -> None:
        self._location = location
        with sqlite3.connect(location) as conn:
            cur = conn.cursor()
            for table in {Experiment, Project}:
                cur.execute(create_table_sql(table))

    @classmethod
    def get_or_create(cls, loc: Path) -> SQLLiteStore:
        return cls(location=loc)

    def create_project(self, project: NewProject) -> Project:
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(insert(Project, project)).fetchone()
        return construct(Project, row)

    def get_project(self, database_id: ProjectID) -> Project:
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(get_by_id(Project, database_id)).fetchone()
        return construct(Project, row)

    def get_projects(self) -> Iterator[Project]:
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            for row in cur.execute(get_all(Project)):
                yield construct(Project, row)

    def create_experiment(self, experiment: NewExperiment) -> Experiment:
        """Create a new experiment."""
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(insert(Experiment, experiment)).fetchone()
        return construct(Experiment, row)

    def get_experiment(self, database_id: ExperimentID) -> Experiment:
        with sqlite3.connect(self._location) as conn:
            cur = conn.cursor()
            row = cur.execute(get_by_id(Experiment, database_id)).fetchone()
        return construct(Experiment, row)
