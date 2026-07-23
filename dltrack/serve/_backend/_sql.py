"""Common functions for data stores."""

from __future__ import annotations

import typing
from types import NoneType, UnionType

from structlog.stdlib import get_logger

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic import BaseModel

ID_KEY: typing.Final = "id"
_log = get_logger(__name__)


def construct[T: BaseModel](obj_type: type[T], args: tuple[typing.Any, ...]) -> T:
    fields = list(obj_type.model_fields.keys())
    return obj_type.model_validate({f: args[i] for i, f in enumerate(fields)})


def escape_value_sql(value: object) -> str:
    _log.debug("Escaping value %s type %s", value, type(value))
    match value:
        case int() | float():
            return f"{value}"
        case str():
            return f"'{value.strip("'")}'"
        case bool():
            return "true" if value else "false"
        case _:
            raise NotImplementedError(type(value))


def annotation_to_sqltype(annotation: type, *, nullable: bool = False) -> str:
    org = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if org is UnionType and len(args) > 0 and args[1] is NoneType:
        # optional
        return annotation_to_sqltype(args[0])
    suffix = "" if nullable else " NOT NULL"
    match annotation():
        case str():
            return f"TEXT{suffix}"
        case int():
            return f"INTEGER{suffix}"
        case float():
            return f"REAL{suffix}"
        case _:
            raise NotImplementedError((annotation, type(annotation)))


def create_table_sql(model: type[BaseModel]) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Creation object {model.__class__} must contain an ID key"
        raise AssertionError(msg)
    base_str = f"""
    CREATE TABLE IF NOT EXISTS {model.__name__}
    """
    typed_keys = [annotation_to_sqltype(field.annotation) for field in model.model_fields.values()]  # pyright: ignore[reportArgumentType]
    sorted_keys = list(model.model_fields.keys())
    id_index = sorted_keys.index(ID_KEY)

    for idx in range(len((sorted_keys))):
        sorted_keys[idx] = f"{sorted_keys[idx]} {typed_keys[idx]}"

    sorted_keys[id_index] = f"{ID_KEY} INTEGER PRIMARY KEY AUTOINCREMENT"
    base_str += "("
    base_str += ",".join(sorted_keys)
    base_str += ");"
    return base_str


def insert(table: type[BaseModel], model: BaseModel) -> str:
    sorted_keys = list(model.__class__.model_fields.keys())
    if ID_KEY in sorted_keys:
        msg = f"Creation object {model.__class__} must not contain an ID key"
        raise AssertionError(msg)
    joined_keys = ",".join(sorted_keys)
    raw_values = ",".join([escape_value_sql(getattr(model, k)) for k in sorted_keys])
    return f"""
    INSERT INTO {table.__name__}
    ({joined_keys})
    VALUES({raw_values})
    RETURNING *;
    """


def insert_many(
    table: type[BaseModel],
    models: Iterable[BaseModel],
) -> tuple[str, Iterable[tuple[str, ...]]]:
    sorted_keys = list(table.model_fields.keys())
    sorted_keys.remove(ID_KEY)
    joined_keys = ",".join(sorted_keys)
    return (
        f"""
        INSERT INTO {table.__name__}
        ({joined_keys})
        VALUES({",".join(["?"] * len(sorted_keys))});
        """,
        (tuple(escape_value_sql(getattr(model, k)) for k in sorted_keys) for model in models),
    )


def get_by_id(model: type[BaseModel], id: int) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Get object {model.__name__} must contain an ID key"
        raise AssertionError(msg)

    return f"""
        SELECT *
        FROM {model.__name__}
        WHERE {ID_KEY} = '{id}';
    """


def get_all(model: type[BaseModel]) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Get object {model.__name__} must contain an ID key"
        raise AssertionError(msg)

    return f"""
        SELECT *
        FROM {model.__name__};
    """


def get_all_by_field(
    model: type[BaseModel],
    field_name: str,
    field_value: str | int | bool,  # noqa: FBT001
    order_by: list[str] | None = None,
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
    order_clause = ("ORDER BY " + ",".join(order_by)) if order_by else ""

    return f"""
        SELECT *
        FROM {model.__name__}
        WHERE {field_name} = {escape_value_sql(field_value)}
        {order_clause};
    """
