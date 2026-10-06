"""
The pydantic-model <-> SQLAlchemy Core mapping every SQL-backed data store is built on.

Tables are generated from the pydantic models themselves, one column per field, so the model stays
the single source of truth for what gets stored. SQLAlchemy then owns everything dialect-specific:
column types, identity columns, identifier quoting, bind-parameter style, `ON CONFLICT`, and
schema introspection. A store only ever supplies an `Engine` (see `SQLStoreBase`).
"""

from __future__ import annotations

import enum
import typing
from datetime import UTC, datetime
from types import NoneType, UnionType

import sqlalchemy as sa
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.dialects.postgresql import JSONB

from dlboard.serve._backend._foreign_keys import ForeignKeyKind

if typing.TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine.interfaces import Dialect

    from dlboard.serve._backend._foreign_keys import ForeignKey


ID_KEY: typing.Final = "id"

# Fields no `update()` may ever write, regardless of what value the caller's in-memory model
# happens to hold -- each is bumped exclusively by its own dedicated mechanism (`revision` by
# `SQLStoreBase._touch_experiment`), so writing it back here would let a stale snapshot (fetched
# before some *other* write bumped it concurrently) silently undo that bump.
_NEVER_UPDATE_FIELDS: typing.Final = frozenset({"revision"})

# sqlite only treats a column as its auto-incrementing rowid alias when it is spelled exactly
# `INTEGER PRIMARY KEY`, so every integer is `INTEGER` there; everywhere else it's 64-bit, since a
# busy metrics table outgrows a 32-bit id.
_INT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
# `AwareDatetime` is only `Annotated[datetime, ...]` to a type checker; at runtime it's its own marker class.
_DATETIMES: tuple[object, ...] = (datetime, AwareDatetime)


class UTCDateTime(sa.TypeDecorator[datetime]):
    """
    A timezone-aware datetime: `TIMESTAMPTZ` where the database has one, ISO-8601 text on sqlite.

    sqlite has no datetime type, and every sqlite database written before this layer existed holds
    `isoformat()` strings -- which also sort chronologically as plain text -- so sqlite keeps
    exactly that. Reads are always aware: a string is parsed, a naive value is taken to be UTC.
    Binds accept either a datetime or an ISO-8601 string (what `model_dump(mode="json")` produces).
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    @typing.override
    def load_dialect_impl(self, dialect: Dialect) -> sa.types.TypeEngine[typing.Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(sa.Text())
        return dialect.type_descriptor(sa.DateTime(timezone=True))

    @typing.override
    def process_bind_param(self, value: datetime | str | None, dialect: Dialect) -> datetime | str | None:
        if value is None:
            return None
        aware = _aware(value)
        return aware.isoformat() if dialect.name == "sqlite" else aware

    @typing.override
    def process_result_value(self, value: datetime | str | None, dialect: Dialect) -> datetime | None:
        return None if value is None else _aware(value)


def _aware(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    """`(inner type, nullable)` for `X | None`; `(annotation, False)` for anything else."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) in (UnionType, typing.Union) and NoneType in args:
        (inner,) = (a for a in args if a is not NoneType)
        return inner, True
    return annotation, False


def column_type(annotation: object) -> sa.types.TypeEngine[typing.Any]:  # noqa: PLR0911 -- one return per SQL type is the clearest shape here
    """The SQL column type a model field's (already un-`Optional`ed) annotation is stored as."""
    if typing.get_origin(annotation) in (dict, list):
        return _JSON
    if not isinstance(annotation, type):
        raise NotImplementedError(annotation)
    kind: type[object] = annotation
    # `bool` before `int` (a subclass of it), and enums before `str` -- every enum here is a `StrEnum`.
    if issubclass(kind, bool):
        return sa.Boolean()
    if issubclass(kind, enum.Enum | str):
        return sa.Text()
    if issubclass(kind, int):
        return _INT
    if issubclass(kind, float):
        return sa.Double()
    if kind in _DATETIMES or issubclass(kind, datetime):
        return UTCDateTime()
    if issubclass(kind, bytes):
        return sa.LargeBinary()
    raise NotImplementedError(kind)


def _server_default(default: object) -> sa.TextClause | sa.ColumnElement[bool] | None:
    """
    A real SQL `DEFAULT` for a plain scalar field default (e.g. `revision: int = 0`).

    So an `INSERT` that omits the column -- every `New*` payload omits every server-managed field --
    gets that value. A `None` default (a genuinely optional field) and dict/list defaults are left
    to the implicit NULL.
    """
    match default:
        case bool():
            return sa.true() if default else sa.false()
        case int() | float():
            return sa.text(repr(default))
        case str():
            return sa.text("'" + default.replace("'", "''") + "'")
        case _:
            return None


def table_for(
    model: type[BaseModel],
    metadata: sa.MetaData,
    references: Mapping[type[BaseModel], sa.Table],
    foreign_keys: Mapping[str, ForeignKey] | None = None,
    unique_columns: list[tuple[str, ...]] | None = None,
) -> sa.Table:
    """
    Declare the table storing `model` on `metadata`: one column per field, named after the model.

    `references` resolves each foreign key's target model to its (already declared) table. An
    `OWNERSHIP`-kind foreign key (see `ForeignKeyKind`) gets a real `ON DELETE CASCADE`, so purging a
    parent row lets the database cascade the delete natively.

    Every other (`ATTRIBUTION`) foreign key is checked at commit rather than per statement
    (`DEFERRABLE INITIALLY DEFERRED`). Some of them point at a row the same purge is removing by
    cascade -- `Artifact.experiment_id` next to its ownership edge through `run_id` -- and
    Postgres, unlike sqlite, may check that reference before the cascade reaching the artifact has
    run. By commit, the cascade has finished and the reference is gone.
    """
    fields = model.model_fields
    for name in [ID_KEY, *(foreign_keys or {}), *(c for cols in unique_columns or [] for c in cols)]:
        if name not in fields:
            msg = f"Column {name} not present in model {model.__name__}"
            raise AssertionError(msg)

    def column(name: str) -> sa.Column[typing.Any]:
        if name == ID_KEY:
            return sa.Column(ID_KEY, _INT, sa.Identity(), primary_key=True)
        annotation, nullable = _unwrap_optional(fields[name].annotation)
        fk = (foreign_keys or {}).get(name)
        fk_args: list[sa.ForeignKey] = []
        if fk is not None:
            target = references[fk.references].c[ID_KEY]
            match fk.kind:
                case ForeignKeyKind.OWNERSHIP:
                    fk_args.append(sa.ForeignKey(target, ondelete="CASCADE"))
                case ForeignKeyKind.ATTRIBUTION:
                    fk_args.append(sa.ForeignKey(target, deferrable=True, initially="DEFERRED"))
        return sa.Column(
            name,
            column_type(annotation),
            *fk_args,
            nullable=nullable,
            server_default=_server_default(fields[name].default),
        )

    return sa.Table(
        model.__name__,
        metadata,
        *(column(name) for name in fields),
        *(sa.UniqueConstraint(*cols) for cols in unique_columns or []),
        sqlite_autoincrement=True,
    )


def construct[T: BaseModel](
    model: type[T], row: Mapping[str, typing.Any] | sa.RowMapping, *, no_validate: bool = False
) -> T:
    """Build `model` from a result row, by column name; the column types already decoded every value."""
    values = {name: row[name] for name in model.model_fields if name in row}
    return model.model_construct(**values) if no_validate else model(**values)


def row_values(model: BaseModel, *, exclude: frozenset[str] = frozenset[str]()) -> dict[str, typing.Any]:
    """`model`'s fields as bind values for its table -- JSON-safe, which is what the JSON columns need."""
    return model.model_dump(mode="json", exclude=set(exclude))


def insert(table: sa.Table, model: BaseModel) -> sa.Insert:
    """
    `INSERT` `model` into `table`, returning the stored row.

    Only `model`'s own fields are written: a `New*` model doesn't declare server-managed columns
    (`id`, `deleted_at`, ...), which are left to the database's identity/default/NULL instead.
    """
    exclude = frozenset[str]({ID_KEY}) if getattr(model, ID_KEY, None) is None else frozenset[str]()
    return sa.insert(table).values(row_values(model, exclude=exclude)).returning(table)


def update(table: sa.Table, model: BaseModel) -> sa.Update:
    """Update the row `model` was read from. Never writes a `_NEVER_UPDATE_FIELDS` column -- see its docstring."""
    return (
        sa.update(table)
        .where(table.c[ID_KEY] == getattr(model, ID_KEY))
        .values(row_values(model, exclude=frozenset({ID_KEY, *_NEVER_UPDATE_FIELDS})))
        .returning(table)
    )
