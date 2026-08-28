"""Common functions for data stores."""

from __future__ import annotations

import enum
import json
import typing
from datetime import datetime
from types import NoneType, UnionType

from pydantic import AwareDatetime, BaseModel
from structlog.stdlib import get_logger

from dltrack.serve._backend._foreign_keys import ForeignKeyKind

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from dltrack.serve._backend._foreign_keys import ForeignKey


ID_KEY: typing.Final = "id"
_log = get_logger(__name__)


def _unwrap_optional(annotation: type) -> type:
    org = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if org is UnionType and len(args) > 0 and args[1] is NoneType:
        return args[0]
    return annotation


def _decode_stored_value(annotation: type, value: object) -> object:
    """Reverse `serialize_complex_sql`: JSON-decode dict/list columns, parse datetime columns."""
    if value is None or not isinstance(value, str):
        return value

    ann = _unwrap_optional(annotation)
    org = typing.get_origin(ann)

    if org in (dict, list):
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            msg = f"Expected JSON-decodable value for annotation {ann!r}, got {value!r}"
            raise ValueError(msg) from exc

    if ann in (datetime, AwareDatetime):
        return datetime.fromisoformat(value)

    return value


def construct[T: BaseModel](
    obj_type: type[T], args: tuple[typing.Any, ...], *, no_validate: bool = True
) -> T:
    fields = list(obj_type.model_fields.keys())
    decoded = {
        f: _decode_stored_value(obj_type.model_fields[f].annotation, args[i])  # pyright: ignore[reportArgumentType]
        for i, f in enumerate(fields)
    }
    if no_validate:
        return obj_type.model_construct(**decoded)  # pyright: ignore[reportArgumentType]
    _log.debug("validating %s with args %s", obj_type, args)
    return obj_type(**decoded)


def ensure_basemodel(arg: typing.Any) -> typing.TypeGuard[list[BaseModel]]:  # noqa: ANN401
    return isinstance(arg, list) and all(isinstance(a, BaseModel) for a in arg)  # pyright: ignore[reportUnknownVariableType]


def ensure_basemodel_dict(arg: typing.Any) -> typing.TypeGuard[dict[str, BaseModel]]:  # noqa: ANN401
    return isinstance(arg, dict) and all(
        isinstance(k, str) and isinstance(v, BaseModel)
        for k, v in arg.items()  # pyright: ignore[reportUnknownVariableType]
    )


def serialize_complex_sql(value: object) -> object:
    """Serialize lists and dictionaries if they appear."""
    match value:
        case dict():
            if ensure_basemodel_dict(value):
                return f"{json.dumps({k: v.model_dump(mode='json') for k, v in value.items()})}"
            return f"{json.dumps(value)}"
        case list():
            if ensure_basemodel(value):
                return f"{json.dumps([m.model_dump(mode='json') for m in value])}"
            return f"{json.dumps(value)}"
        case _:
            return value


def serialize_base_model(value: BaseModel) -> dict[str, typing.Any]:
    """Serialize a base model."""
    return {k: serialize_complex_sql(v) for k, v in value.model_dump(mode="json").items()}


def escape_value_sql(value: object) -> str:
    _log.debug("Escaping value %s type %s", value, type(value))
    match value:
        case bool():
            return "true" if value else "false"
        case int() | float():
            return f"{value}"
        case str():
            return f"'{value.replace("'", "''")}'"
        case NoneType():
            return "NULL"
        case datetime():
            return value.isoformat()
        case _:
            raise NotImplementedError(type(value))


def create_index_sql(model: type[BaseModel], columns: list[str], *, index_name: str | None = None) -> str:
    for c in columns:
        if c not in model.model_fields:
            msg = f"{c} not present in model"
            raise AssertionError(msg)
    name = index_name or f"idx_{model.__name__}_{'_'.join(columns)}"
    raw = f"""
    CREATE INDEX IF NOT EXISTS {name}
    ON {model.__name__} ({",".join(columns)});
    """
    _log.debug("Create index sql: %s", raw)
    return raw


def annotation_to_sqltype(annotation: type, *, nullable: bool = False) -> str:  # noqa: PLR0911 -- one return per SQL type is the clearest shape here
    org = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if org is UnionType and len(args) > 0 and args[1] is NoneType:
        # optional
        return annotation_to_sqltype(args[0], nullable=True)
    suffix = "" if nullable else " NOT NULL"
    if issubclass(annotation, enum.Enum):
        # Calling a bare enum class with no args (as the generic `annotation()` probe below does)
        # raises -- e.g. `Scope()` -- rather than constructing a sentinel instance like `str()`
        # does, so enums need to be special-cased instead of falling through to `match`. Every enum
        # in this codebase is a `StrEnum`, so TEXT is always correct; a plain `IntEnum` would need
        # its own branch if one is ever introduced.
        return f"TEXT{suffix}"
    match annotation():
        case str():
            return f"TEXT{suffix}"
        case int():
            return f"INTEGER{suffix}"
        case float():
            return f"REAL{suffix}"
        case dict() | list() | datetime() | AwareDatetime():  # pyright: ignore[reportGeneralTypeIssues]
            return f"TEXT{suffix}"
        case bytes():
            return f"BLOB{suffix}"
        case _:
            raise NotImplementedError((annotation, type(annotation)))  # pyright: ignore[reportUnknownArgumentType]


def create_table_sql(
    model: type[BaseModel],
    foreign_keys: dict[str, ForeignKey] | None = None,
    unique_columns: list[str] | None = None,
) -> str:
    """
    Build a `CREATE TABLE IF NOT EXISTS` statement reflecting `model`'s fields.

    An `OWNERSHIP`-kind foreign key (see `ForeignKeyKind`) gets a real `ON DELETE CASCADE`, so
    purging a parent row lets SQLite cascade the delete natively instead of the caller having to
    enumerate every dependent table by hand.
    """
    if ID_KEY not in model.model_fields:
        msg = f"Creation object {model.__class__} must contain an ID key"
        raise AssertionError(msg)
    for field_name in foreign_keys or {}:
        if field_name not in model.model_fields:
            msg = f"Foreign key column {field_name} not present in model {model.__name__}"
            raise AssertionError(msg)
    for field_name in unique_columns or []:
        if field_name not in model.model_fields:
            msg = f"Unique column {field_name} not present in model {model.__name__}"
            raise AssertionError(msg)
    base_str = f"""
    CREATE TABLE IF NOT EXISTS {model.__name__}
    """
    typed_keys = [annotation_to_sqltype(field.annotation) for field in model.model_fields.values()]  # pyright: ignore[reportArgumentType]
    sorted_keys = list(model.model_fields.keys())
    id_index = sorted_keys.index(ID_KEY)

    for idx in range(len(sorted_keys)):
        sorted_keys[idx] = f"{sorted_keys[idx]} {typed_keys[idx]}"

    sorted_keys[id_index] = f"{ID_KEY} INTEGER PRIMARY KEY AUTOINCREMENT"
    fk_clauses = [
        f"FOREIGN KEY ({field_name}) REFERENCES {fk.references.__name__}({ID_KEY})"
        + (" ON DELETE CASCADE" if fk.kind is ForeignKeyKind.OWNERSHIP else "")
        for field_name, fk in (foreign_keys or {}).items()
    ]
    unique_clauses = [f"UNIQUE ({field_name})" for field_name in unique_columns or []]
    base_str += "("
    base_str += ",".join([*sorted_keys, *fk_clauses, *unique_clauses])
    base_str += ");"
    _log.debug("Create table sql: %s", base_str)
    return base_str


def update(table: type[BaseModel], model: BaseModel) -> tuple[str, dict[str, typing.Any]]:
    """Update an existing entry."""
    sorted_keys = list(model.__class__.model_fields.keys())
    sorted_keys.remove(ID_KEY)
    interpolate_values = ",".join([f"{k} = :{k}" for k in sorted_keys])
    values = serialize_base_model(model)
    id_match = values.pop(ID_KEY)
    return (
        f"""
        UPDATE {table.__name__}
        set {interpolate_values}
        where id = {id_match}
        RETURNING {select_columns_sql(table)};
        """,
        values,
    )


def insert(table: type[BaseModel], model: BaseModel) -> tuple[str, dict[str, typing.Any]]:
    """
    Build an `INSERT` for `model` into `table`.

    Uses an explicit column list rather than positional `VALUES(...)` -- `model`'s class (typically
    a `New*` type) doesn't have to declare every column `table` has (e.g. `deleted_at`/`deleted_by`
    exist only on the stored `Project`/etc., never on `NewProject`); omitted columns are simply left
    at their SQL-level default (NULL) instead of requiring positional arity to match exactly.
    """
    sorted_keys = list(model.__class__.model_fields.keys())
    if ID_KEY in sorted_keys:
        msg = f"Creation object {model.__class__} must not contain an ID key"
        raise AssertionError(msg)
    columns = [*sorted_keys, ID_KEY]
    raw_values = ",".join([f":{k}" for k in columns])
    values = serialize_base_model(model) | {ID_KEY: None}

    return (
        f"""
        INSERT INTO {table.__name__}
        ({",".join(columns)})
        VALUES({raw_values})
        RETURNING {select_columns_sql(table)};
        """,
        values,
    )


def insert_or_ignore(table: type[BaseModel], model: BaseModel) -> tuple[str, dict[str, typing.Any]]:
    """Like `insert`, but a conflicting row (e.g. a duplicate unique `username`) is silently skipped."""
    statement, values = insert(table, model)
    return statement.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), values


def insert_many(
    table: type[BaseModel],
    models: Iterable[BaseModel],
) -> tuple[str, Iterable[dict[str, typing.Any]]]:
    sorted_keys = list(table.model_fields.keys())
    joined_keys = ",".join(sorted_keys)
    return (
        f"""
        INSERT INTO {table.__name__}
        ({joined_keys})
        VALUES({",".join([f":{k}" for k in sorted_keys])});
        """,
        (serialize_base_model(model) | {ID_KEY: None} for model in models),
    )


def select_columns_sql(model: type[BaseModel]) -> str:
    """
    A `SELECT`-clause column list in `model.model_fields` order, instead of `SELECT *`.

    `construct()` decodes a row positionally by zipping it against `model.model_fields`, but a
    column added to an existing table via `_add_missing_columns`' `ALTER TABLE ... ADD COLUMN`
    always lands physically last regardless of where the field sits in the model -- `SELECT *`
    would then hand `construct()` values in the wrong order for any table that's been backfilled
    this way. Naming columns explicitly, in model order, keeps row-to-field alignment correct
    regardless of physical column order.
    """
    return ",".join(model.model_fields)


def get_by_id(model: type[BaseModel], id: int, *, exclude_deleted: bool = False) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Get object {model.__name__} must contain an ID key"
        raise AssertionError(msg)

    deleted_clause = " AND deleted_at IS NULL" if exclude_deleted else ""
    return f"""
        SELECT {select_columns_sql(model)}
        FROM {model.__name__}
        WHERE {ID_KEY} = '{int(id)}'{deleted_clause};
    """


def get_all(model: type[BaseModel], *, exclude_deleted: bool = False) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Get object {model.__name__} must contain an ID key"
        raise AssertionError(msg)

    where_clause = "WHERE deleted_at IS NULL" if exclude_deleted else ""
    return f"""
        SELECT {select_columns_sql(model)}
        FROM {model.__name__}
        {where_clause};
    """


def get_all_by_field(  # noqa: PLR0913
    model: type[BaseModel],
    field_name: str,
    field_value: str | int | bool,  # noqa: FBT001
    match_field: str | None = None,
    match_field_values: set[str | bool | int | float] | None = None,
    order_by: list[str] | None = None,
    *,
    exclude_deleted: bool = False,
    descending: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Get object {model.__name__} must contain an ID key"
        raise AssertionError(msg)
    if field_name not in model.model_fields:
        msg = f"Get all {model.__name__} must contain key {field_name}"
        raise AssertionError(msg)
    for order in order_by or []:
        if order not in model.model_fields:
            msg = f"{order} not present in model"
            raise AssertionError(msg)
    order_clause = ("ORDER BY " + ",".join(order_by) + (" DESC" if descending else "")) if order_by else ""
    if match_field and not match_field_values:
        msg = "Get all by field match field must have values!"
        raise AssertionError(msg)

    match_clause = (
        "1=1"
        if not match_field or not match_field_values
        else (f"{match_field} in ({','.join(map(escape_value_sql, list(match_field_values)))})")
    )
    deleted_clause = " AND deleted_at IS NULL" if exclude_deleted else ""
    limit_clause = f" LIMIT {int(limit)} OFFSET {int(offset)}" if limit is not None else ""

    return f"""
        SELECT {select_columns_sql(model)}
        FROM {model.__name__}
        WHERE {field_name} = {escape_value_sql(field_value)} AND {match_clause}{deleted_clause}
        {order_clause}{limit_clause};
    """
