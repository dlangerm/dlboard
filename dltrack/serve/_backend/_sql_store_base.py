"""The generic base class for a sql data store."""

from __future__ import annotations

import itertools
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

import pendulum
import sqlalchemy as sa
from pydantic import BaseModel
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve import sql
from dltrack.serve._backend._app_state import APP_STATE_ROW_ID, AppState
from dltrack.serve._backend._foreign_keys import ForeignKey, ForeignKeyKind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from datetime import datetime


_log = get_logger(__name__)

type AnyRow = sa.Row[*tuple[Any, ...]]
"""A result row of any shape -- `Row` is generic over its columns' types, which raw aggregates don't pin down."""

_OWNS = ForeignKeyKind.OWNERSHIP

# Declares every column's foreign key relationship: what it references, and whether it's OWNERSHIP
# (the parent in the delete/restore/purge hierarchy -- gets a real `ON DELETE CASCADE`, and is what
# `_cascade_targets` walks) or plain ATTRIBUTION (e.g. `created_by`, never cascades). This is the
# single source of truth for both SQL schema generation and cascade behavior -- see
# `_foreign_keys.py` -- so a new owned table only ever needs one entry here, not one hardcoded
# cascade list per delete/restore/purge entry point.
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
    models.Comment: {
        "experiment_id": ForeignKey(models.Experiment, _OWNS),
        "author_id": ForeignKey(models.User),
    },
    models.AuditLogEntry: {"user_id": ForeignKey(models.User)},
    models.ArtifactPurgeTask: {"requested_by": ForeignKey(models.User)},
}

UNIQUE_COLUMNS: dict[type[BaseModel], list[str]] = {
    models.User: ["username"],
}

# Dependency order: every table appears after the tables its foreign keys point to.
TABLES: tuple[type[BaseModel], ...] = (
    AppState,
    models.User,
    models.Project,
    models.Experiment,
    models.Run,
    models.UnderlyingMetricTableEntry,
    models.HyperParams,
    models.Artifact,
    models.Page,
    models.Comment,
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

# Indexes the raw-SQL store used to create under column-list names -- too long for Postgres' 63-char
# identifier limit, so they're now named for what they're for instead. A sqlite database from before
# that still has the old ones: dropped once at startup, replaced by the renamed equivalents.
_LEGACY_INDEXES: tuple[str, ...] = (
    "idx_UnderlyingMetricTableEntry_key_experiment_id_run_id_step_timestamp_utc",
    "idx_Artifact_key_fname_run_id_experiment_id_step_created_by_created_at_ref_deleted_by_deleted_at",
)

type CascadePath = tuple[tuple[type[BaseModel], str], ...]
"""
The ownership edges from a cascade's root down to one descendant table, root-most first: each is
`(table, its foreign-key column pointing at the previous table)`. The last one is the descendant.
"""


def _cascade_targets(
    root: type[BaseModel], *, within: frozenset[type[BaseModel]] | None = None
) -> list[CascadePath]:
    """
    Breadth-first walk of the `OWNERSHIP` foreign-key graph (see `FOREIGN_KEYS`) starting at `root`.

    Returns the path to every descendant table. `within`, if given, prunes the walk to only
    tables in that set (e.g. `SOFT_DELETABLE`) -- a table outside it is skipped entirely, neither
    returned nor recursed into, since it and everything under it are irrelevant to why `within`
    was passed (e.g. `Page` has no `deleted_at` to filter on, so a soft-delete/restore cascade must
    never touch it even though it's directly owned by `Project`).

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
    results: list[CascadePath] = []
    frontier: list[tuple[type[BaseModel], CascadePath]] = [(root, ())]
    while frontier:
        parent, path = frontier.pop(0)
        for child, column, parent_model in edges:
            if parent_model is not parent or (within is not None and child not in within):
                continue
            child_path = (*path, (child, column))
            results.append(child_path)
            frontier.append((child, child_path))
    return results


type PageScopeField = Literal["run_id", "experiment_id", "project_id"]
"""The column a `Page` is scoped by -- typed, not a bare `str`, so only these three ever reach a query."""

PAGE_SCOPE_FIELDS: tuple[PageScopeField, ...] = ("run_id", "experiment_id", "project_id")


def _resolve_page_scope(
    *, run_id: int | None, experiment_id: int | None, project_id: int | None
) -> tuple[PageScopeField, int]:
    """
    Resolve the single scope (run, experiment, or project) a page belongs to.

    A page is always owned by exactly one of these three ids; this validates that
    invariant and returns the owning field name paired with its id.
    """
    scopes: dict[PageScopeField, int | None] = {
        "run_id": run_id,
        "experiment_id": experiment_id,
        "project_id": project_id,
    }
    owned: dict[PageScopeField, int] = {field: id_ for field, id_ in scopes.items() if id_ is not None}
    if len(owned) != 1:
        msg = "Exactly one of run, experiment, or project must be defined."
        raise ValueError(msg)
    return next(iter(owned.items()))


class SQLStoreBase[T](ABC, models.DataStore[T]):
    """
    The full `DataStore` contract over any database SQLAlchemy has a dialect for.

    A concrete store supplies only an `Engine` (connection handling, pooling, per-connection
    setup) and `_insert_ignoring_conflicts` -- the one statement SQLAlchemy spells per dialect.
    """

    def __init__(self, engine: sa.Engine, *, schema: str | None = None) -> None:
        """Declare every table on one `MetaData`, then create/migrate the schema in one transaction."""
        self._engine = engine
        self._metadata = sa.MetaData(schema=schema)
        self._tables: dict[type[BaseModel], sa.Table] = {}
        for model in TABLES:
            self._tables[model] = sql.table_for(
                model, self._metadata, self._tables, FOREIGN_KEYS.get(model), UNIQUE_COLUMNS.get(model)
            )
        metrics, artifacts, pages = (
            self._tables[m] for m in (models.UnderlyingMetricTableEntry, models.Artifact, models.Page)
        )
        indexes = [
            sa.Index("idx_metric_lookup", *(c for c in metrics.c if c.name not in ("id", "value"))),
            sa.Index("idx_artifact_lookup", *(c for c in artifacts.c if c.name not in ("id", "tags"))),
            # At most one *shared* page (owner_id NULL) per scope -- exactly what
            # `get_or_create_page` relies on to make its own check-then-insert race-safe.
            *(
                sa.Index(
                    f"idx_Page_shared_{field}",
                    pages.c[field],
                    unique=True,
                    sqlite_where=pages.c.owner_id.is_(None),
                    postgresql_where=pages.c.owner_id.is_(None),
                )
                for field in PAGE_SCOPE_FIELDS
            ),
        ]

        with engine.begin() as conn:
            self._prepare_schema(conn)
            self._metadata.create_all(conn)
            for table in self._tables.values():
                self._add_missing_columns(conn, table)
            for name in _LEGACY_INDEXES:
                conn.execute(sa.text(f"DROP INDEX IF EXISTS {conn.dialect.identifier_preparer.quote(name)}"))
            # A database that hit `get_or_create_page`'s race before the shared-page index existed
            # would otherwise fail to create it at all (the database validates a new unique index
            # against existing rows) -- keep the earliest shared page per scope, the one `ORDER BY
            # id LIMIT 1` always picked anyway. `create_all` skips an existing table's indexes, so
            # they're each created (if missing) explicitly, after that cleanup.
            for field in PAGE_SCOPE_FIELDS:
                shared = sa.and_(pages.c.owner_id.is_(None), pages.c[field].is_not(None))
                keep = sa.select(sa.func.min(pages.c.id)).where(shared).group_by(pages.c[field])
                conn.execute(sa.delete(pages).where(shared, pages.c.id.not_in(keep)))
            for index in indexes:
                index.create(conn, checkfirst=True)
            state = self._tables[AppState]
            conn.execute(
                self._insert_ignoring_conflicts(state).values(
                    id=APP_STATE_ROW_ID, bootstrap_admin_assigned=False
                )
            )

    @property
    def tables(self) -> Mapping[type[BaseModel], sa.Table]:
        """Every table this store manages, keyed by the pydantic model it stores."""
        return self._tables

    def dispose(self) -> None:
        """Close every connection this store's pool holds open (a later call just opens new ones)."""
        self._engine.dispose()

    @abstractmethod
    def _insert_ignoring_conflicts(self, table: sa.Table) -> sa.Insert:
        """
        An `INSERT` into `table` that silently skips a row violating a unique constraint or index.

        Every dialect spells this differently (`INSERT ... ON CONFLICT DO NOTHING` via its own
        `sqlalchemy.dialects.<name>.insert`), so it's the one statement each store supplies itself.
        """

    def _prepare_schema(self, conn: sa.Connection) -> None:
        """
        Run first in the schema creation/migration transaction `conn` is in.

        A no-op by default. A server-based database, where several worker processes can start
        against it at once, overrides this to take a lock that concurrent schema init then waits
        on (and to create anything the tables need to exist first, e.g. their schema).
        """

    @staticmethod
    def _add_missing_columns(conn: sa.Connection, table: sa.Table) -> None:
        """
        Backfill a table with any model field it's missing a column for.

        This project has no migration system: `create_all` skips a table that already exists, so a
        field added to a model after someone's database was created would otherwise never appear
        there. Every added column is nullable regardless of the model's own optionality -- existing
        rows have no value to put there -- except that a plain scalar default (e.g.
        `revision: int = 0`) comes along as a real SQL `DEFAULT`, which backfills existing rows with
        it instead of NULL (`NULL + 1`, in `_touch_experiment`, would stay `NULL` forever).
        """
        existing = {c["name"] for c in sa.inspect(conn).get_columns(table.name, schema=table.schema)}
        for column in table.columns:
            if column.name not in existing:
                conn.execute(sa.text(sql.add_column_ddl(table, column, conn.dialect)))

    def _execute(self, statement: sa.Executable) -> list[AnyRow]:
        """Run one statement in its own transaction, returning its rows (if it has any)."""
        with self._engine.begin() as conn:
            result = conn.execute(statement)
            return list(result) if result.returns_rows else []

    def _fetch[RowT: BaseModel](
        self, model: type[RowT], statement: sa.Executable, *, no_validate: bool = False
    ) -> list[RowT]:
        """Run one statement in its own transaction, building a `model` from each row by column name."""
        with self._engine.begin() as conn:
            return [
                sql.construct(model, row, no_validate=no_validate)
                for row in conn.execute(statement).mappings()
            ]

    def _transaction(self, statements: Iterable[sa.Executable]) -> list[list[AnyRow]]:
        """
        Run several statements as one atomic transaction, returning each statement's rows.

        All-or-nothing: used for cascading soft-delete/restore/purge across multiple tables, where
        a failure partway through must not leave e.g. a project deleted but its experiments intact.
        """
        results: list[list[AnyRow]] = []
        with self._engine.begin() as conn:
            for statement in statements:
                result = conn.execute(statement)
                results.append(list(result) if result.returns_rows else [])
        return results

    def _insert_rows(self, model: type[BaseModel], rows: Iterable[BaseModel]) -> None:
        """Bulk-insert `rows` into `model`'s table in one transaction, as a single executemany."""
        values = [sql.row_values(row, exclude=frozenset({sql.ID_KEY})) for row in rows]
        if values:
            with self._engine.begin() as conn:
                conn.execute(sa.insert(self._tables[model]), values)

    def _by_id[RowT: BaseModel](self, model: type[RowT], entity_id: int) -> list[RowT]:
        """`model`'s row with `entity_id`, if it exists and isn't soft-deleted."""
        table = self._tables[model]
        return self._fetch(
            model, sa.select(table).where(table.c.id == entity_id, table.c.deleted_at.is_(None))
        )

    def _fetch_deleted_at(self, model: type[BaseModel], entity_id: int) -> datetime | None:
        """Return a row's `deleted_at`, or raise if the row doesn't exist at all."""
        table = self._tables[model]
        rows = self._execute(sa.select(table.c.deleted_at).where(table.c.id == entity_id))
        if not rows:
            msg = f"{model.__name__} {entity_id} does not exist"
            raise ValueError(msg)
        return rows[0][0]

    def _ensure_not_deleted(self, model: type[BaseModel], entity_id: int) -> None:
        """Raise if `entity_id` doesn't exist, or has been soft-deleted."""
        if self._fetch_deleted_at(model, entity_id) is not None:
            msg = f"{model.__name__} {entity_id} has been deleted"
            raise ValueError(msg)

    def _touch_experiment(self, experiment_id: int) -> None:
        """
        Bump an experiment's `revision` counter (and `last_activity_at`) after a mutation visible on its page.

        A second statement after the write's own commit, not folded into one transaction with it --
        self-healing (the next write to this experiment bumps it again), so the narrow window where
        a crash could leave `revision` stale by one increment is a possible one-write delay in a live
        client noticing, not a correctness bug.
        """
        experiments = self._tables[models.Experiment]
        self._execute(
            sa.update(experiments)
            .where(experiments.c.id == experiment_id)
            .values(revision=experiments.c.revision + 1, last_activity_at=pendulum.now(pendulum.UTC))
        )

    def _activity_rows(
        self, group: type[models.Project | models.Experiment], project_id: int | None = None
    ) -> list[AnyRow]:
        """`(group id, experiment count, run count, last activity)` per project or experiment, non-deleted rows only."""
        p, e, r = (self._tables[m] for m in (models.Project, models.Experiment, models.Run))
        group_id = p.c.id if group is models.Project else e.c.id
        project_filter = [p.c.id == project_id] if project_id is not None else []
        return self._execute(
            sa.select(
                group_id,
                sa.func.count(sa.distinct(e.c.id)),
                sa.func.count(r.c.id),
                sa.func.max(e.c.last_activity_at),
            )
            .select_from(
                p.join(e, sa.and_(e.c.project_id == p.c.id, e.c.deleted_at.is_(None))).outerjoin(
                    r, sa.and_(r.c.experiment_id == e.c.id, r.c.deleted_at.is_(None))
                )
            )
            .where(p.c.deleted_at.is_(None), *project_filter)
            .group_by(group_id)
        )

    def get_project_stats(self) -> dict[int, models.ProjectStats]:
        """Every (non-deleted) project's activity; a project with no experiments is simply absent."""
        return {
            project_id: models.ProjectStats(
                experiment_count=experiments, run_count=runs, last_activity_at=last_activity
            )
            for project_id, experiments, runs, last_activity in self._activity_rows(models.Project)
        }

    def get_experiment_stats(self, project_id: int) -> dict[int, models.ActivityStats]:
        """Each of a project's (non-deleted) experiments' activity, keyed by experiment id."""
        return {
            experiment_id: models.ActivityStats(run_count=runs, last_activity_at=last_activity)
            for experiment_id, _experiments, runs, last_activity in self._activity_rows(
                models.Experiment, project_id
            )
        }

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
        users = self._tables[models.User]
        by_name = sa.select(users).where(users.c.username == username)
        if existing := self._fetch(models.User, by_name):
            return existing[0]

        self._execute(
            self._insert_ignoring_conflicts(users).values(sql.row_values(models.NewUser(username=username)))
        )
        (user,) = self._fetch(models.User, by_name)

        # A single conditional `UPDATE` is atomic under concurrent writers: if two processes race
        # this for the first time simultaneously, only one can ever see (and flip) the flag unset.
        state = self._tables[AppState]
        claimed = self._execute(
            sa.update(state)
            .where(state.c.id == APP_STATE_ROW_ID, sa.not_(state.c.bootstrap_admin_assigned))
            .values(bootstrap_admin_assigned=True)
            .returning(state.c.id)
        )
        if not claimed:
            return user

        _log.info("Granting bootstrap admin scopes to user %s (%s)", user.id, username)
        return self.update_user(user.model_copy(update={"scopes": [models.Scope.ALL]}))

    def update_user(self, user: models.User) -> models.User:
        """Update a user, e.g. to grant/revoke scopes."""
        _log.debug("updating user %s", user.id)
        (updated,) = self._fetch(models.User, sql.update(self._tables[models.User], user))
        return updated

    def create_project(self, project: models.NewProject) -> models.Project:
        """Create a new project."""
        _log.debug("Creating project with name %s", project.name)
        (created,) = self._fetch(models.Project, sql.insert(self._tables[models.Project], project))
        return created

    def get_or_create_project(
        self, name: str, description: str = "", created_by: int | None = None
    ) -> models.Project:
        """Get the project named `name`, creating it (with `description`) if it doesn't exist yet."""
        projects = self._tables[models.Project]
        existing = self._fetch(
            models.Project,
            sa.select(projects).where(projects.c.name == name, projects.c.deleted_at.is_(None)),
        )
        if existing:
            return existing[0]
        return self.create_project(
            models.NewProject(name=name, description=description, created_by=created_by)
        )

    def get_project(self, database_id: int) -> models.Project:
        """Get project."""
        _log.debug("getting project %s", database_id)
        return next(iter(self._by_id(models.Project, database_id)))

    def get_projects(self) -> Iterator[models.Project]:
        """Get all (non-deleted) projects."""
        _log.debug("getting projects")
        projects = self._tables[models.Project]
        yield from self._fetch(models.Project, sa.select(projects).where(projects.c.deleted_at.is_(None)))

    def update_project(self, project: models.Project) -> models.Project:
        """Update a project."""
        _log.debug("updating project %s", project.id)
        (updated,) = self._fetch(models.Project, sql.update(self._tables[models.Project], project))
        return updated

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        _log.info("Creating experiment for project %s", experiment.project_id)
        self._ensure_not_deleted(models.Project, experiment.project_id)
        (created,) = self._fetch(models.Experiment, sql.insert(self._tables[models.Experiment], experiment))
        return created

    def get_or_create_experiment(
        self,
        project_id: int,
        name: str = "default",
        created_by: int | None = None,
        source: models.ExperimentSource | None = None,
    ) -> models.Experiment:
        """Get the named experiment within `project_id`, creating it if it doesn't exist yet."""
        experiments = self._tables[models.Experiment]
        existing = self._fetch(
            models.Experiment,
            sa.select(experiments).where(
                experiments.c.project_id == project_id,
                experiments.c.name == name,
                experiments.c.deleted_at.is_(None),
            ),
        )
        if existing:
            return existing[0]
        return self.create_experiment(
            models.NewExperiment(project_id=project_id, name=name, created_by=created_by, source=source)
        )

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        """Get an experiment by id, or None if it doesn't exist (or has been deleted)."""
        _log.debug("Getting experiment id %s", database_id)
        return next(iter(self._by_id(models.Experiment, database_id)), None)

    def get_experiments(self, project_id: int) -> Iterator[models.Experiment]:
        """Get all (non-deleted) experiments belonging to a project."""
        _log.debug("Get experiments for project %s", project_id)
        experiments = self._tables[models.Experiment]
        return iter(
            self._fetch(
                models.Experiment,
                sa.select(experiments).where(
                    experiments.c.project_id == project_id, experiments.c.deleted_at.is_(None)
                ),
            )
        )

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        """Update an experiment."""
        _log.debug("updating experiment %s", experiment.id)
        (updated,) = self._fetch(models.Experiment, sql.update(self._tables[models.Experiment], experiment))
        return updated

    def create_run(self, run: models.NewRun) -> models.Run:
        self._ensure_not_deleted(models.Experiment, run.experiment_id)
        (created,) = self._fetch(models.Run, sql.insert(self._tables[models.Run], run))
        self._touch_experiment(run.experiment_id)
        return created

    def get_runs(self, experiment_id: int, *, limit: int = 1000, offset: int = 0) -> Iterator[models.Run]:
        """Get a page of an experiment's (non-deleted) runs, most recently created first."""
        runs = self._tables[models.Run]
        return iter(
            self._fetch(
                models.Run,
                sa.select(runs)
                .where(runs.c.experiment_id == experiment_id, runs.c.deleted_at.is_(None))
                .order_by(runs.c.created_at.desc())
                .limit(limit)
                .offset(offset),
            )
        )

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log a batch of metrics to the data store."""
        _log.debug("Logging metrics batch")
        metric = list(metric)
        for run_id in {m.run_id for m in metric}:
            self._ensure_not_deleted(models.Run, run_id)
        self._insert_rows(
            models.UnderlyingMetricTableEntry,
            itertools.chain.from_iterable(m.to_underlying() for m in metric),
        )
        for experiment_id in {m.experiment_id for m in metric}:
            self._touch_experiment(experiment_id)

    def fetch_metrics(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> models.MetricFrame:
        """An experiment's (non-deleted runs') metrics -- only `keys`, if given, else every metric."""
        # `UnderlyingMetricTableEntry` has no `deleted_at` of its own -- visibility is inherited
        # transitively through its run, which is always soft-deleted in the same cascade as its
        # metrics' logical owner (see `_soft_delete`), so a join against `Run` is sufficient.
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        key_filter = [m.c.key.in_(keys)] if keys is not None else []
        # `ORDER BY m.id` is insertion order, which is what `MetricFrame.from_rows`' last-write-wins needs.
        rows = self._execute(
            sa.select(*(m.c[f] for f in models.MetricRow._fields))
            .join(r, m.c.run_id == r.c.id)
            .where(
                m.c.experiment_id == experiment_id,
                r.c.deleted_at.is_(None),
                m.c.run_id.not_in(exclude_run_ids),
                *key_filter,
            )
            .order_by(m.c.id)
        )
        return models.MetricFrame.from_rows(models.MetricRow._make(row) for row in rows)

    def summarize_metric_keys(self, experiment_id: int) -> list[models.MetricKeySummary]:
        """Every metric key logged in an experiment (non-deleted runs), sorted, without fetching values."""
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        per_run = (
            sa.select(m.c.key, sa.func.count(sa.distinct(m.c.step)).label("steps"))
            .join(r, m.c.run_id == r.c.id)
            .where(m.c.experiment_id == experiment_id, r.c.deleted_at.is_(None))
            .group_by(m.c.key, m.c.run_id)
            .subquery("per_run")
        )
        rows = self._execute(
            sa.select(per_run.c.key, sa.func.max(per_run.c.steps))
            .group_by(per_run.c.key)
            .order_by(per_run.c.key)
        )
        return [models.MetricKeySummary(key, steps) for key, steps in rows]

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters to the data store."""
        _log.debug("Logging hyperparameters for experiment %s", hyperparams.experiment_id)
        self._ensure_not_deleted(models.Run, hyperparams.run_id)
        table = self._tables[models.HyperParams]
        if existing := self._fetch(
            models.HyperParams, sa.select(table).where(table.c.run_id == hyperparams.run_id)
        ):
            _log.warning(
                "Skipping duplicate hyperparameters for experiment %s run %s",
                hyperparams.experiment_id,
                hyperparams.run_id,
            )
            return existing[0]
        (created,) = self._fetch(models.HyperParams, sql.insert(table, hyperparams))
        self._touch_experiment(hyperparams.experiment_id)
        return created

    def fetch_hyperparams(
        self, experiment_id: int, *, exclude_run_ids: frozenset[int] = frozenset()
    ) -> Iterator[models.HyperParams]:
        """Every (non-deleted) run's hyperparameters for an experiment."""
        h, r = self._tables[models.HyperParams], self._tables[models.Run]
        yield from self._fetch(
            models.HyperParams,
            sa.select(h)
            .join(r, h.c.run_id == r.c.id)
            .where(
                h.c.experiment_id == experiment_id,
                r.c.deleted_at.is_(None),
                h.c.run_id.not_in(exclude_run_ids),
            ),
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
        field, value = _resolve_page_scope(run_id=run_id, experiment_id=experiment_id, project_id=project_id)
        pages = self._tables[models.Page]
        # The shared page only: every named view of it (`owner_id` set) is its own `Page` row with
        # the same scope, reached through `get_view` instead.
        shared = (
            sa.select(pages)
            .where(pages.c[field] == value, pages.c.owner_id.is_(None))
            .order_by(pages.c.id)
            .limit(1)
        )
        if existing := self._fetch(page_type, shared):
            return existing[0]

        _log.warning("Inserting new page model for %s = %s", field, value)
        # Insert-ignoring-conflicts, then re-select -- exactly `get_or_create_user`'s own pattern, and
        # for the same reason: two callers racing the check above (e.g. a page load and a script both
        # reaching the same brand-new experiment before either has created its shared page) would
        # otherwise both insert one, leaving two "the" shared pages for one scope. The partial
        # unique index on {field} WHERE owner_id IS NULL (this class's own `__init__`) turns the
        # loser's insert into a no-op instead, and the re-select returns whichever one actually won.
        new_page = (new_page_type or models.NewPage[D, C])(**{field: value})  # pyright: ignore[reportArgumentType]
        self._execute(self._insert_ignoring_conflicts(pages).values(sql.row_values(new_page)))
        (created,) = self._fetch(page_type, shared)
        return created

    def create_view[D, P, C](
        self, page_type: type[models.Page[D, P, C]], view: models.NewPage[D, C]
    ) -> models.Page[D, P, C]:
        """Save `view` -- a named, owned copy of a page's panels and settings -- as its own page."""
        if view.owner_id is None or not view.name:
            msg = "A view needs an owner and a name"
            raise ValueError(msg)
        (created,) = self._fetch(page_type, sql.insert(self._tables[models.Page], view))
        return created

    def get_view[D, P, C](
        self, page_type: type[models.Page[D, P, C]], view_id: int
    ) -> models.Page[D, P, C] | None:
        """A named view by id, or `None` if there's no such view (a shared page's id doesn't count)."""
        pages = self._tables[models.Page]
        rows = self._fetch(
            page_type, sa.select(pages).where(pages.c.id == view_id, pages.c.owner_id.is_not(None))
        )
        return next(iter(rows), None)

    def list_views(self, experiment_id: int, owner_id: int) -> list[models.ViewSummary]:
        """`owner_id`'s views of an experiment's page, by name."""
        pages = self._tables[models.Page]
        rows = self._execute(
            sa.select(pages.c.id, pages.c.name)
            .where(pages.c.experiment_id == experiment_id, pages.c.owner_id == owner_id)
            .order_by(pages.c.name, pages.c.id)
        )
        return [models.ViewSummary(id=view_id, name=name) for view_id, name in rows]

    def delete_view(self, view_id: int, owner_id: int) -> None:
        """Delete one of `owner_id`'s views; anyone else's (or the shared page) is left alone."""
        pages = self._tables[models.Page]
        self._execute(sa.delete(pages).where(pages.c.id == view_id, pages.c.owner_id == owner_id))

    def _bump_notes_revision(self, experiment_id: int) -> None:
        experiments = self._tables[models.Experiment]
        self._execute(
            sa.update(experiments)
            .where(experiments.c.id == experiment_id)
            .values(notes_revision=experiments.c.notes_revision + 1)
        )

    def add_comment(self, comment: models.NewComment) -> models.Comment:
        """Post a note to an experiment's thread."""
        self._ensure_not_deleted(models.Experiment, comment.experiment_id)
        (created,) = self._fetch(models.Comment, sql.insert(self._tables[models.Comment], comment))
        self._bump_notes_revision(comment.experiment_id)
        return created

    def list_comments(self, experiment_id: int) -> list[models.Comment]:
        """An experiment's notes, oldest first."""
        comments = self._tables[models.Comment]
        return self._fetch(
            models.Comment,
            sa.select(comments).where(comments.c.experiment_id == experiment_id).order_by(comments.c.id),
        )

    def delete_comment(self, comment_id: int, author_id: int) -> None:
        """Delete one of `author_id`'s own notes; anyone else's is left alone."""
        comments = self._tables[models.Comment]
        rows = self._execute(
            sa.delete(comments)
            .where(comments.c.id == comment_id, comments.c.author_id == author_id)
            .returning(comments.c.experiment_id)
        )
        for (experiment_id,) in rows:
            self._bump_notes_revision(experiment_id)

    def list_users(self) -> list[models.User]:
        """Every user, by username -- e.g. who a note can mention."""
        users = self._tables[models.User]
        return self._fetch(models.User, sa.select(users).order_by(users.c.username))

    def update_page[D, P, C](self, page: models.Page[D, P, C]) -> models.Page[D, P, C]:
        (updated,) = self._fetch(type(page), sql.update(self._tables[models.Page], page))
        return updated

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        artifacts = list(artifacts)
        for run_id in {a.run_id for a in artifacts}:
            self._ensure_not_deleted(models.Run, run_id)
        self._insert_rows(models.Artifact, artifacts)
        for experiment_id in {a.experiment_id for a in artifacts}:
            self._touch_experiment(experiment_id)

    def get_artifact(self, artifact_id: int) -> models.Artifact | None:
        """Get one (non-deleted) artifact by id, or `None` if it doesn't exist (or has been deleted)."""
        return next(iter(self._by_id(models.Artifact, artifact_id)), None)

    def fetch_artifacts(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> Iterator[models.Artifact]:
        """An experiment's (non-deleted) artifact metadata, not bytes -- only `keys`, if given."""
        a = self._tables[models.Artifact]
        key_filter = [a.c.key.in_(keys)] if keys is not None else []
        return iter(
            self._fetch(
                models.Artifact,
                sa.select(a)
                .where(
                    a.c.experiment_id == experiment_id,
                    a.c.deleted_at.is_(None),
                    a.c.run_id.not_in(exclude_run_ids),
                    *key_filter,
                )
                .order_by(a.c.run_id, a.c.step),
                no_validate=True,
            )
        )

    # -- Soft-delete / restore / purge -----------------------------------------------------------
    #
    # Only Project, Experiment, Run, and Artifact carry `deleted_at`/`deleted_by` -- metrics and
    # hyperparameters have no delete column of their own and are hidden transitively via a join
    # against their run (see `fetch_metrics`/`fetch_hyperparams`), since a run under a deleted
    # experiment/project is always itself soft-deleted by the same cascade.

    def _cascade(
        self, root: type[BaseModel], entity_id: int, *, within: frozenset[type[BaseModel]] | None = None
    ) -> list[tuple[sa.Table, sa.ColumnElement[bool]]]:
        """
        Every table `_cascade_targets` finds under `root`, paired with a predicate selecting its rows under `entity_id`.

        Each predicate nests one `IN (SELECT id ...)` per ownership edge on the way down, e.g. an
        artifact under a project: `run_id IN (SELECT id FROM Run WHERE experiment_id IN (SELECT id
        FROM Experiment WHERE project_id = :id))`.
        """
        results: list[tuple[sa.Table, sa.ColumnElement[bool]]] = []
        for path in _cascade_targets(root, within=within):
            top, top_column = path[0]
            where = self._tables[top].c[top_column] == entity_id
            for (parent, _), (child, column) in itertools.pairwise(path):
                where = self._tables[child].c[column].in_(sa.select(self._tables[parent].c.id).where(where))
            results.append((self._tables[path[-1][0]], where))
        return results

    def _cascade_counts(
        self,
        cascade: list[tuple[sa.Table, sa.ColumnElement[bool]]],
        extra: Callable[[sa.Table], sa.ColumnElement[bool]] = lambda _: sa.true(),
    ) -> dict[str, int]:
        """How many rows each cascade target has matching its predicate (and `extra`), for the audit log."""
        details: dict[str, int] = {}
        for table, where in cascade:
            ((count,),) = self._execute(
                sa.select(sa.func.count()).select_from(table).where(where, extra(table))
            )
            details[table.name] = details.get(table.name, 0) + count
        return details

    def _audit_log_insert(
        self,
        actor_id: int,
        action: models.AuditAction,
        entity_type: models.EntityType,
        entity_id: int,
        details: dict[str, int],
    ) -> sa.Insert:
        """One audit log row's `INSERT`, to fold into a bigger transaction."""
        return sql.insert(
            self._tables[models.AuditLogEntry],
            models.NewAuditLogEntry(
                user_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=json.dumps(details),
            ),
        )

    def _soft_delete(
        self,
        model: type[BaseModel],
        entity_id: int,
        actor: models.User,
        *,
        entity_type: models.EntityType,
    ) -> None:
        """
        Soft-delete one row and cascade to every table `_cascade_targets` finds owned by it.

        Cascade is restricted to `SOFT_DELETABLE`, since only those tables have `deleted_at`. Only
        rows not already independently deleted are touched, so a child soft-deleted earlier on its
        own keeps its original `deleted_at`. Scope enforcement isn't this module's job -- see
        `ScopeEnforcingDataStore`, which every `DataStore` (this one included) is wrapped in before
        it's reachable from the running app.
        """
        table = self._tables[model]
        cascade = self._cascade(model, entity_id, within=SOFT_DELETABLE)
        # Counted *before* the mutation (same predicate, same "not already deleted" guard) so the
        # audit row -- inserted in the same transaction as the mutation, right below -- can record
        # cascade counts without needing the UPDATEs' own row counts.
        details = self._cascade_counts(cascade, lambda t: t.c.deleted_at.is_(None))
        stamp = {"deleted_at": pendulum.now(pendulum.UTC), "deleted_by": actor.id}
        results = self._transaction(
            [
                sa.update(table)
                .where(table.c.id == entity_id, table.c.deleted_at.is_(None))
                .values(stamp)
                .returning(table.c.id),
                *(
                    sa.update(child).where(where, child.c.deleted_at.is_(None)).values(stamp)
                    for child, where in cascade
                ),
                self._audit_log_insert(
                    actor.id, models.AuditAction.SOFT_DELETE, entity_type, entity_id, details
                ),
            ]
        )
        if not results[0]:
            msg = f"{model.__name__} {entity_id} does not exist or is already deleted"
            raise ValueError(msg)

    def _restore(
        self, model: type[BaseModel], entity_id: int, actor: models.User, *, entity_type: models.EntityType
    ) -> None:
        """
        Restore one soft-deleted row and cascade to dependents deleted at the exact same instant.

        Matching on the exact `deleted_at` timestamp (stamped once, atomically, across a whole
        delete cascade in `_soft_delete`) means a child that was independently soft-deleted at a
        different time -- before or after its parent -- keeps its own deletion and isn't
        accidentally resurrected just because an ancestor is being restored. Scope enforcement isn't
        this module's job -- see `ScopeEnforcingDataStore`.
        """
        deleted_at = self._fetch_deleted_at(model, entity_id)
        if deleted_at is None:
            msg = f"{model.__name__} {entity_id} is not deleted"
            raise ValueError(msg)

        table = self._tables[model]
        cascade = self._cascade(model, entity_id, within=SOFT_DELETABLE)
        details = self._cascade_counts(cascade, lambda t: t.c.deleted_at == deleted_at)
        cleared = {"deleted_at": None, "deleted_by": None}
        self._transaction(
            [
                sa.update(table).where(table.c.id == entity_id).values(cleared),
                *(
                    sa.update(child).where(where, child.c.deleted_at == deleted_at).values(cleared)
                    for child, where in cascade
                ),
                self._audit_log_insert(actor.id, models.AuditAction.RESTORE, entity_type, entity_id, details),
            ]
        )

    def _purge(
        self, model: type[BaseModel], entity_id: int, actor: models.User, *, entity_type: models.EntityType
    ) -> None:
        """
        Permanently delete one already-soft-deleted row.

        The database's `ON DELETE CASCADE` (see `FOREIGN_KEYS`/`ForeignKeyKind.OWNERSHIP`) handles
        removing every dependent row natively -- no per-table `DELETE` statements to enumerate here.
        The one irreversible action in this module -- everything else (soft-delete, restore) can be
        undone. Cascade counts for the audit log are computed with `_cascade_targets`'s *full*
        (unfiltered) graph, since purge reaches metrics/hyperparams/pages too, not just the
        soft-deletable entities -- and (unlike soft-delete/restore) counts matching rows regardless
        of their own `deleted_at`, since purging a project removes everything under it either way.

        Every artifact blob about to be orphaned by the cascade gets one `ArtifactPurgeTask` row, in
        the same transaction as the delete, so a background worker can clean up the underlying
        `ArtifactStore` blobs afterward without risking losing track of one if the process dies
        right after this commits. The artifact rows' `ref`s -- the blob locations -- have to be read
        *before* that delete, since the cascade removes them without any Python code seeing them.
        Scope enforcement isn't this module's job -- see `ScopeEnforcingDataStore`.
        """
        if self._fetch_deleted_at(model, entity_id) is None:
            msg = f"{model.__name__} {entity_id} must be soft-deleted before it can be purged"
            raise ValueError(msg)

        table = self._tables[model]
        cascade = self._cascade(model, entity_id)
        details = self._cascade_counts(cascade)

        artifacts = self._tables[models.Artifact]
        artifact_where = (
            artifacts.c.id == entity_id
            if model is models.Artifact
            else next((where for child, where in cascade if child is artifacts), sa.false())
        )
        artifact_refs = self._execute(sa.select(artifacts.c.id, artifacts.c.ref).where(artifact_where))

        purge_tasks = self._tables[models.ArtifactPurgeTask]
        self._transaction(
            [
                sa.delete(table).where(table.c.id == entity_id),
                self._audit_log_insert(actor.id, models.AuditAction.PURGE, entity_type, entity_id, details),
                *(
                    sql.insert(
                        purge_tasks,
                        models.NewArtifactPurgeTask(artifact_id=artifact_id, ref=ref, requested_by=actor.id),
                    )
                    for artifact_id, ref in artifact_refs
                ),
            ]
        )

    def delete_project(self, project_id: int, actor: models.User) -> None:
        """Soft-delete a project and cascade to its experiments, runs, and artifacts. Requires `Scope.PROJECT_DELETE`."""
        self._soft_delete(models.Project, project_id, actor, entity_type=models.EntityType.PROJECT)

    def restore_project(self, project_id: int, actor: models.User) -> None:
        """Restore a soft-deleted project and every experiment/run/artifact deleted with it. Requires `Scope.RESTORE`."""
        self._restore(models.Project, project_id, actor, entity_type=models.EntityType.PROJECT)

    def purge_project(self, project_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted project and everything under it."""
        self._purge(models.Project, project_id, actor, entity_type=models.EntityType.PROJECT)

    def delete_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Soft-delete an experiment and cascade to its runs and artifacts. Requires `Scope.EXPERIMENT_DELETE`."""
        self._soft_delete(models.Experiment, experiment_id, actor, entity_type=models.EntityType.EXPERIMENT)

    def restore_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Restore a soft-deleted experiment and every run/artifact deleted with it. Requires `Scope.RESTORE`."""
        self._restore(models.Experiment, experiment_id, actor, entity_type=models.EntityType.EXPERIMENT)

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted experiment and everything under it."""
        self._purge(models.Experiment, experiment_id, actor, entity_type=models.EntityType.EXPERIMENT)

    def delete_run(self, run_id: int, actor: models.User) -> None:
        """Soft-delete a run and cascade to its artifacts. Requires `Scope.RUN_DELETE`."""
        self._soft_delete(models.Run, run_id, actor, entity_type=models.EntityType.RUN)

    def restore_run(self, run_id: int, actor: models.User) -> None:
        """Restore a soft-deleted run and every artifact deleted with it. Requires `Scope.RESTORE`."""
        self._restore(models.Run, run_id, actor, entity_type=models.EntityType.RUN)

    def purge_run(self, run_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted run and everything under it."""
        self._purge(models.Run, run_id, actor, entity_type=models.EntityType.RUN)

    def delete_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Soft-delete a single artifact (a leaf -- nothing depends on it). Requires `Scope.ARTIFACT_DELETE`."""
        self._soft_delete(models.Artifact, artifact_id, actor, entity_type=models.EntityType.ARTIFACT)

    def restore_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Restore a soft-deleted artifact. Requires `Scope.RESTORE`."""
        self._restore(models.Artifact, artifact_id, actor, entity_type=models.EntityType.ARTIFACT)

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted artifact."""
        self._purge(models.Artifact, artifact_id, actor, entity_type=models.EntityType.ARTIFACT)

    def list_audit_log(
        self,
        actor: models.User,  # noqa: ARG002 -- part of the `DataStore` contract; enforced by `ScopeEnforcingDataStore`
        limit: int = 100,
        offset: int = 0,
    ) -> Iterator[models.AuditLogEntry]:
        """List audit log entries, most recent first, for a trash/admin view. Requires `Scope.AUDIT_LOG_READ`."""
        log = self._tables[models.AuditLogEntry]
        yield from self._fetch(
            models.AuditLogEntry,
            sa.select(log).order_by(log.c.timestamp_utc.desc()).limit(limit).offset(offset),
        )

    def _list_deleted[RowT: BaseModel](self, model: type[RowT], *, limit: int, offset: int) -> Iterator[RowT]:
        """
        Shared query behind every `list_deleted_*` method: most-recently-deleted first, capped.

        A project's cascade can soft-delete everything under it in one go -- easily thousands of
        artifact rows for a training-heavy project -- so this is never unbounded: callers always
        get a page, never "every row," the same discipline `list_audit_log` already follows.
        """
        table = self._tables[model]
        yield from self._fetch(
            model,
            sa.select(table)
            .where(table.c.deleted_at.is_not(None))
            .order_by(table.c.deleted_at.desc())
            .limit(limit)
            .offset(offset),
        )

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
        tasks = self._tables[models.ArtifactPurgeTask]
        yield from self._fetch(
            models.ArtifactPurgeTask,
            sa.select(tasks).order_by(tasks.c.requested_at).limit(limit).offset(offset),
        )

    def count_pending_artifact_purges(self) -> int:
        """Count artifact blobs still waiting to be deleted. 0 means the last purge fully cleaned up."""
        ((count,),) = self._execute(
            sa.select(sa.func.count()).select_from(self._tables[models.ArtifactPurgeTask])
        )
        return count

    def complete_artifact_purge(self, task_id: int) -> None:
        """Record that a queued blob deletion succeeded by deleting its task row."""
        tasks = self._tables[models.ArtifactPurgeTask]
        self._execute(sa.delete(tasks).where(tasks.c.id == task_id))

    def fail_artifact_purge(self, task_id: int, error: str) -> None:
        """Record that a queued blob deletion failed. The task stays pending and is retried later."""
        tasks = self._tables[models.ArtifactPurgeTask]
        self._execute(sa.update(tasks).where(tasks.c.id == task_id).values(last_error=error))
