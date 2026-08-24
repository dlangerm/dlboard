"""The generic base class for a sql data store."""

from __future__ import annotations

import itertools
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Iterator

import pendulum
from pydantic import BaseModel
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve import sql
from dltrack.serve._backend._app_state import APP_STATE_ROW_ID, AppState
from dltrack.serve._backend._foreign_keys import ForeignKey, ForeignKeyKind

if TYPE_CHECKING:
    from collections.abc import Iterable


_log = get_logger(__name__)

_OWNS = ForeignKeyKind.OWNERSHIP

# Declares every column's foreign key relationship: what it references, and whether it's OWNERSHIP
# (the parent in the delete/restore/purge hierarchy -- gets a real `ON DELETE CASCADE`, and is what
# `_cascade_targets` walks) or plain ATTRIBUTION (e.g. `created_by`, never cascades). This is the
# single source of truth for both SQL schema generation and cascade behavior -- see
# `_foreign_keys.py` -- so a new owned table only ever needs one entry here, not one hardcoded
# cascade list per delete/restore/purge entry point.
#
# Ordered so that a referenced table is always created before the table that references it (SQLite
# accepts forward references too, but this keeps the migration rebuild in `_migrations.py`, which
# walks this same map, easy to reason about).
FOREIGN_KEYS: dict[type[BaseModel], dict[str, ForeignKey]] = {
    models.Project: {
        "created_by": ForeignKey(models.User),
        "deleted_by": ForeignKey(models.User),
    },
    models.Experiment: {
        "project_id": ForeignKey(models.Project, _OWNS),
        "created_by": ForeignKey(models.User),
        "deleted_by": ForeignKey(models.User),
    },
    models.Run: {
        "experiment_id": ForeignKey(models.Experiment, _OWNS),
        "created_by": ForeignKey(models.User),
        "deleted_by": ForeignKey(models.User),
    },
    models.UnderlyingMetricTableEntry: {
        "experiment_id": ForeignKey(models.Experiment),  # denormalized convenience, not an ownership edge
        "run_id": ForeignKey(models.Run, _OWNS),
    },
    models.HyperParams: {
        "experiment_id": ForeignKey(models.Experiment),
        "run_id": ForeignKey(models.Run, _OWNS),
    },
    models.Artifact: {
        "experiment_id": ForeignKey(models.Experiment),
        "run_id": ForeignKey(models.Run, _OWNS),
        "created_by": ForeignKey(models.User),
        "deleted_by": ForeignKey(models.User),
    },
    models.Page: {
        "run_id": ForeignKey(models.Run, _OWNS),
        "experiment_id": ForeignKey(models.Experiment, _OWNS),
        "project_id": ForeignKey(models.Project, _OWNS),
    },
    models.AuditLogEntry: {"user_id": ForeignKey(models.User)},
    models.ArtifactPurgeTask: {"requested_by": ForeignKey(models.User)},
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
    models.AuditLogEntry,
    models.ArtifactPurgeTask,
)

# The entities a user can soft-delete/restore -- the only tables with `deleted_at`/`deleted_by`.
# Metrics, hyperparameters, and pages aren't in this set even though they're cascade-owned (see
# `FOREIGN_KEYS`): they have no `deleted_at` column of their own, so `_cascade_targets` is asked to
# stop descending into them for soft-delete/restore purposes -- purge (a hard delete) still reaches
# them, via `ON DELETE CASCADE`, without needing this restriction.
SOFT_DELETABLE: frozenset[type[BaseModel]] = frozenset(
    {models.Project, models.Experiment, models.Run, models.Artifact}
)


def _cascade_targets(
    root: type[BaseModel], *, within: frozenset[type[BaseModel]] | None = None
) -> list[tuple[type[BaseModel], str]]:
    """
    Breadth-first walk of the `OWNERSHIP` foreign-key graph (see `FOREIGN_KEYS`) starting at `root`.

    Returns every descendant table paired with a SQL WHERE clause (referencing `:id`, the root
    row's id) that selects that table's rows under `root`. `within`, if given, prunes the walk to
    only tables in that set (e.g. `SOFT_DELETABLE`) -- a table outside it is skipped entirely,
    neither returned nor recursed into, since it and everything under it are irrelevant to why
    `within` was passed (e.g. `Page` has no `deleted_at` to filter on, so a soft-delete/restore
    cascade must never touch it even though it's directly owned by `Project`).

    This is the single place "what belongs to what" is resolved for soft-delete/restore cascades
    and for purge's audit-log counts: adding a new owned table to `FOREIGN_KEYS` is enough for it to
    participate correctly everywhere this is used, with no other code to update.
    """
    edges = [
        (child, column, fk.references)
        for child, columns in FOREIGN_KEYS.items()
        for column, fk in columns.items()
        if fk.kind is ForeignKeyKind.OWNERSHIP
    ]
    results: list[tuple[type[BaseModel], str]] = []
    frontier: list[tuple[type[BaseModel], str]] = [(root, "id = :id")]
    while frontier:
        parent, parent_where = frontier.pop(0)
        for child, column, parent_table in edges:
            if parent_table is not parent:
                continue
            if within is not None and child not in within:
                continue
            where = (
                f"{column} = :id"
                if parent is root
                else f"{column} IN (SELECT id FROM {parent.__name__} WHERE {parent_where})"
            )
            results.append((child, where))
            frontier.append((child, where))
    return results


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

    def _count_matching(
        self,
        table: type[BaseModel],
        where_clause: str,
        entity_id: int,
        extra_clause: str = "",
        extra_values: dict[str, Any] | None = None,
    ) -> int:
        """
        Count rows in `table` matching `where_clause` (which may reference `:id`), plus `extra_clause`.

        `extra_clause` may itself reference bind parameters (e.g. `:deleted_at`) supplied via
        `extra_values` -- kept as separate parameters rather than interpolated into the SQL text.
        """
        rows = list(
            self._execute_raw_sql(
                f"SELECT count(*) FROM {table.__name__} WHERE {where_clause}{extra_clause}",
                {"id": entity_id, **(extra_values or {})},
            )
        )
        return rows[0][0]

    def _record_audit_log(
        self,
        actor_id: int,
        action: models.AuditAction,
        entity_type: models.EntityType,
        entity_id: int,
        details: dict[str, int],
    ) -> tuple[str, dict[str, Any]]:
        """Build the (statement, values) pair for one audit log row, to fold into a bigger transaction."""
        return sql.insert(
            models.AuditLogEntry,
            models.NewAuditLogEntry(
                user_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=json.dumps(details),
            ),
        )

    def _soft_delete(
        self, table: type[BaseModel], entity_id: int, actor_id: int, *, entity_type: models.EntityType
    ) -> None:
        """
        Soft-delete one row and cascade to every table `_cascade_targets` finds owned by it.

        Cascade is restricted to `SOFT_DELETABLE`, since only those tables have `deleted_at`. Only
        rows not already independently deleted are touched, so a child soft-deleted earlier on its
        own keeps its original `deleted_at`.
        """
        cascade = _cascade_targets(table, within=SOFT_DELETABLE)
        # Counted *before* the mutation (same WHERE clause, same "not already deleted" guard) so the
        # audit row -- inserted in the same transaction as the mutation, right below -- can record
        # cascade counts without needing the UPDATEs' own row counts, which `_execute_in_transaction`
        # doesn't expose for statements without `RETURNING`.
        details: dict[str, int] = {}
        for child, where_clause in cascade:
            details[child.__name__] = details.get(child.__name__, 0) + self._count_matching(
                child, where_clause, entity_id, " AND deleted_at IS NULL"
            )

        now = pendulum.now(pendulum.UTC).isoformat()
        audit_statement, audit_values = self._record_audit_log(
            actor_id, models.AuditAction.SOFT_DELETE, entity_type, entity_id, details
        )
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
            (audit_statement, audit_values),
        ]
        results = self._execute_in_transaction(statements)
        if not results[0]:
            msg = f"{table.__name__} {entity_id} does not exist or is already deleted"
            raise ValueError(msg)

    def _restore(
        self, table: type[BaseModel], entity_id: int, actor_id: int, *, entity_type: models.EntityType
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

        cascade = _cascade_targets(table, within=SOFT_DELETABLE)
        details: dict[str, int] = {}
        for child, where_clause in cascade:
            details[child.__name__] = details.get(child.__name__, 0) + self._count_matching(
                child, where_clause, entity_id, " AND deleted_at = :deleted_at", {"deleted_at": deleted_at}
            )

        audit_statement, audit_values = self._record_audit_log(
            actor_id, models.AuditAction.RESTORE, entity_type, entity_id, details
        )
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
            (audit_statement, audit_values),
        ]
        self._execute_in_transaction(statements)

    def _artifact_refs_to_purge(self, table: type[BaseModel], entity_id: int) -> list[tuple[int, str]]:
        """
        Find every `Artifact` row about to disappear when `entity_id` (of type `table`) is purged.

        Reuses `_cascade_targets`'s generic graph walk rather than hardcoding "artifacts live under
        runs" here -- if `table` *is* `Artifact`, it's the one row being purged directly; otherwise
        look for `Artifact` among its cascade descendants (today that's always via `Run`, but this
        doesn't assume that). Must run *before* the purge's `DELETE`, since that's the only place
        an `Artifact` row's `ref` -- the blob location -- is ever readable; SQLite's `ON DELETE
        CASCADE` removes cascaded rows without any Python code seeing them.
        """
        if table is models.Artifact:
            where_clause = "id = :id"
        else:
            matches = [wc for child, wc in _cascade_targets(table) if child is models.Artifact]
            if not matches:
                return []
            where_clause = matches[0]
        rows = self._execute_raw_sql(f"SELECT id, ref FROM Artifact WHERE {where_clause}", {"id": entity_id})
        return [(row[0], row[1]) for row in rows]

    def _purge(
        self, table: type[BaseModel], entity_id: int, actor: models.User, *, entity_type: models.EntityType
    ) -> None:
        """
        Permanently delete one already-soft-deleted row.

        SQLite's `ON DELETE CASCADE` (see `FOREIGN_KEYS`/`ForeignKeyKind.OWNERSHIP`) handles
        removing every dependent row natively -- no per-table `DELETE` statements to enumerate here.
        The one irreversible action in this module -- everything else (soft-delete, restore) can be
        undone -- so it's the one place that actually gates on a scope (`Scope.PURGE`) rather than
        just recording who did it. Cascade counts for the audit log are computed with
        `_cascade_targets`'s *full* (unfiltered) graph, since purge reaches metrics/hyperparams/pages
        too, not just the soft-deletable entities -- and (unlike soft-delete/restore) counts matching
        rows regardless of their own `deleted_at`, since purging a project removes everything under
        it either way.

        Every artifact blob about to be orphaned by the cascade gets one `ArtifactPurgeTask` row, in
        the same transaction as the delete, so a background worker can clean up the underlying
        `ArtifactStore` blobs afterward without risking losing track of one if the process dies
        right after this commits.
        """
        if not models.has_scope(actor, models.Scope.PURGE):
            msg = f"User {actor.id} lacks the {models.Scope.PURGE} scope"
            raise PermissionError(msg)

        if self._fetch_deleted_at(table, entity_id) is None:
            msg = f"{table.__name__} {entity_id} must be soft-deleted before it can be purged"
            raise ValueError(msg)

        details: dict[str, int] = {}
        for child, where_clause in _cascade_targets(table):
            details[child.__name__] = details.get(child.__name__, 0) + self._count_matching(
                child, where_clause, entity_id
            )

        artifact_refs = self._artifact_refs_to_purge(table, entity_id)

        audit_statement, audit_values = self._record_audit_log(
            actor.id, models.AuditAction.PURGE, entity_type, entity_id, details
        )
        self._execute_in_transaction(
            [
                (f"DELETE FROM {table.__name__} WHERE id = :id", {"id": entity_id}),
                (audit_statement, audit_values),
                *(
                    sql.insert(
                        models.ArtifactPurgeTask,
                        models.NewArtifactPurgeTask(artifact_id=artifact_id, ref=ref, requested_by=actor.id),
                    )
                    for artifact_id, ref in artifact_refs
                ),
            ]
        )

    def delete_project(self, project_id: int, actor_id: int) -> None:
        """Soft-delete a project and cascade to its experiments, runs, and artifacts."""
        self._soft_delete(models.Project, project_id, actor_id, entity_type=models.EntityType.PROJECT)

    def restore_project(self, project_id: int, actor_id: int) -> None:
        """Restore a soft-deleted project and every experiment/run/artifact deleted with it."""
        self._restore(models.Project, project_id, actor_id, entity_type=models.EntityType.PROJECT)

    def purge_project(self, project_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted project and everything under it."""
        self._purge(models.Project, project_id, actor, entity_type=models.EntityType.PROJECT)

    def delete_experiment(self, experiment_id: int, actor_id: int) -> None:
        """Soft-delete an experiment and cascade to its runs and artifacts."""
        self._soft_delete(
            models.Experiment, experiment_id, actor_id, entity_type=models.EntityType.EXPERIMENT
        )

    def restore_experiment(self, experiment_id: int, actor_id: int) -> None:
        """Restore a soft-deleted experiment and every run/artifact deleted with it."""
        self._restore(models.Experiment, experiment_id, actor_id, entity_type=models.EntityType.EXPERIMENT)

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted experiment and everything under it."""
        self._purge(models.Experiment, experiment_id, actor, entity_type=models.EntityType.EXPERIMENT)

    def delete_run(self, run_id: int, actor_id: int) -> None:
        """Soft-delete a run and cascade to its artifacts."""
        self._soft_delete(models.Run, run_id, actor_id, entity_type=models.EntityType.RUN)

    def restore_run(self, run_id: int, actor_id: int) -> None:
        """Restore a soft-deleted run and every artifact deleted with it."""
        self._restore(models.Run, run_id, actor_id, entity_type=models.EntityType.RUN)

    def purge_run(self, run_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted run and everything under it."""
        self._purge(models.Run, run_id, actor, entity_type=models.EntityType.RUN)

    def delete_artifact(self, artifact_id: int, actor_id: int) -> None:
        """Soft-delete a single artifact (a leaf -- nothing depends on it)."""
        self._soft_delete(models.Artifact, artifact_id, actor_id, entity_type=models.EntityType.ARTIFACT)

    def restore_artifact(self, artifact_id: int, actor_id: int) -> None:
        """Restore a soft-deleted artifact."""
        self._restore(models.Artifact, artifact_id, actor_id, entity_type=models.EntityType.ARTIFACT)

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted artifact."""
        self._purge(models.Artifact, artifact_id, actor, entity_type=models.EntityType.ARTIFACT)

    def list_audit_log(self, limit: int = 100, offset: int = 0) -> Iterator[models.AuditLogEntry]:
        """List audit log entries, most recent first, for a trash/admin view."""
        statement = f"""
            SELECT * FROM {models.AuditLogEntry.__name__}
            ORDER BY timestamp_utc DESC
            LIMIT {int(limit)} OFFSET {int(offset)};
        """
        yield from self._execute_sql_query(models.AuditLogEntry, statement)

    def _list_deleted[RowT: BaseModel](self, table: type[RowT], *, limit: int, offset: int) -> Iterator[RowT]:
        """
        Shared query behind every `list_deleted_*` method: most-recently-deleted first, capped.

        A project's cascade can soft-delete everything under it in one go -- easily thousands of
        artifact rows for a training-heavy project -- so this is never unbounded: callers always
        get a page, never "every row," the same discipline `list_audit_log` already follows.
        """
        statement = f"""
            SELECT * FROM {table.__name__}
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
            LIMIT {int(limit)} OFFSET {int(offset)};
        """
        yield from self._execute_sql_query(table, statement)

    def list_deleted_projects(self, limit: int = 100, offset: int = 0) -> Iterator[models.Project]:
        """List soft-deleted projects, most recently deleted first, for a trash/admin view."""
        yield from self._list_deleted(models.Project, limit=limit, offset=offset)

    def list_deleted_experiments(self, limit: int = 100, offset: int = 0) -> Iterator[models.Experiment]:
        """List soft-deleted experiments, most recently deleted first, for a trash/admin view."""
        yield from self._list_deleted(models.Experiment, limit=limit, offset=offset)

    def list_deleted_runs(self, limit: int = 100, offset: int = 0) -> Iterator[models.Run]:
        """List soft-deleted runs, most recently deleted first, for a trash/admin view."""
        yield from self._list_deleted(models.Run, limit=limit, offset=offset)

    def list_deleted_artifacts(self, limit: int = 100, offset: int = 0) -> Iterator[models.Artifact]:
        """List soft-deleted artifacts, most recently deleted first, for a trash/admin view."""
        yield from self._list_deleted(models.Artifact, limit=limit, offset=offset)

    def list_pending_artifact_purges(
        self, limit: int = 100, offset: int = 0
    ) -> Iterator[models.ArtifactPurgeTask]:
        """
        List artifact blobs still waiting to be deleted from the `ArtifactStore`, oldest first.

        Every row in this table is, by definition, pending work -- a completed task is deleted
        outright (see `complete_artifact_purge`) rather than marked done, so there's no status
        column to filter on here.
        """
        statement = f"""
            SELECT * FROM {models.ArtifactPurgeTask.__name__}
            ORDER BY requested_at ASC
            LIMIT {int(limit)} OFFSET {int(offset)};
        """
        yield from self._execute_sql_query(models.ArtifactPurgeTask, statement)

    def count_pending_artifact_purges(self) -> int:
        """Count artifact blobs still waiting to be deleted. 0 means the last purge fully cleaned up."""
        rows = list(self._execute_raw_sql(f"SELECT count(*) FROM {models.ArtifactPurgeTask.__name__}"))
        return rows[0][0]

    def complete_artifact_purge(self, task_id: int) -> None:
        """Record that a queued blob deletion succeeded by deleting its task row."""
        list(
            self._execute_raw_sql(
                f"DELETE FROM {models.ArtifactPurgeTask.__name__} WHERE id = :id", {"id": task_id}
            )
        )

    def fail_artifact_purge(self, task_id: int, error: str) -> None:
        """Record that a queued blob deletion failed. The task stays pending and is retried later."""
        list(
            self._execute_raw_sql(
                f"UPDATE {models.ArtifactPurgeTask.__name__} SET last_error = :error WHERE id = :id",
                {"error": error, "id": task_id},
            )
        )
