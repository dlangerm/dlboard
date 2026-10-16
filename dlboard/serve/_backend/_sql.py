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
from datetime import datetime
from types import NoneType, UnionType

import numpy as np
import pendulum
import sqlalchemy as sa
from pydantic import AwareDatetime, BaseModel, TypeAdapter
from sqlalchemy.dialects.postgresql import JSONB

from dlboard import models
from dlboard.serve._backend._foreign_keys import ForeignKeyKind

if typing.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


_DATETIME_PARSER = TypeAdapter(datetime)


def _aware(value: datetime | str) -> datetime:
    """
    `value` as an aware datetime, a naive one taken to be UTC.

    Runs for every timestamp a query reads (a metrics fetch reads one per row), so it stays a plain
    pydantic parse plus a `tzinfo`, not a pendulum `DateTime` built per value -- that is 10-30x slower.
    """
    parsed = _DATETIME_PARSER.validate_python(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=pendulum.UTC)


class DecodedJSON(sa.TypeDecorator[typing.Any]):
    """
    A JSON column that reads back as the model field's own type, not as bare JSON.

    A row is built without validating it (the database only holds what a validated model wrote), so the
    column does the decoding that gives a value inside the JSON its type back: a `Quantity` tag
    comes out as one, not as the `{"value": ..., "unit": ...}` dict it is stored as.
    """

    impl = _JSON
    cache_ok = True

    def __init__(self, annotation: object) -> None:
        super().__init__()
        self.annotation = annotation
        self._adapter: TypeAdapter[typing.Any] = TypeAdapter(annotation)

    @typing.override
    def process_result_value(self, value: object, dialect: Dialect) -> object:
        return None if value is None else self._adapter.validate_python(value)


class PackedFloatArray(sa.TypeDecorator[list[float]]):
    """
    A list of floats stored as the packed little-endian 64-bit array it is: 8 bytes each, read back bit for bit.

    The same bytes on every database. JSON text spells each float out in about 22 characters, is parsed again
    on every read, and (on sqlite, which does not compress) is what makes a row of a few hundred of them cost
    several times its worth.
    """

    impl = sa.LargeBinary
    cache_ok = True

    @typing.override
    def process_bind_param(self, value: Sequence[float] | None, dialect: Dialect) -> bytes | None:
        return None if value is None else np.asarray(value, dtype="<f8").tobytes()

    @typing.override
    def process_result_value(self, value: bytes | None, dialect: Dialect) -> list[float] | None:
        return None if value is None else np.frombuffer(value, dtype="<f8").tolist()


class PackedCountArray(sa.TypeDecorator[list[int]]):
    """
    A list of non-negative integers stored as a packed little-endian array of the narrowest unsigned width that fits.

    One byte says how wide (1, 2, 4 or 8), so a histogram of a few thousand values costs a byte or two per bucket,
    and one of a tensor with billions of values in a bucket still fits: there is no ceiling to validate against.
    """

    impl = sa.LargeBinary
    cache_ok = True

    @typing.override
    def process_bind_param(self, value: Sequence[int] | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        counts = np.asarray(value, dtype=np.uint64)
        width = np.min_scalar_type(int(counts.max(initial=0))).itemsize
        return bytes([width]) + counts.astype(f"<u{width}").tobytes()

    @typing.override
    def process_result_value(self, value: bytes | None, dialect: Dialect) -> list[int] | None:
        if value is None:
            return None
        packed = memoryview(value)
        return np.frombuffer(packed[1:], dtype=f"<u{packed[0]}").tolist()


_PACKED_COLUMNS: typing.Final[Mapping[type[object], type[sa.types.TypeEngine[typing.Any]]]] = {
    models.PackedFloats: PackedFloatArray,
    models.PackedCounts: PackedCountArray,
}
"""The column a field marked with each kind of `Annotated` marker is stored in, instead of the one its type implies."""


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    """`(inner type, nullable)` for `X | None`; `(annotation, False)` for anything else."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) in (UnionType, typing.Union) and NoneType in args:
        (inner,) = (a for a in args if a is not NoneType)
        return inner, True
    return annotation, False


def _marked_type(metadata: Sequence[object]) -> sa.types.TypeEngine[typing.Any] | None:
    """The column the first `_PACKED_COLUMNS` marker among `metadata` asks for, if there is one."""
    return next((_PACKED_COLUMNS[type(m)]() for m in metadata if type(m) in _PACKED_COLUMNS), None)


def column_type(  # noqa: PLR0911 -- one return per SQL type is the clearest shape here
    annotation: object, metadata: Sequence[object] = ()
) -> sa.types.TypeEngine[typing.Any]:
    """The SQL column type a model field (its already un-`Optional`ed annotation, and any `Annotated` markers) is stored as."""
    if (marked := _marked_type(metadata)) is not None:
        return marked
    if typing.get_origin(annotation) in (dict, list):
        return DecodedJSON(annotation)
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
            column_type(annotation, fields[name].metadata),
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
    values = row_values(model)
    if values.get(ID_KEY) is None:
        values.pop(ID_KEY, None)
    return sa.insert(table).values(values).returning(table)


def update(table: sa.Table, model: BaseModel) -> sa.Update:
    """Update the row `model` was read from. Never writes a `_NEVER_UPDATE_FIELDS` column -- see its docstring."""
    values = row_values(model, exclude=_NEVER_UPDATE_FIELDS)
    row_id: int = values.pop(ID_KEY)
    return sa.update(table).where(table.c[ID_KEY] == row_id).values(values).returning(table)
