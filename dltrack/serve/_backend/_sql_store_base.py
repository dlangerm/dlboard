"""The generic base class for a sql data store."""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Iterator

from pydantic import BaseModel
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve import sql

if TYPE_CHECKING:
    from collections.abc import Iterable


_log = get_logger(__name__)


class SQLStoreBase[T](ABC, models.DataStore[T]):
    """Use any sql-compatible database as the data store."""

    def __init__(self) -> None:
        """Initialize the underlying tables."""
        for table in {
            models.Project,
            models.Experiment,
            models.Run,
            models.UnderlyingMetricTableEntry,
            models.HyperParams,
            models.Page,
            models.Artifact,
        }:
            list(self._execute_raw_sql(sql.create_table_sql(table)))

        list(
            self._execute_raw_sql(
                sql.create_index_sql(
                    models.UnderlyingMetricTableEntry,
                    [k for k in models.UnderlyingMetricTableEntry.model_fields if k not in ("id", "value")],
                )
            )
        )

    @abstractmethod
    def _execute_raw_sql(
        self,
        statement: str,
        values: dict[str, Any] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""

    @abstractmethod
    def _execute_raw_sql_query_many(
        self,
        statement: str,
        values: Iterable[dict[str, Any]] | None = None,
    ) -> Iterator[tuple[Any, ...]]:
        """Execute raw sql."""

    def _execute_sql_query[RowT: BaseModel](
        self,
        expected_model: type[RowT],
        statement: str,
        values: dict[str, Any] | None = None,
        *,
        no_validate: bool = False,
    ) -> Iterator[RowT]:
        """Execute raw sql."""
        for row in self._execute_raw_sql(statement, values):
            yield sql.construct(expected_model, row, no_validate=no_validate)

    def _execute_sql_query_many[RowT: BaseModel](
        self,
        expected_model: type[RowT],
        statement: str,
        values: Iterable[dict[str, Any]],
        *,
        no_validate: bool = False,
    ) -> Iterator[RowT]:
        """Execute raw sql."""
        for row in self._execute_raw_sql_query_many(statement, values):
            yield sql.construct(expected_model, row, no_validate=no_validate)

    def _consume_row_iterator[RowT: BaseModel](self, row_iterator: Iterator[RowT]) -> list[RowT]:
        """Consume a row iterator and return a list of rows."""
        return list(row_iterator)

    def create_project(self, project: models.NewProject) -> models.Project:
        """create_project."""
        _log.info("Creating project with name %s", project.name)
        statement, values = sql.insert(models.Project, project)
        results = self._consume_row_iterator(self._execute_sql_query(models.Project, statement, values))
        return results[0]

    def get_project(self, database_id: int) -> models.Project:
        """Get project."""
        _log.info("getting project %s", database_id)
        return next(self._execute_sql_query(models.Project, sql.get_by_id(models.Project, database_id)))

    def get_projects(self) -> Iterator[models.Project]:
        """create_project."""
        _log.info("getting projects")
        yield from self._execute_sql_query(models.Project, sql.get_all(models.Project))

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        _log.info("Creating experiment for project %s", experiment.project_id)
        statement, values = sql.insert(models.Experiment, experiment)
        return self._consume_row_iterator(self._execute_sql_query(models.Experiment, statement, values))[0]

    def get_experiment(self, database_id: int) -> models.Experiment:
        """Tfdsafs."""
        _log.info("Getting experiment id %s", database_id)
        return next(self._execute_sql_query(models.Experiment, sql.get_by_id(models.Experiment, database_id)))

    def get_experiments(self, project_id: int) -> Iterator[models.Experiment]:
        """Tfdsafs."""
        _log.info("Get experiments for project %s", project_id)
        return self._execute_sql_query(
            models.Experiment, sql.get_all_by_field(models.Experiment, "project_id", project_id)
        )

    def create_run(self, run: models.NewRun) -> models.Run:
        statement, values = sql.insert(models.Run, run)
        return self._consume_row_iterator(self._execute_sql_query(models.Run, statement, values))[0]

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Tfdsafs."""
        _log.info("Logging metrics batch")
        self._consume_row_iterator(
            self._execute_sql_query_many(
                models.UnderlyingMetricTableEntry,
                *sql.insert_many(
                    models.UnderlyingMetricTableEntry,
                    itertools.chain(
                        *(m.to_underlying() for m in metric),
                    ),
                ),
                no_validate=True,
            )
        )

    def fetch_metrics(
        self,
        experiment_id: int,
        run_id: int | None = None,
        metric_name_match: set[str] | None = None,
        step_range: slice[Any, Any, Any] | None = None,
    ) -> Iterator[models.LoggedMetrics]:
        """create_project."""
        if step_range is not None or run_id is not None:
            raise NotImplementedError

        _log.info("Match fields %s", metric_name_match)

        yield from models.LoggedMetrics.from_underlying(
            self._execute_sql_query(
                models.UnderlyingMetricTableEntry,
                sql.get_all_by_field(
                    models.UnderlyingMetricTableEntry,
                    "experiment_id",
                    experiment_id,
                    match_field="key" if metric_name_match else None,
                    match_field_values=metric_name_match,  # pyright: ignore[reportArgumentType]
                    order_by=["run_id", "step"],
                ),
                no_validate=True,
            )
        )

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> None:
        """Log hyperparameters to the data store."""
        _log.info("Logging hyperparameters for experiment %s", hyperparams.experiment_id)
        existing = list(
            self._execute_sql_query(
                models.HyperParams,
                sql.get_all_by_field(
                    models.HyperParams,
                    "run_id",
                    hyperparams.run_id,
                ),
            )
        )

        if existing:
            _log.info(
                "Skipping duplicate hyperparameters for experiment %s run %s",
                hyperparams.experiment_id,
                hyperparams.run_id,
            )
            return
        statement, values = sql.insert(models.HyperParams, hyperparams)
        self._consume_row_iterator(self._execute_sql_query(models.HyperParams, statement, values))

    def fetch_hyperparams(self, experiment_id: int) -> Iterator[models.HyperParams]:
        """Fetch hyperparameters for a particular experiment."""
        _log.info("Fetching hyperparameters for experiment %s", experiment_id)
        yield from (
            self._execute_sql_query(
                models.HyperParams,
                sql.get_all_by_field(
                    models.HyperParams,
                    "experiment_id",
                    experiment_id,
                ),
            )
        )

    def get_or_create_page[D, P, C](
        self,
        page_type: type[models.Page[D, P, C]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[D, C]] | None = None,
    ) -> models.Page[D, P, C]:
        args = {
            "run_id": run_id,
            "experiment_id": experiment_id,
            "project_id": project_id,
        }

        if sum(1 if id_ is not None else 0 for id_ in args.values()) != 1:
            msg = "Exactly one of run, experiment, or project must be defined."
            raise ValueError(msg)

        maybe_insert_values = {k: v for k, v in args.items() if v is not None}

        field, value = maybe_insert_values.popitem()
        for row in self._execute_sql_query(
            page_type,
            sql.get_all_by_field(
                models.Page,
                field,
                value,
            ),
        ):
            _log.info("found row")
            return row

        _log.info("Inserting page model for %s = %s", field, value)
        statement, values = sql.insert(
            models.Page,
            (new_page_type or models.NewPage[D, C])(**{field: value}),  # pyright: ignore[reportArgumentType]
        )

        return self._consume_row_iterator(
            self._execute_sql_query(
                page_type,
                statement,
                values,
            )
        )[0]

    def update_page[D, P, C](self, page: models.Page[D, P, C]) -> models.Page[D, P, C]:
        statement, values = sql.update(models.Page, page)
        return self._consume_row_iterator(
            self._execute_sql_query(
                type(page),
                statement,
                values,
            ),
        )[0]

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        self._consume_row_iterator(
            self._execute_sql_query_many(
                models.Artifact,
                *sql.insert_many(
                    models.Artifact,
                    artifacts,
                ),
            )
        )

    def fetch_artifacts(
        self,
        keys: set[str] | None = None,
        run_id: int | None = None,
        experiment_id: int | None = None,
        step: int | None = None,
        fname: str | None = None,
    ) -> Iterator[models.Artifact]:
        """Get artifact metadata (not bytes) for a run or experiment."""
        if experiment_id is None:
            raise NotImplementedError
        if fname:
            raise NotImplementedError

        clauses = [f"experiment_id = {sql.escape_value_sql(experiment_id)}"]
        if keys:
            clauses.append(f"key in ({','.join(sql.escape_value_sql(k) for k in keys)})")
        if run_id is not None:
            clauses.append(f"run_id = {sql.escape_value_sql(run_id)}")
        if step is not None:
            clauses.append(f"step = {sql.escape_value_sql(step)}")

        statement = f"""
            SELECT * FROM {models.Artifact.__name__}
            WHERE {" AND ".join(clauses)}
            ORDER BY run_id, step;
        """
        return self._execute_sql_query(models.Artifact, statement, no_validate=True)
