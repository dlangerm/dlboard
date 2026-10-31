"""The generic base class for a sql data store."""

from __future__ import annotations

import itertools
import json
import math
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import pendulum
import sqlalchemy as sa
from pydantic import BaseModel
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._telemetry.traces import SpanKind, span
from dlboard.serve import sql
from dlboard.serve._backend._app_state import APP_STATE_ROW_ID, AppState
from dlboard.serve._backend._comparison_settings import ComparisonSettings
from dlboard.serve._backend._foreign_keys import ForeignKey, ForeignKeyKind
from dlboard.serve._backend._metric_frame import MetricFrame, MetricKeySummary, MetricRow
from dlboard.serve._backend._schema_upgrade import run_migrations
from dlboard.serve.telemetry import Counter, Outcome, OutcomeAttributes, Unit

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Sequence


_log = get_logger(__name__)

_PURGES = Counter[OutcomeAttributes](
    "dlboard.artifact.purges",
    Unit.ARTIFACTS,
    "Purged artifacts' blobs deleted from the artifact store, or that could not be.",
)

SCHEMA_PREP_OPTION: Final = "dlboard_schema_prep"
"""
Execution option marking the one transaction that creates/migrates the schema (`SQLStoreBase.__init__`).

A dialect's engine setup (e.g. sqlite's, which otherwise can't tell this transaction apart from an
ordinary query at `begin()` time) reads this via `conn.get_execution_options()` to take a stronger
lock for just this one transaction, instead of paying for it on every query for the engine's whole
lifetime.
"""

type AnyRow = sa.Row[*tuple[Any, ...]]
"""A result row of any shape -- `Row` is generic over its columns' types, which raw aggregates don't pin down."""

_OWNS = ForeignKeyKind.OWNERSHIP

_IN_BATCH: Final = 500
"""How many ids or rows one statement binds: they are bound values, and a database allows only so many in a statement."""

_STRIDE_DOUBLINGS: Final = 32
"""How many powers of two `fetch_histograms` can thin by: far more steps than any run will ever log."""


_SUMMARIES: Final[
    Mapping[models.MetricReduction, Callable[[sa.ColumnElement[Any]], sa.ColumnElement[Any]]]
] = {
    models.MetricReduction.MIN: sa.func.min,
    models.MetricReduction.MAX: sa.func.max,
    models.MetricReduction.MEAN: sa.func.avg,
}
"""What each `MetricReduction` that summarizes a whole series is, in SQL."""

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
    models.UnderlyingHistogramTableEntry: {
        "experiment_id": ForeignKey(models.Experiment),
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
    models.RunPin: {
        "run_id": ForeignKey(models.Run, _OWNS),
        "experiment_id": ForeignKey(
            models.Experiment
        ),  # denormalized convenience, like the per-step tables: not an ownership edge
        "pinned_by": ForeignKey(models.User),
    },
    models.ComparisonRun: {
        "page_id": ForeignKey(models.Page, _OWNS),
        "run_id": ForeignKey(models.Run, _OWNS),
        "added_by": ForeignKey(models.User),
    },
    models.AuditLogEntry: {"user_id": ForeignKey(models.User)},
    models.ArtifactPurgeTask: {"requested_by": ForeignKey(models.User)},
    models.ProjectGrant: {
        "project_id": ForeignKey(models.Project, _OWNS),
        "user_id": ForeignKey(models.User),
    },
    models.ApiToken: {"user_id": ForeignKey(models.User)},
    models.PasswordCredential: {"user_id": ForeignKey(models.User)},
}

# One unique constraint per tuple. A grant's grantee columns are each nullable (exactly one is set),
# and SQL treats NULLs as distinct, so a user grant and a group grant on one project never collide.
UNIQUE_COLUMNS: dict[type[BaseModel], list[tuple[str, ...]]] = {
    models.User: [("username",), ("issuer", "subject")],
    models.ProjectGrant: [("project_id", "user_id"), ("project_id", "group")],
    models.ApiToken: [("token_id",)],
    models.PasswordCredential: [("user_id",)],
    models.RunPin: [("run_id", "pinned_by", "color")],
    models.ComparisonRun: [("page_id", "run_id")],
}

# Dependency order: every table appears after the tables its foreign keys point to.
TABLES: tuple[type[BaseModel], ...] = (
    AppState,
    models.User,
    models.ApiToken,
    models.PasswordCredential,
    models.Project,
    models.ProjectGrant,
    models.Experiment,
    models.Run,
    models.UnderlyingMetricTableEntry,
    models.UnderlyingHistogramTableEntry,
    models.HyperParams,
    models.Artifact,
    models.Page,
    models.Comment,
    models.RunPin,
    models.ComparisonRun,
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


def build_metadata(schema: str | None = None) -> tuple[sa.MetaData, dict[type[BaseModel], sa.Table]]:
    """
    Declare every table (and the indexes not implied by a column alone) on one `MetaData`.

    The one place the full schema is assembled from the pydantic models -- used both to build a
    concrete store's live tables and, with `schema=None`, as the `target_metadata` the `migrations/`
    env imports to autogenerate future revisions against.
    """
    metadata = sa.MetaData(schema=schema)
    tables: dict[type[BaseModel], sa.Table] = {}
    for model in TABLES:
        tables[model] = sql.table_for(
            model, metadata, tables, FOREIGN_KEYS.get(model), UNIQUE_COLUMNS.get(model)
        )
    runs, hyperparams, metrics, histograms, artifacts, pages, run_pins, comparison_runs = (
        tables[m]
        for m in (
            models.Run,
            models.HyperParams,
            models.UnderlyingMetricTableEntry,
            models.UnderlyingHistogramTableEntry,
            models.Artifact,
            models.Page,
            models.RunPin,
            models.ComparisonRun,
        )
    )
    # `sa.Index(...)` registers itself onto its columns' table/metadata on construction, so building
    # these is enough to make them part of `metadata` -- nothing further needs to reference them.
    #
    # Every read of a per-step table is one experiment's (`fetch_*`, `summarize_*`), so each leads with
    # `experiment_id`, then walks one key's runs and their steps in order -- an index that leads with
    # anything else is one Postgres can only scan whole.
    for name, per_step in (
        ("idx_metric_lookup", metrics),
        ("idx_histogram_lookup", histograms),
        ("idx_artifact_lookup", artifacts),
    ):
        sa.Index(name, per_step.c.experiment_id, per_step.c.key, per_step.c.run_id, per_step.c.step)
    # Deleting or purging a run finds its rows by `run_id`, which nothing above leads with.
    for name, per_step in (
        ("idx_metric_run", metrics),
        ("idx_histogram_run", histograms),
        ("idx_artifact_run", artifacts),
    ):
        sa.Index(name, per_step.c.run_id)
    sa.Index("idx_run_experiment", runs.c.experiment_id, runs.c.created_at)
    sa.Index("idx_hyperparams_experiment", hyperparams.c.experiment_id, hyperparams.c.run_id)
    for field in PAGE_SCOPE_FIELDS:
        # At most one *shared* page (owner_id NULL) per scope -- exactly what `get_or_create_page`
        # relies on to make its own check-then-insert race-safe.
        sa.Index(
            f"idx_Page_shared_{field}",
            pages.c[field],
            unique=True,
            sqlite_where=pages.c.owner_id.is_(None),
            postgresql_where=pages.c.owner_id.is_(None),
        )
    # A project's pins are found by the experiments of the project, then each experiment's by when it was pinned.
    sa.Index("idx_RunPin_experiment", run_pins.c.experiment_id, run_pins.c.pinned_at)
    # A project's comparisons are listed by project and owner; the unique `(page_id, run_id)` already indexes a comparison's
    # members, and purging a run finds the comparisons it is in by `run_id`, which that does not lead with.
    sa.Index("idx_Page_project", pages.c.project_id, pages.c.owner_id)
    sa.Index("idx_ComparisonRun_run", comparison_runs.c.run_id)
    return metadata, tables


def _runs_among(
    column: sa.ColumnElement[int], run_ids: frozenset[int] | None
) -> list[sa.ColumnElement[bool]]:
    """What limits a query to the runs `run_ids` names, if it is limited to any: `None` is every run of the experiment."""
    return [] if run_ids is None else [column.in_(run_ids)]


def _in_batches(ids: Iterable[int]) -> Iterator[list[int]]:
    """`ids` in sorted batches of `_IN_BATCH`, for a statement that binds each of them."""
    return (list(batch) for batch in itertools.batched(sorted(ids), _IN_BATCH))


def _name_matches(runs: sa.Table, name_contains: str) -> list[sa.ColumnElement[bool]]:
    """What keeps the runs whose name has `name_contains` in it (whatever its case) or whose id it is, if it is any."""
    searched = name_contains.strip()
    if not searched:
        return []
    return [
        sa.or_(
            sa.func.lower(runs.c.name).contains(searched.lower(), autoescape=True),
            runs.c.id == int(searched) if searched.isdigit() else sa.false(),
        )
    ]


def _among_keys(column: sa.ColumnElement[str], keys: frozenset[str] | None) -> list[sa.ColumnElement[bool]]:
    """
    What limits a query to the `keys` named, if it is limited to any: `None` is every key.

    The names are written into the statement rather than bound: thousands of them would pass the number of
    parameters one statement may bind (32,766 on some SQLite builds, 65,535 on Postgres).
    """
    if keys is None:
        return []
    return [
        column.in_(sa.bindparam(None, sorted(keys), type_=sa.String, expanding=True, literal_execute=True))
    ]


def _live_runs(runs: sa.Table, experiment_id: int) -> list[sa.ColumnElement[bool]]:
    """What selects an experiment's runs that are not soft-deleted."""
    return [runs.c.experiment_id == experiment_id, runs.c.deleted_at.is_(None)]


def _series(
    table: sa.FromClause, experiment_id: int, key: sa.ColumnElement[str], run_id: sa.ColumnElement[int]
) -> list[sa.ColumnElement[bool]]:
    """What selects one run's rows under `key` in a per-step `table`: a prefix of its `(experiment_id, key, run_id, step)` index."""
    return [table.c.experiment_id == experiment_id, table.c.key == key, table.c.run_id == run_id]


def _is_number(value: sa.ColumnElement[float | None]) -> sa.ColumnElement[bool]:
    """
    Whether `value` is a number: not `NULL`, and not `NaN`.

    Postgres sorts `NaN` above every number, `+inf` included. (SQLite stores a `NaN` as `NULL` to begin with.)
    """
    return value <= math.inf


def _distinct_keys(table: sa.Table, experiment_id: int) -> sa.CTE:
    """
    Every key a per-step `table` holds for an experiment, ascending, found by one index probe per key.

    A loose index scan: a recursive query that hops from each key to the next one in the table's
    `(experiment_id, key, ...)` index, so the work depends on how many keys there are, never on how many
    rows each one has. `SELECT DISTINCT key` reads (and sorts) every row. The last row is a `NULL` key,
    the hop past the final one.
    """
    in_experiment = table.c.experiment_id == experiment_id
    keys = sa.select(sa.func.min(table.c.key).label("key")).where(in_experiment).cte("keys", recursive=True)
    next_key = (
        sa.select(sa.func.min(table.c.key)).where(in_experiment, table.c.key > keys.c.key).scalar_subquery()
    )
    return keys.union_all(sa.select(next_key).where(keys.c.key.is_not(None)))


_COMPOUND_SELECT_TERMS: Final = 500
"""How many SELECTs one `UNION ALL` may chain: SQLite's `SQLITE_LIMIT_COMPOUND_SELECT`, 500 by default."""


def _named(
    rows: Sequence[tuple[Any, ...]],
    columns: Sequence[tuple[str, type[sa.types.TypeEngine[Any]]]],
    name: str,
    dialect: sa.Dialect,
) -> sa.Subquery:
    """
    `rows` as a table of `columns`, to probe each by its index rather than hop through every value a table holds.

    A UNION ALL of literals rather than VALUES: SQLite cannot name the columns of a VALUES list. Its cap on a
    compound SELECT is why the literals are unioned in blocks under that cap, and the blocks unioned in turn.

    The values are written into the statement rather than bound, by the dialect's own escaping of a literal: thousands
    of them would pass the number of parameters one statement may bind (32,766 on some SQLite builds). And the
    statement is written as one text, not built as a SELECT object for each row, which took seconds to build and
    compile for 11,000 rows (0.7 s for the whole read now, and the same the first time). So a statement built on it
    must be executed `uncached` (see `_execute`), as its text is its cache key. What bounds it is the length of a statement
    (SQLite's default is 1,000,000 bytes: about 20,000 names of 30 characters).
    """
    if not rows:
        msg = f"cannot name no rows as table {name!r}: a UNION ALL needs a SELECT"
        raise ValueError(msg)
    # A driver that takes `%s` parameters has its dialect write a `%` in a literal twice, and so does `text` for the
    # whole statement: it is written once here, for `text` to do it.
    percent = "%" if dialect.paramstyle in {"format", "pyformat"} else "%%"
    escapes = [
        cast("Callable[[object], str]", sql_type().literal_processor(dialect=dialect))
        for _, sql_type in columns
    ]
    blocks = [
        "SELECT * FROM ("
        + " UNION ALL ".join(
            "SELECT "
            + ", ".join(
                f"{escape(value).replace('%%', percent)} AS {column}"
                for value, escape, (column, _) in zip(row, escapes, columns, strict=True)
            )
            for row in block
        )
        + f") AS block_{at}"
        for at, block in enumerate(itertools.batched(rows, _COMPOUND_SELECT_TERMS))
    ]
    # A `:` is how `text` marks a parameter of its own, and `\:` is how it is told there is none.
    text = " UNION ALL ".join(blocks).replace(":", "\\:")
    return (
        sa.text(text).columns(*(sa.column(column, sql_type()) for column, sql_type in columns)).subquery(name)
    )


def _named_keys(keys: frozenset[str], dialect: sa.Dialect) -> sa.Subquery:
    """`keys` as a `key` column, to probe each by its index rather than hop through every key a table holds."""
    return _named([(key,) for key in sorted(keys)], [("key", sa.String)], "logged", dialect)


def _grid_cells(first: int, last: int, grid: int) -> list[tuple[int, int]]:
    """
    The cells of a grid over the steps `first` to `last`: at most `grid` of them, as (after, up to and including).

    The cells run end to end, so every step is in exactly one, and the last ends on `last`. Their ends are every
    `stride`-th step from `first`, where `stride` is the smallest power of two that keeps them that few. A power of
    two, so a grid's cells stay put while a run trains, until its stride doubles.
    """
    stride = 1
    while True:
        ends = list(range(first, last + 1, stride))
        if ends[-1] != last:
            ends.append(last)
        if len(ends) <= grid:
            return list(itertools.pairwise([first - 1, *ends]))
        stride *= 2


def _logged_keys(table: sa.Table, runs: sa.Table, experiment_id: int) -> sa.Subquery:
    """
    The keys a per-step `table` holds for a run of the experiment that is not soft-deleted, as a `key` column.

    The keys come from `_distinct_keys`; each is kept if any live run logged it, found by one probe per key that stops at
    the first such row (#372). The probe is scalar, with no order, so the index on the key and run answers it.
    """
    keys = _distinct_keys(table, experiment_id)
    # A key's first row in a live run, found through the run join: a key a live run logged costs one probe, and a key
    # only deleted runs logged reads only those runs' rows (#372). A scalar subquery, not an EXISTS, which Postgres
    # flattens into a join over every row.
    first_live_run = (
        sa.select(table.c.run_id)
        .select_from(table)
        .join(runs, table.c.run_id == runs.c.id)
        .where(
            table.c.experiment_id == experiment_id,
            table.c.key == keys.c.key,
            *_live_runs(runs, experiment_id),
        )
        .limit(1)  # any live run will do, so no order: an order walks the run index over every row (#372)
        .correlate(keys)
        .scalar_subquery()
    )
    logged_by_live_run = first_live_run.is_not(None)
    return sa.select(keys.c.key).where(keys.c.key.is_not(None), logged_by_live_run).subquery("logged_keys")


class SQLStoreBase[T](ABC, models.DataStore[T]):
    """
    The full `DataStore` contract over a database SQLAlchemy has a dialect for. Tested on SQLite and Postgres only.

    A concrete store supplies only an `Engine` (connection handling, pooling, per-connection
    setup) and `_insert_ignoring_conflicts` -- the one statement SQLAlchemy spells per dialect.
    """

    def __init__(self, engine: sa.Engine, *, schema: str | None = None) -> None:
        """Declare every table on one `MetaData`, then bring the schema to the latest revision."""
        self._engine = engine
        self._metadata, self._tables = build_metadata(schema)

        with engine.connect() as conn:
            conn = conn.execution_options(**{SCHEMA_PREP_OPTION: True})
            with conn.begin():
                self._prepare_schema(conn)
                if schema is not None:
                    # Postgres-only: everything the baseline revision creates is unqualified (built
                    # with `schema=None`, see `migrations/env.py`), so which schema it lands in is
                    # whatever the connection's search_path resolves to for the rest of this transaction.
                    conn.execute(sa.text(f'SET search_path TO "{schema}"'))
                run_migrations(conn)
                self._verify_schema(conn)
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

    def _verify_schema(self, conn: sa.Connection) -> None:
        """
        Run last in the schema creation/migration transaction `conn` is in, after `run_migrations`.

        A no-op by default. Overridden where a migration could commit with a less obvious integrity
        problem than the database's own constraints would catch -- see `SQLLiteStore`, whose batch
        migrations run with foreign key enforcement off.
        """

    @contextmanager
    def _begin(self) -> Generator[sa.Connection]:
        """
        A transaction on a connection of its own: committed once the block is done, rolled back if it raises.

        `Engine.begin()`, but with its commit in a span. A commit is a round trip (on Postgres, a wait for the WAL to
        reach disk) that the SQL instrumentation, which only sees statements, leaves out of every trace.
        """
        with self._engine.connect() as conn, conn.begin() as transaction:
            yield conn
            with span("dlboard.store.commit", SpanKind.INTERNAL):
                transaction.commit()

    def _execute(self, statement: sa.Executable, *, uncached: bool = False) -> list[AnyRow]:
        """
        Run one statement in its own transaction, returning its rows (if it has any).

        `uncached` keeps the compiled statement out of the engine's cache, for one built on `_named`: its text, thousands
        of names long, is what the cache would be keyed by.
        """
        with self._begin() as conn:
            if uncached:
                conn = conn.execution_options(compiled_cache=None)
            result = conn.execute(statement)
            return list(result.all()) if result.returns_rows else []

    def _fetch[RowT: BaseModel](
        self, model: type[RowT], statement: sa.Executable, *, no_validate: bool = False
    ) -> list[RowT]:
        """Run one statement in its own transaction, building a `model` from each row by column name."""
        with self._begin() as conn:
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
        with self._begin() as conn:
            for statement in statements:
                result = conn.execute(statement)
                results.append(list(result.all()) if result.returns_rows else [])
        return results

    def _insert_rows(self, model: type[BaseModel], rows: Iterable[BaseModel]) -> None:
        """Bulk-insert `rows` into `model`'s table in one transaction."""
        self._insert_into([(model, rows)])

    def _insert_into(self, tables: Iterable[tuple[type[BaseModel], Iterable[BaseModel]]]) -> None:
        """Bulk-insert each table's rows, all in one transaction (one commit, however many tables)."""
        with span("dlboard.store.rows", SpanKind.INTERNAL):
            pending = [
                (self._tables[model], [sql.row_values(row, exclude=frozenset({sql.ID_KEY})) for row in rows])
                for model, rows in tables
            ]
        pending = [(table, values) for table, values in pending if values]
        if not pending:
            return
        with self._begin() as conn, span("dlboard.store.insert", SpanKind.INTERNAL):
            for table, values in pending:
                self._bulk_insert(conn, table, values)

    def _bulk_insert(self, conn: sa.Connection, table: sa.Table, rows: list[dict[str, Any]]) -> None:
        """Insert `rows` (each keyed by column) into `table` on `conn`. A dialect may load them faster than this does."""
        conn.execute(sa.insert(table), rows)

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

    def _ensure_runs_not_deleted(self, run_ids: set[int]) -> dict[int, int]:
        """
        `_ensure_not_deleted` for many runs at once: one read, not one transaction a run (#436); the experiment of each.

        Raises as the one-at-a-time check does: for a run that does not exist, or that has been soft-deleted.
        """
        if not run_ids:
            return {}
        r = self._tables[models.Run]
        found = {
            run_id: (experiment_id, deleted_at)
            for batch in _in_batches(run_ids)
            for run_id, experiment_id, deleted_at in self._execute(
                sa.select(r.c.id, r.c.experiment_id, r.c.deleted_at).where(r.c.id.in_(batch))
            )
        }
        if missing := run_ids - found.keys():
            msg = f"Run {min(missing)} does not exist"
            raise ValueError(msg)
        if deleted := {run_id for run_id, (_, deleted_at) in found.items() if deleted_at is not None}:
            msg = f"Run {min(deleted)} has been deleted"
            raise ValueError(msg)
        return {run_id: experiment_id for run_id, (experiment_id, _) in found.items()}

    def _touch_experiments(self, experiment_ids: set[int]) -> None:
        """`_touch_experiment` for many experiments, in one statement: each is still its own short transaction, not one held over the batch."""
        if not experiment_ids:
            return
        experiments = self._tables[models.Experiment]
        self._execute(
            sa.update(experiments)
            .where(experiments.c.id.in_(experiment_ids))
            .values(revision=experiments.c.revision + 1, last_activity_at=pendulum.now(pendulum.UTC))
        )

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
        project_filter: list[sa.ColumnElement[bool]] = (
            [p.c.id == project_id] if project_id is not None else []
        )
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

    def get_or_create_user(self, principal: models.Principal) -> models.User:
        """
        Get the user for `principal`'s `(issuer, subject)`, creating it if this is the first time it's been seen.

        A brand new user is granted no scopes. The exception is the very first unverified user any
        store instance ever creates (see `models.UNVERIFIED_ISSUER`): it atomically claims a one-time
        "bootstrap admin" grant (`Scope.ALL`), via `AppState.bootstrap_admin_assigned`, so a single
        local user gets full permissions with zero configuration. Every user after that gets
        nothing, so this never silently generalizes into "everyone who connects is admin" if the
        same database ends up shared by more than one person. A verified identity never claims it at
        all: on a deployment that actually authenticates people, "whoever signs in first" is not an
        acceptable way to pick an admin (see `AuthSettings.admin_users` instead).
        """
        users = self._tables[models.User]
        by_identity = sa.select(users).where(
            users.c.issuer == principal.issuer, users.c.subject == principal.subject
        )
        if existing := self._fetch(models.User, by_identity):
            return self._refresh_user(existing[0], principal)

        self._execute(
            self._insert_ignoring_conflicts(users).values(
                sql.row_values(models.NewUser.from_principal(principal))
            )
        )
        created = self._fetch(models.User, by_identity)
        if not created:
            msg = f"The username {principal.username!r} is already taken by a different identity"
            raise ValueError(msg)
        (user,) = created
        if principal.issuer != models.UNVERIFIED_ISSUER:
            return user

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

        _log.info("Granting bootstrap admin scopes to user %s (%s)", user.id, user.username)
        return self.update_user(user.model_copy(update={"scopes": [models.Scope.ALL]}))

    def _refresh_user(self, user: models.User, principal: models.Principal) -> models.User:
        """`user`, with whatever its issuer now says about its email/groups written back if it changed."""
        groups = sorted(principal.groups)
        if (user.email, user.groups) == (principal.email, groups):
            return user
        return self.update_user(user.model_copy(update={"email": principal.email, "groups": groups}))

    def get_user(self, user_id: int) -> models.User | None:
        """A user by id."""
        users = self._tables[models.User]
        return next(iter(self._fetch(models.User, sa.select(users).where(users.c.id == user_id))), None)

    def find_user(self, username: str) -> models.User | None:
        """A user by their (unique) username."""
        users = self._tables[models.User]
        return next(
            iter(self._fetch(models.User, sa.select(users).where(users.c.username == username))), None
        )

    def update_user(self, user: models.User) -> models.User:
        """Update a user, e.g. to grant/revoke scopes."""
        _log.debug("updating user %s", user.id)
        (updated,) = self._fetch(models.User, sql.update(self._tables[models.User], user))
        return updated

    def list_project_grants(self, project_ids: Collection[int]) -> list[models.ProjectGrant]:
        """Every grant on any of `project_ids`."""
        grants = self._tables[models.ProjectGrant]
        return self._fetch(
            models.ProjectGrant,
            sa.select(grants).where(grants.c.project_id.in_(project_ids)).order_by(grants.c.id),
        )

    def grants_for(self, user: models.User) -> list[models.ProjectGrant]:
        """Every grant naming `user` directly, or one of `user.groups`."""
        grants = self._tables[models.ProjectGrant]
        return self._fetch(
            models.ProjectGrant,
            sa.select(grants).where(sa.or_(grants.c.user_id == user.id, grants.c.group.in_(user.groups))),
        )

    def set_project_grant(self, grant: models.NewProjectGrant) -> models.ProjectGrant:
        """Grant a role on a project, replacing whatever that same user/group already had there."""
        grants = self._tables[models.ProjectGrant]
        grantee = (
            grants.c.user_id == grant.user_id if grant.user_id is not None else grants.c.group == grant.group
        )
        replace = sa.delete(grants).where(grants.c.project_id == grant.project_id, grantee)
        self._transaction([replace, sql.insert(grants, grant)])
        (stored,) = self._fetch(
            models.ProjectGrant, sa.select(grants).where(grants.c.project_id == grant.project_id, grantee)
        )
        return stored

    def delete_project_grant(self, project_id: int, grant_id: int) -> None:
        """Remove one of `project_id`'s grants."""
        grants = self._tables[models.ProjectGrant]
        self._execute(sa.delete(grants).where(grants.c.id == grant_id, grants.c.project_id == project_id))

    def create_api_token(self, token: models.NewApiToken) -> models.ApiToken:
        """Store a freshly minted API token's hash."""
        (created,) = self._fetch(models.ApiToken, sql.insert(self._tables[models.ApiToken], token))
        return created

    def get_api_token(self, token_id: str) -> models.ApiToken | None:
        """An API token by its public `token_id`, revoked or not."""
        tokens = self._tables[models.ApiToken]
        return next(
            iter(self._fetch(models.ApiToken, sa.select(tokens).where(tokens.c.token_id == token_id))), None
        )

    def list_api_tokens(self, user_id: int) -> list[models.ApiToken]:
        """`user_id`'s API tokens, newest first, including revoked ones."""
        tokens = self._tables[models.ApiToken]
        return self._fetch(
            models.ApiToken,
            sa.select(tokens).where(tokens.c.user_id == user_id).order_by(tokens.c.id.desc()),
        )

    def revoke_api_token(self, api_token_id: int, user_id: int) -> None:
        """Revoke one of `user_id`'s API tokens; anyone else's is left alone."""
        tokens = self._tables[models.ApiToken]
        self._execute(
            sa.update(tokens)
            .where(tokens.c.id == api_token_id, tokens.c.user_id == user_id, tokens.c.revoked_at.is_(None))
            .values(revoked_at=pendulum.now(pendulum.UTC))
        )

    def touch_api_token(self, api_token_id: int) -> None:
        """Record that an API token was just used."""
        tokens = self._tables[models.ApiToken]
        self._execute(
            sa.update(tokens)
            .where(tokens.c.id == api_token_id)
            .values(last_used_at=pendulum.now(pendulum.UTC))
        )

    def get_password_credential(self, user_id: int) -> models.PasswordCredential | None:
        """`user_id`'s password hash, if they have one."""
        creds = self._tables[models.PasswordCredential]
        return next(
            iter(self._fetch(models.PasswordCredential, sa.select(creds).where(creds.c.user_id == user_id))),
            None,
        )

    def set_password_credential(self, credential: models.PasswordCredential) -> None:
        """Set (or replace) a user's password hash."""
        creds = self._tables[models.PasswordCredential]
        self._transaction(
            [
                sa.delete(creds).where(creds.c.user_id == credential.user_id),
                sql.insert(creds, credential.model_copy(update={"id": None})),
            ]
        )

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

    def get_project(self, database_id: int) -> models.Project | None:
        """A (non-deleted) project by id, or `None` if it doesn't exist (or has been deleted)."""
        _log.debug("getting project %s", database_id)
        return next(iter(self._by_id(models.Project, database_id)), None)

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

    def get_run(self, run_id: int) -> models.Run | None:
        """A (non-deleted) run by id."""
        return next(iter(self._by_id(models.Run, run_id)), None)

    def finish_run(self, run_id: int, status: models.RunStatus, ended_at: datetime) -> models.Run:
        """Record that a run is done: its `status` and when it `ended_at`. Raises if it is missing or deleted."""
        self._ensure_not_deleted(models.Run, run_id)
        runs = self._tables[models.Run]
        (finished,) = self._fetch(
            models.Run,
            sa.update(runs)
            .where(runs.c.id == run_id)
            .values(status=status.value, ended_at=ended_at)
            .returning(runs),
        )
        self._touch_experiment(finished.experiment_id)
        return finished

    def get_runs(
        self,
        experiment_id: int,
        *,
        limit: int | None = 1000,
        offset: int = 0,
        run_ids: frozenset[int] | None,
        name_contains: str = "",
    ) -> Iterator[models.Run]:
        """A page (`limit` of `None` being every one) of an experiment's (non-deleted) runs, most recently created first; only `run_ids`, if given, and those `name_contains` finds."""
        runs = self._tables[models.Run]
        return iter(
            self._fetch(
                models.Run,
                sa.select(runs)
                .where(
                    runs.c.experiment_id == experiment_id,
                    runs.c.deleted_at.is_(None),
                    *_runs_among(runs.c.id, run_ids),
                    *_name_matches(runs, name_contains),
                )
                .order_by(runs.c.created_at.desc())
                .limit(limit)
                .offset(offset),
            )
        )

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log a batch of metrics to the data store."""
        _log.debug("Logging metrics batch")
        with span("dlboard.metrics.parse", SpanKind.INTERNAL):
            metric = list(metric)
        for run_id in {m.run_id for m in metric}:
            self._ensure_not_deleted(models.Run, run_id)
        self._insert_into(
            [
                (
                    models.UnderlyingMetricTableEntry,
                    itertools.chain.from_iterable(m.to_underlying() for m in metric),
                ),
                (
                    models.UnderlyingHistogramTableEntry,
                    itertools.chain.from_iterable(m.to_underlying_histograms() for m in metric),
                ),
            ]
        )
        for experiment_id in {m.experiment_id for m in metric}:
            self._touch_experiment(experiment_id)

    def fetch_metrics(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        run_ids: frozenset[int] | None,
        reduction: models.MetricReduction = models.MetricReduction.HISTORY,
    ) -> MetricFrame:
        """
        An experiment's (non-deleted runs') metrics -- only `keys`, if given, else every metric.

        Every step, unless a `reduction` asks for less: then a row per run and key, found without reading the rest.
        """
        match reduction:
            case models.MetricReduction.HISTORY:
                rows = self._metric_history(experiment_id, keys, run_ids)
            case models.MetricReduction.LATEST:
                rows = self._metric_ends(experiment_id, keys, run_ids, latest=True)
            case models.MetricReduction.FIRST:
                rows = self._metric_ends(experiment_id, keys, run_ids, latest=False)
            case models.MetricReduction.MIN | models.MetricReduction.MAX | models.MetricReduction.MEAN:
                rows = self._metric_summaries(experiment_id, keys, run_ids, _SUMMARIES[reduction])
        return MetricFrame.from_rows(rows)

    def _metric_history(
        self,
        experiment_id: int,
        keys: frozenset[str] | None,
        run_ids: frozenset[int] | None,
    ) -> Sequence[AnyRow]:
        # `UnderlyingMetricTableEntry` has no `deleted_at` of its own -- visibility is inherited
        # transitively through its run, which is always soft-deleted in the same cascade as its
        # metrics' logical owner (see `_soft_delete`), so a join against `Run` is sufficient.
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        key_filter = _among_keys(m.c.key, keys)
        # The timestamp is left as stored: `MetricFrame.from_rows` has to read it as a column of timestamps anyway, and
        # parsing each one here as well (the column type does) was a third of what this cost.
        stored = sa.type_coerce(m.c.timestamp_utc, sa.types.NullType()).label("timestamp_utc")
        # `ORDER BY m.id` is insertion order, which is what `MetricFrame.from_rows`' last-write-wins needs.
        return self._execute(
            sa.select(*(stored if f == "timestamp_utc" else m.c[f] for f in MetricRow._fields))
            .join(r, m.c.run_id == r.c.id)
            .where(
                m.c.experiment_id == experiment_id,
                r.c.deleted_at.is_(None),
                *_runs_among(m.c.run_id, run_ids),
                *key_filter,
            )
            .order_by(m.c.id)
        )

    def check_stored_timestamps(self) -> None:
        """
        Raise unless the driver returns a stored metric timestamp as an ISO string or an aware datetime (#468).

        `_metric_history` reads the timestamp column raw, for speed, and `MetricFrame.from_rows` accepts exactly those two
        forms. A driver that returns anything else would fail deep in a chart, so startup checks one stored row. An
        empty experiment has nothing to check, so this passes until something is logged.
        """
        m = self._tables[models.UnderlyingMetricTableEntry]
        rows = self._execute(sa.select(sa.type_coerce(m.c.timestamp_utc, sa.types.NullType())).limit(1))
        if not rows:
            return
        ((value,),) = rows
        if isinstance(value, str) or (isinstance(value, datetime) and value.tzinfo is not None):
            return
        msg = f"the driver returned a stored timestamp as {type(value).__name__}, not an ISO string or aware datetime"
        raise RuntimeError(msg)

    def _metric_ends(
        self,
        experiment_id: int,
        keys: frozenset[str] | None,
        run_ids: frozenset[int] | None,
        *,
        latest: bool,
    ) -> list[MetricRow]:
        """
        Each live run's last (or first) row of each key, found by one probe through the index per run and key.

        The probe is only an ordered read of the key's series: the first row in step order is the newest write of
        its step, so it is the answer unless its value is missing. A key whose last value is missing (a NaN, the
        last step of a diverged run) is settled by `_settled_ends`, which walks back to the last value there is.
        """
        if keys is not None and not keys:
            return []
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        # Asked for by name, the keys are probed directly; only "every metric" has to find out which keys there are.
        logged = _distinct_keys(m, experiment_id) if keys is None else _named_keys(keys, self._engine.dialect)
        end = (m.c.step.desc() if latest else m.c.step.asc(), m.c.id.desc())
        if self._engine.dialect.name == "postgresql":
            # One lateral probe per run and key, reading the end row's own columns: no second lookup by its id (#373).
            end_row = (
                sa.select(m.c.step, m.c.value, m.c.timestamp_utc)
                .where(*_series(m, experiment_id, logged.c.key, r.c.id))
                .order_by(*end)
                .limit(1)
                .correlate(logged, r)
                .lateral("end_row")
            )
            ends_of = (
                sa.select(
                    r.c.id.label("run_id"),
                    end_row.c.step,
                    logged.c.key,
                    end_row.c.value,
                    end_row.c.timestamp_utc,
                )
                .select_from(logged.join(r, sa.true()).join(end_row, sa.true()))
                .where(
                    logged.c.key.is_not(None), *_live_runs(r, experiment_id), *_runs_among(r.c.id, run_ids)
                )
            )
        else:
            # SQLite has no LATERAL: the end row is found, then read by its id, as before.
            end_id = (
                sa.select(m.c.id)
                .where(*_series(m, experiment_id, logged.c.key, r.c.id))
                .order_by(*end)
                .limit(1)
                .correlate(logged, r)
                .scalar_subquery()
            )
            ends = (
                sa.select(r.c.id.label("run_id"), logged.c.key.label("key"), end_id.label("end_id"))
                .select_from(logged.join(r, sa.true()))
                .where(
                    logged.c.key.is_not(None), *_live_runs(r, experiment_id), *_runs_among(r.c.id, run_ids)
                )
                .cte("ends")
            )
            found = m.alias("found")

            def of_the_end(column: sa.ColumnElement[Any]) -> sa.ScalarSelect[Any]:
                return sa.select(column).where(found.c.id == ends.c.end_id).correlate(ends).scalar_subquery()

            ends_of = sa.select(
                ends.c.run_id,
                of_the_end(found.c.step).label("step"),
                ends.c.key,
                of_the_end(found.c.value).label("value"),
                of_the_end(found.c.timestamp_utc).label("timestamp_utc"),
            ).where(ends.c.end_id.is_not(None))
        rows = [MetricRow._make(row) for row in self._execute(ends_of, uncached=keys is not None)]
        unsettled = {(row.run_id, row.key) for row in rows if row.value is None or math.isnan(row.value)}
        if not unsettled:
            return [row for row in rows if row.value is not None]
        settled = {
            (row.run_id, row.key): row
            for row in self._settled_ends(experiment_id, sorted(unsettled), latest=latest)
        }
        kept = [row for row in rows if (row.run_id, row.key) not in unsettled and row.value is not None]
        return kept + [settled[pair] for pair in unsettled if pair in settled]

    def _settled_ends(
        self, experiment_id: int, pairs: Sequence[tuple[int, str]], *, latest: bool
    ) -> list[MetricRow]:
        """
        For each of `pairs` (a run and a key), its last (or first) row that is a number, by a probe through the index.

        Only for the pairs whose last row is missing (a NaN, the last step of a diverged run), so that is all it reads,
        however many runs and keys were asked for. The probe walks back over the series to the last value there is: a
        series that ends in a long stretch of missing values reads that stretch. A step logged twice (a re-sent batch)
        counts as its newest write, as in `MetricFrame.from_rows`.
        """
        m = self._tables[models.UnderlyingMetricTableEntry]
        named = _named(pairs, [("run_id", sa.Integer), ("key", sa.String)], "unsettled", self._engine.dialect)
        end = (m.c.step.desc() if latest else m.c.step.asc(), m.c.id.desc())
        rewritten = m.alias("rewritten")
        # The newest write of a step is the one that counts, a missing value included: that step then has none.
        newest_write = ~sa.exists().where(
            *_series(rewritten, experiment_id, m.c.key, m.c.run_id),
            rewritten.c.step == m.c.step,
            rewritten.c.id > m.c.id,
        )

        def at_the_end(column: sa.ColumnElement[Any]) -> sa.ScalarSelect[Any]:
            return (
                sa.select(column)
                .where(
                    *_series(m, experiment_id, named.c.key, named.c.run_id),
                    _is_number(m.c.value),
                    newest_write,
                )
                .order_by(*end)
                .limit(1)
                .correlate(named)
                .scalar_subquery()
            )

        ends = sa.select(
            named.c.run_id,
            at_the_end(m.c.step).label("step"),
            named.c.key,
            at_the_end(m.c.value).label("value"),
            at_the_end(m.c.timestamp_utc).label("timestamp_utc"),
        ).subquery("ends")
        rows = self._execute(
            sa.select(*(ends.c[f] for f in MetricRow._fields)).where(ends.c.value.is_not(None)), uncached=True
        )
        return [MetricRow._make(row) for row in rows]

    def _metric_summaries(
        self,
        experiment_id: int,
        keys: frozenset[str] | None,
        run_ids: frozenset[int] | None,
        summarize: Callable[[sa.ColumnElement[Any]], sa.ColumnElement[Any]],
    ) -> list[MetricRow]:
        """
        One row per live run and key: `summarize` of its values, at its last step.

        The database reads every step of the keys asked for and sends back a row per run and key.
        A step logged twice (a re-sent batch) counts as its newest write, as in `MetricFrame.from_rows`.
        """
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        key_filter = _among_keys(m.c.key, keys)
        written = (
            sa.select(
                m.c.run_id,
                m.c.key,
                m.c.step,
                m.c.value,
                m.c.timestamp_utc,
                sa.func.row_number()
                .over(partition_by=(m.c.run_id, m.c.key, m.c.step), order_by=m.c.id.desc())
                .label("newest"),
            )
            .join(r, m.c.run_id == r.c.id)
            .where(
                m.c.experiment_id == experiment_id,
                r.c.deleted_at.is_(None),
                *_runs_among(m.c.run_id, run_ids),
                *key_filter,
            )
            .subquery("written")
        )
        rows = self._execute(
            sa.select(
                written.c.run_id,
                sa.func.max(written.c.step).label("step"),
                written.c.key,
                summarize(written.c.value).label("value"),
                sa.func.max(written.c.timestamp_utc).label("timestamp_utc"),
            )
            .where(written.c.newest == 1, _is_number(written.c.value))
            .group_by(written.c.run_id, written.c.key)
        )
        return [MetricRow._make(row) for row in rows]

    def summarize_metric_keys(self, experiment_id: int) -> list[str]:
        """Every metric key logged in an experiment (non-deleted runs), sorted, without judging or fetching any."""
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        keys = _logged_keys(m, r, experiment_id)
        return [key for (key,) in self._execute(sa.select(keys.c.key).order_by(keys.c.key))]

    def summarize_metric_steps(self, experiment_id: int, keys: frozenset[str]) -> list[MetricKeySummary]:
        """
        How many steps each of `keys` logged in a run, at most: the longest span of steps, judged on the newest runs.

        Only `keys` are probed, so the reads are bounded by how many keys are asked for and by `SUMMARY_RUNS`. A key
        no live run logged comes back with 0.
        """
        if not keys:
            return []
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        named = _named_keys(keys, self._engine.dialect)
        # Judged on the newest runs only, so what it reads is bounded by `SUMMARY_RUNS`, not by how many runs there are.
        newest = (
            sa.select(r.c.id.label("run_id"))
            .where(*_live_runs(r, experiment_id))
            .order_by(r.c.created_at.desc())
            .limit(models.SUMMARY_RUNS)
            .subquery("newest")
        )
        first_step, last_step = (
            sa.select(extent(m.c.step))
            .where(*_series(m, experiment_id, named.c.key, newest.c.run_id))
            .correlate(named, newest)
            .scalar_subquery()
            for extent in (sa.func.min, sa.func.max)
        )
        longest = (
            sa.select(sa.func.max(last_step - first_step + 1))
            .select_from(newest)
            .correlate(named)
            .scalar_subquery()
        )
        rows = self._execute(
            sa.select(named.c.key, sa.func.coalesce(longest, 0)).order_by(named.c.key), uncached=True
        )
        return [MetricKeySummary(key, max_steps_per_run) for key, max_steps_per_run in rows]

    def fetch_series_band(
        self,
        experiment_id: int,
        *,
        groups: Mapping[str, str],
        run_ids: frozenset[int] | None,
        grid: int,
    ) -> list[models.SeriesBand]:
        """
        Per live run and group, the spread of its keys' values over the cells of a grid of at most `grid` steps.

        A key counts in a cell if its newest write there is a number, a step logged twice counting its newest write as
        in `MetricFrame.from_rows`. The quantiles are nearest-rank. What it reads is an ordered probe of the index for
        each key, run and cell (the key's newest write in the cell), and one pass over those values, so it does not
        grow with the steps a run trained, and a run that logs every 50 steps is as well covered as one that logs every
        step.
        """
        if not groups:
            return []
        m, r = self._tables[models.UnderlyingMetricTableEntry], self._tables[models.Run]
        labels = sorted(set(groups.values()))
        label_ids = {label: at for at, label in enumerate(labels)}
        logged = _named(
            [(key, label_ids[label]) for key, label in sorted(groups.items())],
            [("key", sa.String), ("grp", sa.Integer)],
            "logged",
            self._engine.dialect,
        )
        runs = (
            sa.select(r.c.id.label("run_id"))
            .where(*_live_runs(r, experiment_id), *_runs_among(r.c.id, run_ids))
            .subquery("runs")
        )

        def an_end_of_the_series(order: sa.UnaryExpression[int]) -> sa.ScalarSelect[Any]:
            return (
                sa.select(m.c.step)
                .where(*_series(m, experiment_id, logged.c.key, runs.c.run_id))
                .order_by(order)
                .limit(1)
                .correlate(logged, runs)
                .scalar_subquery()
            )

        ends = (
            sa.select(
                an_end_of_the_series(m.c.step.asc()).label("first"),
                an_end_of_the_series(m.c.step.desc()).label("last"),
            )
            .select_from(logged.join(runs, sa.true()))
            .subquery("ends")
        )
        first, last = self._execute(
            sa.select(sa.func.min(ends.c.first), sa.func.max(ends.c.last)), uncached=True
        )[0]
        if first is None or last is None:
            return []
        at_cell = _named(
            _grid_cells(first, last, grid),
            [("after", sa.Integer), ("until", sa.Integer)],
            "cells",
            self._engine.dialect,
        )
        # The newest write of each key in each run's cell: one ordered probe of the index per combination, with
        # nothing in it but the cell's bounds, so the planner has no other plan to choose.
        newest_value = (
            sa.select(m.c.value)
            .where(
                *_series(m, experiment_id, logged.c.key, runs.c.run_id),
                m.c.step > at_cell.c.after,
                m.c.step <= at_cell.c.until,
            )
            .order_by(m.c.step.desc(), m.c.id.desc())
            .limit(1)
            .correlate(logged, runs, at_cell)
            .scalar_subquery()
        )
        # `OFFSET 0` keeps the databases from folding the probe into each place its value is used, which would run it
        # again for each; and the value is read by the probe itself, since a join back to the table is one Postgres
        # may plan as a scan of every experiment's rows.
        written = (
            sa.select(
                runs.c.run_id,
                logged.c.grp,
                at_cell.c.until.label("step"),
                logged.c.key,
                newest_value.label("value"),
            )
            .select_from(logged.join(runs, sa.true()).join(at_cell, sa.true()))
            .offset(0)
            .subquery("written")
        )
        cell = (written.c.run_id, written.c.grp, written.c.step)
        ranked = (
            sa.select(
                *written.c,
                sa.func.row_number()
                .over(partition_by=cell, order_by=(written.c.value, written.c.key))
                .label("rank"),
                sa.func.count().over(partition_by=cell).label("n"),
            )
            .where(_is_number(written.c.value))
            .subquery("ranked")
        )

        def at_rank(
            position: sa.ColumnElement[Any], column: sa.ColumnElement[Any] = ranked.c.value
        ) -> sa.ColumnElement[Any]:
            return sa.func.min(sa.case((ranked.c.rank == position, column)))

        def nearest_rank(percent: int) -> sa.ColumnElement[Any]:
            # ceil(n * percent / 100) is (x - x % 100) / 100 for x = n * percent + 99: the division then comes out whole on
            # every database, where dividing a non-whole number by 100 is done as a float on one and rounded on another.
            total = ranked.c.n * percent + 99
            return sa.cast((total - total % 100) / 100, sa.Integer)

        rows = self._execute(
            sa.select(
                ranked.c.run_id,
                ranked.c.grp,
                ranked.c.step,
                sa.func.max(ranked.c.n),
                at_rank(sa.literal(1)),
                at_rank(nearest_rank(25)),
                at_rank(nearest_rank(50)),
                at_rank(nearest_rank(75)),
                at_rank(ranked.c.n),
                sa.func.avg(ranked.c.value),
                at_rank(sa.literal(1), ranked.c.key),
                at_rank(ranked.c.n, ranked.c.key),
            )
            .group_by(ranked.c.run_id, ranked.c.grp, ranked.c.step)
            .order_by(ranked.c.run_id, ranked.c.grp, ranked.c.step),
            uncached=True,
        )
        return [models.SeriesBand._make((run_id, labels[grp], *rest)) for run_id, grp, *rest in rows]

    def fetch_histograms(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str],
        max_steps: int,
        run_ids: frozenset[int] | None,
    ) -> list[models.UnderlyingHistogramTableEntry]:
        """
        An experiment's (non-deleted runs') histograms under `keys`, thinned to about `max_steps` steps per run and key.

        What bounds a render's cost however long a run has trained: the thinning is done here, in the database,
        and only the histograms that survive it are read back -- the payloads of the rest never leave the table.
        A run and key with at most `max_steps` steps keep all of them. A longer one keeps every `stride`-th step
        (counting from its first) and always its latest, where `stride` is the smallest power of two that fits:
        a power of two, so the steps kept stay put while a run trains, until the stride doubles, instead of every
        poll picking a different set.
        """
        h, r = self._tables[models.UnderlyingHistogramTableEntry], self._tables[models.Run]
        # A batch the client re-sent after a failure holds a step twice: the newest write wins, as it does for metrics.
        latest = (
            sa.select(sa.func.max(h.c.id).label("id"))
            .select_from(h)
            .join(r, h.c.run_id == r.c.id)
            .where(
                h.c.experiment_id == experiment_id,
                r.c.deleted_at.is_(None),
                *_runs_among(h.c.run_id, run_ids),
                h.c.key.in_(keys),
            )
            .group_by(h.c.run_id, h.c.key, h.c.step)
            .subquery("latest")
        )
        series = (h.c.run_id, h.c.key)
        ranked = (
            sa.select(
                h.c.id,
                sa.func.row_number().over(partition_by=series, order_by=h.c.step).label("position"),
                sa.func.count().over(partition_by=series).label("total"),
            )
            .join(latest, h.c.id == latest.c.id)
            .subquery("ranked")
        )
        # Explicitly 64-bit: Postgres' driver casts an integer bound against `total` to `INTEGER`, which `max_steps << k`
        # outgrows (sqlite has no such limit, so only Postgres can tell).
        stride = sa.case(
            *(
                (
                    ranked.c.total <= sa.literal(max_steps << k, sa.BigInteger),
                    sa.literal(1 << k, sa.BigInteger),
                )
                for k in range(_STRIDE_DOUBLINGS)
            ),
            else_=sa.literal(1 << _STRIDE_DOUBLINGS, sa.BigInteger),
        )
        kept = sa.select(ranked.c.id).where(
            sa.or_((ranked.c.position - 1) % stride == 0, ranked.c.position == ranked.c.total)
        )
        return self._fetch(
            models.UnderlyingHistogramTableEntry,
            sa.select(h).where(h.c.id.in_(kept)).order_by(h.c.run_id, h.c.key, h.c.step),
            no_validate=True,
        )

    def summarize_histogram_keys(self, experiment_id: int) -> list[str]:
        """Every histogram key logged in an experiment (non-deleted runs), sorted, without fetching any."""
        keys = _logged_keys(
            self._tables[models.UnderlyingHistogramTableEntry], self._tables[models.Run], experiment_id
        )
        return [key for (key,) in self._execute(sa.select(keys.c.key).order_by(keys.c.key))]

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
        self, experiment_id: int, *, run_ids: frozenset[int] | None
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
                *_runs_among(h.c.run_id, run_ids),
            ),
        )

    def get_or_create_page[D, C](
        self,
        page_type: type[models.Page[D, C]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[D, C]] | None = None,
    ) -> models.Page[D, C]:
        field, value = _resolve_page_scope(run_id=run_id, experiment_id=experiment_id, project_id=project_id)
        if field == "project_id":
            # A page of a project is a comparison, which is someone's. Were there a shared one (`owner_id` NULL, as an
            # experiment's is), `_page_repository.ref_of` would take it for a comparison it cannot load.
            msg = "A project's pages are comparisons, which are someone's: save one with `create_comparison`"
            raise ValueError(msg)
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
        new_page_fields: dict[str, Any] = {field: value}
        new_page = (new_page_type or models.NewPage[D, C])(**new_page_fields)
        self._execute(self._insert_ignoring_conflicts(pages).values(sql.row_values(new_page)))
        (created,) = self._fetch(page_type, shared)
        return created

    def create_view[D, C](
        self, page_type: type[models.Page[D, C]], view: models.NewPage[D, C]
    ) -> models.Page[D, C]:
        """Save `view` -- a named, owned copy of a page's panels and settings -- as its own page."""
        if view.owner_id is None or not view.name:
            msg = "A view needs an owner and a name"
            raise ValueError(msg)
        _resolve_page_scope(run_id=view.run_id, experiment_id=view.experiment_id, project_id=view.project_id)
        (created,) = self._fetch(page_type, sql.insert(self._tables[models.Page], view))
        return created

    def create_comparison[D, C](
        self,
        page_type: type[models.Page[D, C]],
        comparison: models.NewPage[D, C],
        run_ids: frozenset[int],
    ) -> models.Page[D, C]:
        """Save `comparison` -- a named, owned page scoped to a project -- with the runs of `run_ids` that can be members."""
        if comparison.owner_id is None or not comparison.name:
            msg = "A comparison needs an owner and a name"
            raise ValueError(msg)
        field, project_id = _resolve_page_scope(
            run_id=comparison.run_id, experiment_id=comparison.experiment_id, project_id=comparison.project_id
        )
        if field != "project_id":
            msg = "A comparison is scoped to a project"
            raise ValueError(msg)
        with self._begin() as conn:
            row = conn.execute(sql.insert(self._tables[models.Page], comparison)).mappings().one()
            created = sql.construct(page_type, row)
            self._add_members(conn, created.id, project_id, run_ids, added_by=comparison.owner_id)
        return created

    def _members_in_project(
        self,
    ) -> tuple[sa.Table, sa.Table, sa.Table, sa.Table, sa.Join, list[sa.ColumnElement[bool]]]:
        """
        The tables of a comparison's members, and what keeps the members that are still its to show.

        A member is shown while its run and its experiment are not deleted (restoring either brings it back) and its
        experiment is in the comparison's own project: an experiment can be moved to another project, and its runs must
        not follow it into a comparison of people who can no longer see it.
        """
        pages, members, runs, experiments = (
            self._tables[m] for m in (models.Page, models.ComparisonRun, models.Run, models.Experiment)
        )
        joined = (
            members.join(pages, pages.c.id == members.c.page_id)
            .join(runs, runs.c.id == members.c.run_id)
            .join(experiments, experiments.c.id == runs.c.experiment_id)
        )
        return (
            pages,
            members,
            runs,
            experiments,
            joined,
            [
                runs.c.deleted_at.is_(None),
                experiments.c.deleted_at.is_(None),
                experiments.c.project_id == pages.c.project_id,
            ],
        )

    def comparison_runs(self, page_id: int) -> list[models.Run]:
        """The members of a comparison that are still there, newest first."""
        _pages, members, runs, _experiments, joined, shown = self._members_in_project()
        return self._fetch(
            models.Run,
            sa.select(runs)
            .select_from(joined)
            .where(members.c.page_id == page_id, *shown)
            .order_by(runs.c.created_at.desc(), runs.c.id.desc()),
        )

    def add_comparison_runs(self, page_id: int, run_ids: frozenset[int], *, added_by: int) -> int:
        """Add the runs of `run_ids` that belong to the comparison's project and are still there; how many were new."""
        pages = self._tables[models.Page]
        with self._begin() as conn:
            # Locked until this commits, so two adds to one comparison are counted one after the other (on a database
            # with row locks: a one-writer database has no two).
            project_id = conn.execute(
                sa.select(pages.c.project_id)
                .where(pages.c.id == page_id, pages.c.owner_id.is_not(None))
                .with_for_update()
            ).scalar_one_or_none()
            if project_id is None:
                msg = f"Comparison {page_id} does not exist"
                raise ValueError(msg)
            return self._add_members(conn, page_id, project_id, run_ids, added_by=added_by)

    def _add_members(
        self, conn: sa.Connection, page_id: int, project_id: int, run_ids: frozenset[int], *, added_by: int
    ) -> int:
        """`add_comparison_runs` on `conn`, so that `create_comparison` can add a comparison's first members in its transaction."""
        members, runs, experiments = (
            self._tables[m] for m in (models.ComparisonRun, models.Run, models.Experiment)
        )
        eligible = {
            run_id
            for batch in _in_batches(run_ids)
            for run_id in conn.execute(
                sa.select(runs.c.id)
                .select_from(runs.join(experiments, experiments.c.id == runs.c.experiment_id))
                .where(
                    runs.c.id.in_(batch),
                    experiments.c.project_id == project_id,
                    runs.c.deleted_at.is_(None),
                    experiments.c.deleted_at.is_(None),
                )
            ).scalars()
        }
        # Every member counts, a deleted one too: restoring it must not take the comparison past its limit.
        held = set(conn.execute(sa.select(members.c.run_id).where(members.c.page_id == page_id)).scalars())
        limit = ComparisonSettings().max_comparison_runs
        if len(held | eligible) > limit:
            msg = (
                f"A comparison holds at most {limit} runs: it has {len(held)}, "
                f"so {max(limit - len(held), 0)} more fit"
            )
            raise ValueError(msg)
        new = eligible - held
        for batch in _in_batches(new):
            conn.execute(
                self._insert_ignoring_conflicts(members).values(
                    [
                        sql.row_values(
                            models.NewComparisonRun(page_id=page_id, run_id=run_id, added_by=added_by),
                            exclude=frozenset({sql.ID_KEY}),
                        )
                        for run_id in batch
                    ]
                )
            )
        return len(new)

    def remove_comparison_runs(self, page_id: int, run_ids: frozenset[int]) -> None:
        """Take `run_ids` out of a comparison."""
        members = self._tables[models.ComparisonRun]
        self._transaction(
            sa.delete(members).where(members.c.page_id == page_id, members.c.run_id.in_(batch))
            for batch in _in_batches(run_ids)
        )

    def _comparison_summaries(
        self, where: list[sa.ColumnElement[bool]], viewer_id: int
    ) -> list[models.ComparisonSummary]:
        """The comparisons that `where` (over `Page`) keeps, the viewer's own first, then by name."""
        pages, members, runs, experiments = (
            self._tables[m] for m in (models.Page, models.ComparisonRun, models.Run, models.Experiment)
        )
        users = self._tables[models.User]
        run_count = (
            sa.select(sa.func.count())
            .select_from(
                members.join(runs, runs.c.id == members.c.run_id).join(
                    experiments, experiments.c.id == runs.c.experiment_id
                )
            )
            .where(
                members.c.page_id == pages.c.id,
                runs.c.deleted_at.is_(None),
                experiments.c.deleted_at.is_(None),
                experiments.c.project_id == pages.c.project_id,
            )
            .scalar_subquery()
        )
        rows = self._execute(
            sa.select(pages.c.id, pages.c.name, pages.c.owner_id, users.c.username, pages.c.shared, run_count)
            .select_from(pages.join(users, users.c.id == pages.c.owner_id))
            .where(pages.c.project_id.is_not(None), pages.c.owner_id.is_not(None), *where)
            .order_by((pages.c.owner_id != viewer_id), pages.c.name, pages.c.id)
        )
        return [
            models.ComparisonSummary(
                id=id_, name=name, owner_id=owner_id, owner_name=owner_name, shared=shared, run_count=count
            )
            for id_, name, owner_id, owner_name, shared, count in rows
        ]

    def list_comparisons(
        self,
        project_id: int,
        viewer_id: int,
        *,
        editable: bool = False,  # noqa: ARG002 -- this store has no access control to narrow by
    ) -> list[models.ComparisonSummary]:
        """
        `viewer_id`'s own comparisons of a project, then anyone's shared ones, by name.

        With `editable`, only those whose runs the viewer may change. Here that is all of them, since anyone who can
        see one may: who may is the authorizing store's to say.
        """
        pages = self._tables[models.Page]
        return self._comparison_summaries(
            [
                pages.c.project_id == project_id,
                sa.or_(pages.c.owner_id == viewer_id, pages.c.shared.is_(True)),
            ],
            viewer_id,
        )

    def comparison_summary(self, page_id: int) -> models.ComparisonSummary | None:
        """One comparison as `list_comparisons` lists it, or `None` if there is no such comparison."""
        found = self._comparison_summaries([self._tables[models.Page].c.id == page_id], viewer_id=0)
        return next(iter(found), None)

    def can_change_comparison_runs(self, page_id: int) -> bool:
        """Whether a comparison's runs can be changed: here by anyone, for any comparison there is."""
        pages = self._tables[models.Page]
        return bool(
            self._execute(
                sa.select(pages.c.id).where(
                    pages.c.id == page_id, pages.c.project_id.is_not(None), pages.c.owner_id.is_not(None)
                )
            )
        )

    def get_view[D, C](self, page_type: type[models.Page[D, C]], view_id: int) -> models.Page[D, C] | None:
        """A named view by id, or `None` if there's no such view (a shared page's id doesn't count)."""
        pages = self._tables[models.Page]
        rows = self._fetch(
            page_type, sa.select(pages).where(pages.c.id == view_id, pages.c.owner_id.is_not(None))
        )
        return next(iter(rows), None)

    def list_views(self, experiment_id: int, viewer_id: int) -> list[models.ViewSummary]:
        """`viewer_id`'s own views of an experiment's page, plus anyone else's shared ones, by name."""
        pages = self._tables[models.Page]
        rows = self._execute(
            sa.select(pages.c.id, pages.c.name)
            .where(
                pages.c.experiment_id == experiment_id,
                sa.or_(pages.c.owner_id == viewer_id, pages.c.shared.is_(True)),
            )
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

    def pin_runs(
        self,
        pinned_by: int,
        run_ids: frozenset[int],
        color: models.PinColor = models.PinColor.PLAIN,
    ) -> None:
        """Pin `run_ids` for `pinned_by` in `color`, a run already pinned in it keeping its pin."""
        experiments = self._ensure_runs_not_deleted(set(run_ids))
        pins = self._tables[models.RunPin]
        now = pendulum.now(pendulum.UTC)
        self._transaction(
            self._insert_ignoring_conflicts(pins).values(
                [
                    sql.row_values(
                        models.NewRunPin(
                            run_id=run_id,
                            experiment_id=experiments[run_id],
                            pinned_by=pinned_by,
                            color=color,
                            pinned_at=now,
                        ),
                        exclude=frozenset({sql.ID_KEY}),
                    )
                    for run_id in batch
                ]
            )
            for batch in _in_batches(run_ids)
        )

    def unpin_runs(self, pinned_by: int, run_ids: frozenset[int], color: models.PinColor) -> None:
        """Remove `pinned_by`'s pin of `color` from each of `run_ids`; anyone else's, and other colours', are left alone."""
        pins = self._tables[models.RunPin]
        self._transaction(
            sa.delete(pins).where(
                pins.c.pinned_by == pinned_by, pins.c.color == color, pins.c.run_id.in_(batch)
            )
            for batch in _in_batches(run_ids)
        )

    def run_experiments(self, run_ids: frozenset[int]) -> dict[int, int]:
        """The experiment each of `run_ids` is of, whether or not it is deleted; a run that does not exist is left out."""
        runs = self._tables[models.Run]
        return {
            run_id: experiment_id
            for batch in _in_batches(run_ids)
            for run_id, experiment_id in self._execute(
                sa.select(runs.c.id, runs.c.experiment_id).where(runs.c.id.in_(batch))
            )
        }

    def _named_pins(self, statement: sa.Select[Any]) -> list[models.NamedPin]:
        """The pins `statement` selects (all of the table's columns, then `username`), each with who pinned it."""
        with self._begin() as conn:
            return [
                models.NamedPin(pin=sql.construct(models.RunPin, row), username=row["username"])
                for row in conn.execute(statement).mappings()
            ]

    def pins_of(
        self, experiment_id: int, *, run_ids: frozenset[int] | None
    ) -> dict[int, list[models.NamedPin]]:
        """The pins on an experiment's (non-deleted) runs, by run, oldest first: only `run_ids`' if given."""
        pins, runs, users = (self._tables[m] for m in (models.RunPin, models.Run, models.User))
        found = self._named_pins(
            sa.select(pins, users.c.username)
            .join(runs, runs.c.id == pins.c.run_id)
            .join(users, users.c.id == pins.c.pinned_by)
            .where(
                pins.c.experiment_id == experiment_id,
                runs.c.deleted_at.is_(None),
                *_runs_among(pins.c.run_id, run_ids),
            )
            .order_by(pins.c.pinned_at, pins.c.id)
        )
        by_run: dict[int, list[models.NamedPin]] = {}
        for named in found:
            by_run.setdefault(named.pin.run_id, []).append(named)
        return by_run

    def _pins_of_project(
        self,
        project_id: int,
        colors: frozenset[models.PinColor] | None,
        pinned_by: frozenset[int] | None,
        experiment_id: int | None,
        name_contains: str,
    ) -> tuple[sa.Join, list[sa.ColumnElement[bool]]]:
        """
        What the reads of a project's pins select from and keep.

        The project's (not deleted) experiments are read first, which are few, and the pins are asked for `IN` those ids,
        each found by `idx_RunPin_experiment`: never through the project's runs, whose number nothing bounds. The ids go
        into the statement as values and not as a join to `Experiment`, because a planner cannot know which experiments
        a join selects: it takes the average number of pins an experiment has, and a project whose experiments have few
        beside others that have many is then read by hashing every pin there is.
        """
        pins, runs, experiments = (self._tables[m] for m in (models.RunPin, models.Run, models.Experiment))
        in_project = [
            experiment
            for (experiment,) in self._execute(
                sa.select(experiments.c.id).where(
                    experiments.c.project_id == project_id, experiments.c.deleted_at.is_(None)
                )
            )
        ]
        return pins.join(runs, runs.c.id == pins.c.run_id), [
            pins.c.experiment_id.in_(in_project),
            runs.c.deleted_at.is_(None),
            *([pins.c.color.in_(colors)] if colors is not None else []),
            *([pins.c.pinned_by.in_(pinned_by)] if pinned_by is not None else []),
            *([pins.c.experiment_id == experiment_id] if experiment_id is not None else []),
            *_name_matches(runs, name_contains),
        ]

    def _pinned_run_page(  # noqa: PLR0913 -- the filters of the pinned-runs list, and its page
        self,
        project_id: int,
        colors: frozenset[models.PinColor] | None,
        pinned_by: frozenset[int] | None,
        experiment_id: int | None,
        name_contains: str,
        limit: int,
        offset: int,
    ) -> tuple[int, list[int]]:
        """How many runs match the filters, and the ids of a page of them: each once, the one pinned last first."""
        pins = self._tables[models.RunPin]
        selected, conditions = self._pins_of_project(
            project_id, colors, pinned_by, experiment_id, name_contains
        )
        ((total,),) = self._execute(
            sa.select(sa.func.count(sa.distinct(pins.c.run_id))).select_from(selected).where(*conditions)
        )
        newest = sa.func.max(pins.c.pinned_at)
        rows = self._execute(
            sa.select(pins.c.run_id)
            .select_from(selected)
            .where(*conditions)
            .group_by(pins.c.run_id)
            .order_by(newest.desc(), pins.c.run_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return total, [run_id for (run_id,) in rows]

    def list_pinned_runs(  # noqa: PLR0913 -- one keyword per filter of the pinned-runs list
        self,
        project_id: int,
        *,
        colors: frozenset[models.PinColor] | None = None,
        pinned_by: frozenset[int] | None = None,
        experiment_id: int | None = None,
        name_contains: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> models.PinnedRuns:
        """A page of a project's pinned (non-deleted) runs, each once with every pin on it, the one pinned last first."""
        runs, experiments, pins, users = (
            self._tables[m] for m in (models.Run, models.Experiment, models.RunPin, models.User)
        )
        total, run_ids = self._pinned_run_page(
            project_id, colors, pinned_by, experiment_id, name_contains, limit, offset
        )
        run_rows = {
            run.id: run for run in self._fetch(models.Run, sa.select(runs).where(runs.c.id.in_(run_ids)))
        }
        experiment_rows = {
            experiment.id: experiment
            for experiment in self._fetch(
                models.Experiment,
                sa.select(experiments).where(
                    experiments.c.id.in_({run.experiment_id for run in run_rows.values()})
                ),
            )
        }
        pins_by_run: dict[int, list[models.NamedPin]] = {}
        for named in self._named_pins(
            sa.select(pins, users.c.username)
            .join(users, users.c.id == pins.c.pinned_by)
            .where(pins.c.run_id.in_(run_ids))
            .order_by(pins.c.pinned_at, pins.c.id)
        ):
            pins_by_run.setdefault(named.pin.run_id, []).append(named)
        return models.PinnedRuns(
            total=total,
            runs=[
                models.PinnedRun(
                    run=run_rows[run_id],
                    experiment=experiment_rows[run_rows[run_id].experiment_id],
                    pins=pins_by_run[run_id],
                )
                for run_id in run_ids
            ],
        )

    def pin_counts(self, project_id: int) -> dict[models.PinColor, int]:
        """How many of a project's (non-deleted) runs are pinned in each colour by anyone."""
        pins = self._tables[models.RunPin]
        selected, conditions = self._pins_of_project(project_id, None, None, None, "")
        rows = self._execute(
            sa.select(pins.c.color, sa.func.count(sa.distinct(pins.c.run_id)))
            .select_from(selected)
            .where(*conditions)
            .group_by(pins.c.color)
        )
        return {models.PinColor(color): count for color, count in rows}

    def pinned_run_ids(  # noqa: PLR0913 -- the filters of `list_pinned_runs`, and how many runs
        self,
        project_id: int,
        *,
        colors: frozenset[models.PinColor] | None = None,
        pinned_by: frozenset[int] | None = None,
        experiment_id: int | None = None,
        name_contains: str = "",
        limit: int,
    ) -> list[int]:
        """The runs `list_pinned_runs` would list for these filters, the one pinned last first; at most `limit`."""
        return self._pinned_run_page(project_id, colors, pinned_by, experiment_id, name_contains, limit, 0)[1]

    def list_pinners(self, project_id: int) -> list[models.Pinner]:
        """Who has pinned a run of a project (not a deleted one), by username."""
        pins, users = self._tables[models.RunPin], self._tables[models.User]
        selected, conditions = self._pins_of_project(project_id, None, None, None, "")
        rows = self._execute(
            sa.select(users.c.id, users.c.username)
            .select_from(selected.join(users, users.c.id == pins.c.pinned_by))
            .where(*conditions)
            .distinct()
            .order_by(users.c.username, users.c.id)
        )
        return [models.Pinner(user_id=user_id, username=username) for user_id, username in rows]

    def list_users(self) -> list[models.User]:
        """Every user, by username -- e.g. who a note can mention."""
        users = self._tables[models.User]
        return self._fetch(models.User, sa.select(users).order_by(users.c.username))

    def update_page[D, C](self, page: models.Page[D, C]) -> models.Page[D, C]:
        pages = self._tables[models.Page]
        stored = self._execute(
            sa.select(*(pages.c[field] for field in PAGE_SCOPE_FIELDS)).where(pages.c.id == page.id)
        )
        scope = page.model_dump(include=set(PAGE_SCOPE_FIELDS))
        if stored and tuple(stored[0]) != tuple(scope[field] for field in PAGE_SCOPE_FIELDS):
            msg = "A page's scope cannot change"
            raise ValueError(msg)
        (updated,) = self._fetch(type(page), sql.update(pages, page))
        return updated

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        artifacts = list(artifacts)
        self._ensure_runs_not_deleted({a.run_id for a in artifacts})
        self._insert_rows(models.Artifact, artifacts)
        self._touch_experiments({a.experiment_id for a in artifacts})

    def count_artifacts_by_ref(self, ref: str) -> int:
        a = self._tables[models.Artifact]
        ((count,),) = self._execute(sa.select(sa.func.count()).select_from(a).where(a.c.ref == ref))
        return count

    def get_artifact(self, artifact_id: int) -> models.Artifact | None:
        """Get one (non-deleted) artifact by id, or `None` if it doesn't exist (or has been deleted)."""
        return next(iter(self._by_id(models.Artifact, artifact_id)), None)

    def fetch_artifacts(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        key_prefixes: frozenset[str] = frozenset(),
        run_ids: frozenset[int] | None,
        steps: models.StepRange | None = None,
    ) -> Iterator[models.Artifact]:
        """
        An experiment's (non-deleted) artifact metadata, not bytes.

        Only the artifacts under `keys` and under any key starting with one of `key_prefixes`, if either
        is given -- `keys=None` with no prefixes means every artifact. Only those logged at `steps`, if given.
        """
        a = self._tables[models.Artifact]
        step_filter = (
            list[sa.ColumnElement[bool]]() if steps is None else [a.c.step.between(steps.first, steps.last)]
        )
        key_filter: list[sa.ColumnElement[bool]] = (
            [
                sa.or_(
                    a.c.key.in_(keys or ()),
                    *(a.c.key.startswith(prefix, autoescape=True) for prefix in key_prefixes),
                )
            ]
            if keys is not None or key_prefixes
            else []
        )
        return iter(
            self._fetch(
                models.Artifact,
                sa.select(a)
                .where(
                    a.c.experiment_id == experiment_id,
                    a.c.deleted_at.is_(None),
                    *_runs_among(a.c.run_id, run_ids),
                    *key_filter,
                    *step_filter,
                )
                .order_by(a.c.run_id, a.c.step),
                no_validate=True,
            )
        )

    def artifact_steps(
        self, experiment_id: int, *, key: str, run_ids: frozenset[int] | None
    ) -> dict[int, list[int]]:
        """The steps each run logged a (non-deleted) artifact under `key` at, in order, by run: none of their metadata."""
        a = self._tables[models.Artifact]
        rows = self._execute(
            sa.select(a.c.run_id, a.c.step)
            .distinct()
            .where(
                a.c.experiment_id == experiment_id,
                a.c.key == key,
                a.c.deleted_at.is_(None),
                *_runs_among(a.c.run_id, run_ids),
            )
            .order_by(a.c.run_id, a.c.step)
        )
        steps: dict[int, list[int]] = {}
        for run_id, step in rows:
            steps.setdefault(run_id, []).append(step)
        return steps

    def summarize_artifact_keys(self, experiment_id: int) -> list[models.Artifact]:
        """An experiment's latest (non-deleted) artifact under each key, sorted by key -- not every artifact logged under it."""
        a = self._tables[models.Artifact]
        keys = _distinct_keys(a, experiment_id)
        latest = (
            sa.select(a.c.id)
            .where(a.c.experiment_id == experiment_id, a.c.key == keys.c.key, a.c.deleted_at.is_(None))
            .order_by(a.c.run_id.desc(), a.c.step.desc(), a.c.id.desc())
            .limit(1)
            .correlate(keys)
            .scalar_subquery()
        )
        # Two statements, not a join of the rows to their ids: Postgres plans that join as a scan of the whole table.
        ids = [
            id_
            for (id_,) in self._execute(sa.select(latest).where(keys.c.key.is_not(None)))
            if id_ is not None
        ]
        return self._fetch(
            models.Artifact, sa.select(a).where(a.c.id.in_(ids)).order_by(a.c.key), no_validate=True
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
        # A table several paths lead to (a comparison's runs are owned by its page *and* by their run) is counted once,
        # over the rows any of them selects.
        paths: dict[sa.Table, list[sa.ColumnElement[bool]]] = {}
        for table, where in cascade:
            paths.setdefault(table, []).append(where)
        details: dict[str, int] = {}
        for table, wheres in paths.items():
            ((count,),) = self._execute(
                sa.select(sa.func.count()).select_from(table).where(sa.or_(*wheres), extra(table))
            )
            details[table.name] = details.get(table.name, 0) + cast("int", count)
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
        `AuthorizingDataStore` (`_authorization.py`), which every request reaches any `DataStore`
        (this one included) through.
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
        this module's job -- see `AuthorizingDataStore`.
        """
        deleted_at = self._fetch_deleted_at(model, entity_id)
        if deleted_at is None:
            msg = f"{model.__name__} {entity_id} is not deleted"
            raise ValueError(msg)

        table = self._tables[model]
        cascade = self._cascade(model, entity_id, within=SOFT_DELETABLE)
        details = self._cascade_counts(cascade, lambda t: t.c.deleted_at == deleted_at)
        cleared: dict[str, None] = {"deleted_at": None, "deleted_by": None}
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
        Scope enforcement isn't this module's job -- see `AuthorizingDataStore`.

        Purging what is purged already (a second click, another process) does nothing (#429): an entity that is gone is
        what a purge was asked for, and only the purge that deletes its row records it and queues its blobs.
        """
        table = self._tables[model]
        rows = self._execute(sa.select(table.c.deleted_at).where(table.c.id == entity_id))
        if not rows:
            return
        if rows[0][0] is None:
            msg = f"{model.__name__} {entity_id} must be soft-deleted before it can be purged"
            raise ValueError(msg)

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
        with self._begin() as conn:
            if (
                conn.execute(sa.delete(table).where(table.c.id == entity_id).returning(table.c.id)).first()
                is None
            ):
                return  # another purge got there first, and recorded it
            conn.execute(
                self._audit_log_insert(actor.id, models.AuditAction.PURGE, entity_type, entity_id, details)
            )
            for artifact_id, ref in artifact_refs:
                conn.execute(
                    sql.insert(
                        purge_tasks,
                        models.NewArtifactPurgeTask(artifact_id=artifact_id, ref=ref, requested_by=actor.id),
                    )
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

    def delete_runs(self, run_ids: frozenset[int], actor: models.User) -> None:
        """
        Soft-delete each of `run_ids` as `delete_run` does.

        Every id is checked first, so one that does not exist or is already deleted refuses the whole call before any run is
        touched. The deletes themselves are one transaction per run, each with its own audit entry, so a failure part-way
        leaves the runs before it deleted.

        Requires `Scope.RUN_DELETE`.
        """
        runs = self._tables[models.Run]
        live = {
            run_id
            for batch in _in_batches(run_ids)
            for (run_id,) in self._execute(
                sa.select(runs.c.id).where(runs.c.id.in_(batch), runs.c.deleted_at.is_(None))
            )
        }
        if gone := run_ids - live:
            msg = f"Run {min(gone)} does not exist or is already deleted"
            raise ValueError(msg)
        # One by one, each in its own transaction: every run is audited, restored and purged on its own.
        for run_id in sorted(run_ids):
            self.delete_run(run_id, actor)

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
        actor: models.User,  # noqa: ARG002 -- part of the `DataStore` contract; enforced by `AuthorizingDataStore`
        limit: int = 100,
        offset: int = 0,
    ) -> Iterator[models.AuditLogEntry]:
        """List audit log entries, most recent first, for a trash/admin view. Requires `Scope.AUDIT_LOG_READ`."""
        log = self._tables[models.AuditLogEntry]
        yield from self._fetch(
            models.AuditLogEntry,
            # Entries made in one clock tick share a timestamp: the later id is the later entry.
            sa.select(log).order_by(log.c.timestamp_utc.desc(), log.c.id.desc()).limit(limit).offset(offset),
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
        _PURGES.add(1, OutcomeAttributes(outcome=Outcome.SUCCESS))

    def fail_artifact_purge(self, task_id: int, error: str) -> None:
        """Record that a queued blob deletion failed. The task stays pending and is retried later."""
        tasks = self._tables[models.ArtifactPurgeTask]
        self._execute(sa.update(tasks).where(tasks.c.id == task_id).values(last_error=error))
        _PURGES.add(1, OutcomeAttributes(outcome=Outcome.FAILURE))

    def artifact_purge_progress_at(self) -> datetime | None:
        """When a running purge last made progress (on `AppState`'s one row); `None` while none is running."""
        state = self._tables[AppState]
        ((progress_at,),) = self._execute(
            sa.select(state.c.artifact_purge_progress_at).where(state.c.id == APP_STATE_ROW_ID)
        )
        return progress_at

    def record_artifact_purge_progress(self, *, running: bool) -> None:
        """Stamp that a purge is making progress now (`running`), or clear it once the purge is done."""
        state = self._tables[AppState]
        self._execute(
            sa.update(state)
            .where(state.c.id == APP_STATE_ROW_ID)
            .values(artifact_purge_progress_at=pendulum.now(pendulum.UTC) if running else None)
        )
