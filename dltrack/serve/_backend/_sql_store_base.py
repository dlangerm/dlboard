"""The generic base class for a sql data store."""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Iterator

import pendulum
from pydantic import BaseModel
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve import sql
from dltrack.serve._backend._app_state import APP_STATE_ROW_ID, AppState

if TYPE_CHECKING:
    from collections.abc import Iterable


_log = get_logger(__name__)

# Declares which `*_id` columns are real foreign keys, and what they reference. Ordered so that
# a referenced table is always created before the table that references it (SQLite will accept
# forward references too, but keeping this in dependency order keeps the migration rebuild in
# `_migrations.py`, which walks this same map, easy to reason about).
FOREIGN_KEYS: dict[type[BaseModel], dict[str, type[BaseModel]]] = {
    models.Project: {"created_by": models.User, "deleted_by": models.User},
    models.Experiment: {
        "project_id": models.Project,
        "created_by": models.User,
        "deleted_by": models.User,
    },
    models.Run: {"experiment_id": models.Experiment, "created_by": models.User, "deleted_by": models.User},
    models.UnderlyingMetricTableEntry: {"experiment_id": models.Experiment, "run_id": models.Run},
    models.HyperParams: {"experiment_id": models.Experiment, "run_id": models.Run},
    models.Artifact: {
        "experiment_id": models.Experiment,
        "run_id": models.Run,
        "created_by": models.User,
        "deleted_by": models.User,
    },
    models.Page: {"run_id": models.Run, "experiment_id": models.Experiment, "project_id": models.Project},
}

UNIQUE_COLUMNS: dict[type[BaseModel], list[str]] = {
    models.User: ["username"],
}

# Dependency order: every table appears after the tables its foreign keys point to.
TABLES: tuple[type[BaseModel], ...] = (
    models.User,
    models.Project,
    models.Experiment,
    models.Run,
    models.UnderlyingMetricTableEntry,
    models.HyperParams,
    models.Artifact,
    models.Page,
)


def _resolve_page_scope(
    *, run_id: int | None, experiment_id: int | None, project_id: int | None
) -> tuple[str, int]:
    """
    Resolve the single scope (run, experiment, or project) a page belongs to.

    A page is always owned by exactly one of these three ids; this validates that
    invariant and returns the owning field name paired with its id.
    """
    scopes = {"run_id": run_id, "experiment_id": experiment_id, "project_id": project_id}
    owned = {field: id_ for field, id_ in scopes.items() if id_ is not None}
    if len(owned) != 1:
        msg = "Exactly one of run, experiment, or project must be defined."
        raise ValueError(msg)
    return next(iter(owned.items()))


class SQLStoreBase[T](ABC, models.DataStore[T]):
    """Use any sql-compatible database as the data store."""

    def __init__(self) -> None:
        """Initialize the underlying tables."""
        list(self._execute_raw_sql(sql.create_table_sql(AppState)))
        list(
            self._execute_raw_sql(
                f"INSERT OR IGNORE INTO {AppState.__name__} (id, bootstrap_admin_assigned) "
                f"VALUES ({APP_STATE_ROW_ID}, 0);"
            )
        )

        for table in TABLES:
            list(
                self._execute_raw_sql(
                    sql.create_table_sql(table, FOREIGN_KEYS.get(table), UNIQUE_COLUMNS.get(table))
                )
            )

        self._run_migrations()

        list(
            self._execute_raw_sql(
                sql.create_index_sql(
                    models.UnderlyingMetricTableEntry,
                    [k for k in models.UnderlyingMetricTableEntry.model_fields if k not in ("id", "value")],
                )
            )
        )
        list(
            self._execute_raw_sql(
                sql.create_index_sql(
                    models.Artifact, [k for k in models.Artifact.model_fields if k not in ("id", "tags")]
                )
            )
        )

    @abstractmethod
    def _run_migrations(self) -> None:
        """
        Bring the underlying schema up to date.

        Called once at startup, after the initial `CREATE TABLE IF NOT EXISTS` pass, so a brand
        new database already has every column/constraint and this is a no-op, while an existing
        database gets migrated forward. Implementations must hard-fail (raise) rather than swallow
        errors if a migration can't be applied cleanly.
        """

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

    @abstractmethod
    def _execute_in_transaction(
        self, statements: Iterable[tuple[str, dict[str, Any] | None]]
    ) -> list[list[tuple[Any, ...]]]:
        """
        Execute several statements as one atomic transaction, returning each statement's rows.

        All-or-nothing: used for cascading soft-delete/restore/purge across multiple tables, where
        a failure partway through must not leave e.g. a project deleted but its experiments intact.
        """

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

    def _first_committed_row[RowT: BaseModel](self, rows: Iterator[RowT]) -> RowT:
        """
        Return the first row of a write query, forcing the underlying transaction to commit.

        `_execute_raw_sql` implementations (e.g. sqlite's) run inside a `with` block that
        only commits once its generator is fully drained; taking just `next(rows)` would
        leave the generator suspended mid-write and the change uncommitted.
        """
        return list(rows)[0]  # noqa: RUF015 -- must fully drain `rows` to commit; see docstring

    def _fetch_deleted_at(self, table: type[BaseModel], entity_id: int) -> str | None:
        """Return the raw `deleted_at` value for a row, or raise if the row doesn't exist at all."""
        rows = list(
            self._execute_raw_sql(
                f"SELECT deleted_at FROM {table.__name__} WHERE id = :id", {"id": entity_id}
            )
        )
        if not rows:
            msg = f"{table.__name__} {entity_id} does not exist"
            raise ValueError(msg)
        return rows[0][0]

    def _ensure_not_deleted(self, table: type[BaseModel], entity_id: int) -> None:
        """Raise if `entity_id` doesn't exist, or has been soft-deleted."""
        if self._fetch_deleted_at(table, entity_id) is not None:
            msg = f"{table.__name__} {entity_id} has been deleted"
            raise ValueError(msg)

    def get_or_create_user(self, username: str) -> models.User:
        """
        Get the user for `username`, creating it if this is the first time it's been seen.

        A brand new user is granted no scopes. The exception is the very first user any store
        instance ever creates: it atomically claims a one-time "bootstrap admin" grant
        (`Scope.ALL`), via `AppState.bootstrap_admin_assigned`, so a single local user gets full
        permissions with zero configuration. Every user after that gets nothing, so this never
        silently generalizes into "everyone who connects is admin" if the same database ends up
        shared by more than one person.
        """
        existing = list(
            self._execute_sql_query(models.User, sql.get_all_by_field(models.User, "username", username))
        )
        if existing:
            return existing[0]

        insert_statement, insert_values = sql.insert_or_ignore(models.User, models.NewUser(username=username))
        list(self._execute_sql_query(models.User, insert_statement, insert_values))
        user = next(
            self._execute_sql_query(models.User, sql.get_all_by_field(models.User, "username", username))
        )

        # A single `UPDATE ... WHERE bootstrap_admin_assigned = 0` is atomic under SQLite's
        # serialized writes: if two processes race this for the first time simultaneously, only one
        # can ever see (and flip) the flag while it's still 0.
        claimed = list(
            self._execute_raw_sql(
                f"UPDATE {AppState.__name__} SET bootstrap_admin_assigned = 1 "
                f"WHERE id = {APP_STATE_ROW_ID} AND bootstrap_admin_assigned = 0 RETURNING id;"
            )
        )
        if not claimed:
            return user

        _log.info("Granting bootstrap admin scopes to user %s (%s)", user.id, username)
        update_statement, update_values = sql.update(
            models.User, user.model_copy(update={"scopes": [models.Scope.ALL]})
        )
        return self._first_committed_row(
            self._execute_sql_query(models.User, update_statement, update_values)
        )

    def create_project(self, project: models.NewProject) -> models.Project:
        """Create a new project."""
        _log.debug("Creating project with name %s", project.name)
        statement, values = sql.insert(models.Project, project)
        return self._first_committed_row(self._execute_sql_query(models.Project, statement, values))

    def get_project(self, database_id: int) -> models.Project:
        """Get project."""
        _log.debug("getting project %s", database_id)
        return next(
            self._execute_sql_query(
                models.Project, sql.get_by_id(models.Project, database_id, exclude_deleted=True)
            )
        )

    def get_projects(self) -> Iterator[models.Project]:
        """Get all (non-deleted) projects."""
        _log.debug("getting projects")
        yield from self._execute_sql_query(models.Project, sql.get_all(models.Project, exclude_deleted=True))

    def update_project(self, project: models.Project) -> models.Project:
        """Update a project."""
        _log.debug("updating project %s", project.id)
        statement, values = sql.update(models.Project, project)
        return self._first_committed_row(self._execute_sql_query(models.Project, statement, values))

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        _log.info("Creating experiment for project %s", experiment.project_id)
        self._ensure_not_deleted(models.Project, experiment.project_id)
        statement, values = sql.insert(models.Experiment, experiment)
        return self._first_committed_row(self._execute_sql_query(models.Experiment, statement, values))

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        """Get an experiment by id, or None if it doesn't exist (or has been deleted)."""
        _log.debug("Getting experiment id %s", database_id)
        try:
            return next(
                self._execute_sql_query(
                    models.Experiment, sql.get_by_id(models.Experiment, database_id, exclude_deleted=True)
                )
            )
        except StopIteration:
            return None

    def get_experiments(self, project_id: int) -> Iterator[models.Experiment]:
        """Get all (non-deleted) experiments belonging to a project."""
        _log.debug("Get experiments for project %s", project_id)
        return self._execute_sql_query(
            models.Experiment,
            sql.get_all_by_field(models.Experiment, "project_id", project_id, exclude_deleted=True),
        )

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        """Update an experiment."""
        _log.debug("updating experiment %s", experiment.id)
        statement, values = sql.update(models.Experiment, experiment)
        return self._first_committed_row(self._execute_sql_query(models.Experiment, statement, values))

    def create_run(self, run: models.NewRun) -> models.Run:
        self._ensure_not_deleted(models.Experiment, run.experiment_id)
        statement, values = sql.insert(models.Run, run)
        return self._first_committed_row(self._execute_sql_query(models.Run, statement, values))

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log a batch of metrics to the data store."""
        _log.debug("Logging metrics batch")
        metric = list(metric)
        for run_id in {m.run_id for m in metric}:
            self._ensure_not_deleted(models.Run, run_id)
        list(
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
        """Fetch logged metrics for an experiment, optionally filtered by metric name."""
        if step_range is not None or run_id is not None:
            raise NotImplementedError

        _log.debug("Match fields %s", metric_name_match)

        # `UnderlyingMetricTableEntry` has no `deleted_at` of its own -- visibility is inherited
        # transitively through its run, which is always soft-deleted in the same cascade as its
        # metrics' logical owner (see `_soft_delete`), so a join against `Run` is sufficient.
        clauses = [f"m.experiment_id = {sql.escape_value_sql(experiment_id)}", "r.deleted_at IS NULL"]
        if metric_name_match:
            clauses.append(f"m.key in ({','.join(sql.escape_value_sql(k) for k in metric_name_match)})")
        statement = f"""
            SELECT m.* FROM {models.UnderlyingMetricTableEntry.__name__} m
            JOIN {models.Run.__name__} r ON m.run_id = r.id
            WHERE {" AND ".join(clauses)}
            ORDER BY m.run_id, m.step;
        """

        yield from models.LoggedMetrics.from_underlying(
            self._execute_sql_query(models.UnderlyingMetricTableEntry, statement, no_validate=True)
        )

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters to the data store."""
        _log.debug("Logging hyperparameters for experiment %s", hyperparams.experiment_id)
        self._ensure_not_deleted(models.Run, hyperparams.run_id)
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
            _log.warning(
                "Skipping duplicate hyperparameters for experiment %s run %s",
                hyperparams.experiment_id,
                hyperparams.run_id,
            )
            return existing[0]
        statement, values = sql.insert(models.HyperParams, hyperparams)
        return self._first_committed_row(self._execute_sql_query(models.HyperParams, statement, values))

    def fetch_hyperparams(self, experiment_id: int) -> Iterator[models.HyperParams]:
        """Fetch hyperparameters for a particular experiment."""
        _log.debug("Fetching hyperparameters for experiment %s", experiment_id)
        statement = f"""
            SELECT h.* FROM {models.HyperParams.__name__} h
            JOIN {models.Run.__name__} r ON h.run_id = r.id
            WHERE h.experiment_id = {sql.escape_value_sql(experiment_id)} AND r.deleted_at IS NULL;
        """
        yield from self._execute_sql_query(models.HyperParams, statement)

    def get_or_create_page[D, P, C](
        self,
        page_type: type[models.Page[D, P, C]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[D, C]] | None = None,
    ) -> models.Page[D, P, C]:
        field, value = _resolve_page_scope(run_id=run_id, experiment_id=experiment_id, project_id=project_id)
        for row in self._execute_sql_query(
            page_type,
            sql.get_all_by_field(
                models.Page,
                field,
                value,
            ),
        ):
            return row

        _log.warning("Inserting new page model for %s = %s", field, value)
        statement, values = sql.insert(
            models.Page,
            (new_page_type or models.NewPage[D, C])(**{field: value}),  # pyright: ignore[reportArgumentType]
        )

        return self._first_committed_row(
            self._execute_sql_query(
                page_type,
                statement,
                values,
            )
        )

    def update_page[D, P, C](self, page: models.Page[D, P, C]) -> models.Page[D, P, C]:
        statement, values = sql.update(models.Page, page)
        return self._first_committed_row(
            self._execute_sql_query(
                type(page),
                statement,
                values,
            ),
        )

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        artifacts = list(artifacts)
        for run_id in {a.run_id for a in artifacts}:
            self._ensure_not_deleted(models.Run, run_id)
        list(
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
        """Get artifact metadata (not bytes) for a run or experiment. Excludes deleted artifacts."""
        if experiment_id is None:
            raise NotImplementedError
        if fname:
            raise NotImplementedError

        clauses = [f"experiment_id = {sql.escape_value_sql(experiment_id)}", "deleted_at IS NULL"]
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

    # -- Soft-delete / restore / purge -----------------------------------------------------------
    #
    # Only Project, Experiment, Run, and Artifact carry `deleted_at`/`deleted_by` -- metrics and
    # hyperparameters have no delete column of their own and are hidden transitively via a join
    # against their run (see `fetch_metrics`/`fetch_hyperparams`), since a run under a deleted
    # experiment/project is always itself soft-deleted by the same cascade.

    def _soft_delete(
        self,
        table: type[BaseModel],
        entity_id: int,
        actor_id: int,
        *,
        cascade: Iterable[tuple[type[BaseModel], str]] = (),
    ) -> None:
        """
        Soft-delete one row and cascade the same delete to dependent tables.

        `cascade` is a list of `(child table, WHERE clause)` pairs; the WHERE clause may reference
        `:id` (the id of the row being deleted). Only rows not already independently deleted are
        touched, so a child soft-deleted earlier on its own keeps its original `deleted_at`.
        """
        now = pendulum.now(pendulum.UTC).isoformat()
        statements = [
            (
                f"UPDATE {table.__name__} SET deleted_at = :now, deleted_by = :actor "
                "WHERE id = :id AND deleted_at IS NULL RETURNING id",
                {"now": now, "actor": actor_id, "id": entity_id},
            ),
            *(
                (
                    f"UPDATE {child.__name__} SET deleted_at = :now, deleted_by = :actor "
                    f"WHERE {where_clause} AND deleted_at IS NULL",
                    {"now": now, "actor": actor_id, "id": entity_id},
                )
                for child, where_clause in cascade
            ),
        ]
        results = self._execute_in_transaction(statements)
        if not results[0]:
            msg = f"{table.__name__} {entity_id} does not exist or is already deleted"
            raise ValueError(msg)

    def _restore(
        self,
        table: type[BaseModel],
        entity_id: int,
        *,
        cascade: Iterable[tuple[type[BaseModel], str]] = (),
    ) -> None:
        """
        Restore one soft-deleted row and cascade to dependents deleted at the exact same instant.

        Matching on the exact `deleted_at` timestamp (stamped once, atomically, across a whole
        delete cascade in `_soft_delete`) means a child that was independently soft-deleted at a
        different time -- before or after its parent -- keeps its own deletion and isn't
        accidentally resurrected just because an ancestor is being restored.
        """
        deleted_at = self._fetch_deleted_at(table, entity_id)
        if deleted_at is None:
            msg = f"{table.__name__} {entity_id} is not deleted"
            raise ValueError(msg)

        statements = [
            (
                f"UPDATE {table.__name__} SET deleted_at = NULL, deleted_by = NULL WHERE id = :id",
                {"id": entity_id},
            ),
            *(
                (
                    f"UPDATE {child.__name__} SET deleted_at = NULL, deleted_by = NULL "
                    f"WHERE {where_clause} AND deleted_at = :deleted_at",
                    {"id": entity_id, "deleted_at": deleted_at},
                )
                for child, where_clause in cascade
            ),
        ]
        self._execute_in_transaction(statements)

    def _purge(
        self,
        table: type[BaseModel],
        entity_id: int,
        actor: models.User,
        *,
        cascade: Iterable[str] = (),
    ) -> None:
        """
        Permanently delete one already-soft-deleted row and everything cascade-dependent on it.

        The one irreversible action in this module -- everything else (soft-delete, restore) can
        be undone -- so it's the one place that actually gates on a scope (`Scope.PURGE`) rather
        than just recording who did it. `cascade` is a list of full `DELETE ...` statements
        (dependency order: children before parents) that may reference `:id`.
        """
        if not models.has_scope(actor, models.Scope.PURGE):
            msg = f"User {actor.id} lacks the {models.Scope.PURGE} scope"
            raise PermissionError(msg)

        if self._fetch_deleted_at(table, entity_id) is None:
            msg = f"{table.__name__} {entity_id} must be soft-deleted before it can be purged"
            raise ValueError(msg)

        statements = [(statement, {"id": entity_id}) for statement in cascade]
        statements.append((f"DELETE FROM {table.__name__} WHERE id = :id", {"id": entity_id}))
        self._execute_in_transaction(statements)

    def delete_project(self, project_id: int, actor_id: int) -> None:
        """Soft-delete a project and cascade to its experiments, runs, and artifacts."""
        experiments_of_project = f"(SELECT id FROM {models.Experiment.__name__} WHERE project_id = :id)"
        self._soft_delete(
            models.Project,
            project_id,
            actor_id,
            cascade=[
                (models.Experiment, "project_id = :id"),
                (models.Run, f"experiment_id IN {experiments_of_project}"),
                (models.Artifact, f"experiment_id IN {experiments_of_project}"),
            ],
        )

    def restore_project(self, project_id: int) -> None:
        """Restore a soft-deleted project and every experiment/run/artifact deleted with it."""
        experiments_of_project = f"(SELECT id FROM {models.Experiment.__name__} WHERE project_id = :id)"
        self._restore(
            models.Project,
            project_id,
            cascade=[
                (models.Experiment, "project_id = :id"),
                (models.Run, f"experiment_id IN {experiments_of_project}"),
                (models.Artifact, f"experiment_id IN {experiments_of_project}"),
            ],
        )

    def purge_project(self, project_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted project and everything under it."""
        experiments_of_project = f"(SELECT id FROM {models.Experiment.__name__} WHERE project_id = :id)"
        runs_of_project = (
            f"(SELECT id FROM {models.Run.__name__} WHERE experiment_id IN {experiments_of_project})"
        )
        self._purge(
            models.Project,
            project_id,
            actor,
            cascade=[
                f"DELETE FROM {models.UnderlyingMetricTableEntry.__name__} "
                f"WHERE experiment_id IN {experiments_of_project}",
                f"DELETE FROM {models.HyperParams.__name__} WHERE experiment_id IN {experiments_of_project}",
                f"DELETE FROM {models.Artifact.__name__} WHERE experiment_id IN {experiments_of_project}",
                f"DELETE FROM {models.Page.__name__} WHERE project_id = :id "
                f"OR experiment_id IN {experiments_of_project} OR run_id IN {runs_of_project}",
                f"DELETE FROM {models.Run.__name__} WHERE experiment_id IN {experiments_of_project}",
                f"DELETE FROM {models.Experiment.__name__} WHERE project_id = :id",
            ],
        )

    def delete_experiment(self, experiment_id: int, actor_id: int) -> None:
        """Soft-delete an experiment and cascade to its runs and artifacts."""
        self._soft_delete(
            models.Experiment,
            experiment_id,
            actor_id,
            cascade=[(models.Run, "experiment_id = :id"), (models.Artifact, "experiment_id = :id")],
        )

    def restore_experiment(self, experiment_id: int) -> None:
        """Restore a soft-deleted experiment and every run/artifact deleted with it."""
        self._restore(
            models.Experiment,
            experiment_id,
            cascade=[(models.Run, "experiment_id = :id"), (models.Artifact, "experiment_id = :id")],
        )

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted experiment and everything under it."""
        runs_of_experiment = f"(SELECT id FROM {models.Run.__name__} WHERE experiment_id = :id)"
        self._purge(
            models.Experiment,
            experiment_id,
            actor,
            cascade=[
                f"DELETE FROM {models.UnderlyingMetricTableEntry.__name__} WHERE experiment_id = :id",
                f"DELETE FROM {models.HyperParams.__name__} WHERE experiment_id = :id",
                f"DELETE FROM {models.Artifact.__name__} WHERE experiment_id = :id",
                f"DELETE FROM {models.Page.__name__} WHERE experiment_id = :id OR run_id IN {runs_of_experiment}",
                f"DELETE FROM {models.Run.__name__} WHERE experiment_id = :id",
            ],
        )

    def delete_run(self, run_id: int, actor_id: int) -> None:
        """Soft-delete a run and cascade to its artifacts."""
        self._soft_delete(models.Run, run_id, actor_id, cascade=[(models.Artifact, "run_id = :id")])

    def restore_run(self, run_id: int) -> None:
        """Restore a soft-deleted run and every artifact deleted with it."""
        self._restore(models.Run, run_id, cascade=[(models.Artifact, "run_id = :id")])

    def purge_run(self, run_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted run and everything under it."""
        self._purge(
            models.Run,
            run_id,
            actor,
            cascade=[
                f"DELETE FROM {models.UnderlyingMetricTableEntry.__name__} WHERE run_id = :id",
                f"DELETE FROM {models.HyperParams.__name__} WHERE run_id = :id",
                f"DELETE FROM {models.Artifact.__name__} WHERE run_id = :id",
                f"DELETE FROM {models.Page.__name__} WHERE run_id = :id",
            ],
        )

    def delete_artifact(self, artifact_id: int, actor_id: int) -> None:
        """Soft-delete a single artifact (a leaf -- nothing depends on it)."""
        self._soft_delete(models.Artifact, artifact_id, actor_id)

    def restore_artifact(self, artifact_id: int) -> None:
        """Restore a soft-deleted artifact."""
        self._restore(models.Artifact, artifact_id)

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted artifact."""
        self._purge(models.Artifact, artifact_id, actor)

    def list_deleted_projects(self) -> Iterator[models.Project]:
        """List soft-deleted projects, for a trash/admin view."""
        yield from self._execute_sql_query(
            models.Project, f"SELECT * FROM {models.Project.__name__} WHERE deleted_at IS NOT NULL"
        )

    def list_deleted_experiments(self) -> Iterator[models.Experiment]:
        """List soft-deleted experiments, for a trash/admin view."""
        yield from self._execute_sql_query(
            models.Experiment, f"SELECT * FROM {models.Experiment.__name__} WHERE deleted_at IS NOT NULL"
        )

    def list_deleted_runs(self) -> Iterator[models.Run]:
        """List soft-deleted runs, for a trash/admin view."""
        yield from self._execute_sql_query(
            models.Run, f"SELECT * FROM {models.Run.__name__} WHERE deleted_at IS NOT NULL"
        )

    def list_deleted_artifacts(self) -> Iterator[models.Artifact]:
        """List soft-deleted artifacts, for a trash/admin view."""
        yield from self._execute_sql_query(
            models.Artifact, f"SELECT * FROM {models.Artifact.__name__} WHERE deleted_at IS NOT NULL"
        )
