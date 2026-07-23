"""Common functions for data stores."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from pydantic import BaseModel


ID_KEY: typing.Final = "id"


def construct[T: BaseModel](obj_type: type[T], args: tuple[typing.Any, ...]) -> T:
    fields = list(obj_type.model_fields.keys())
    return obj_type.model_validate({f: args[i] for i, f in enumerate(fields)})


def annotation_to_sqltype(type: type | None) -> str:
    assert type is not None

    match type:
        case int():
            return "INTEGER"

    raise NotImplementedError(type)


def escape_value_sql(value: typing.Any) -> str:  # noqa: ANN401
    match value:
        case int() | float():
            return f"{value}"
        case str():
            return f"'{value}'"
        case bool():
            return "true" if value else "false"
    raise NotImplementedError(type(value))


def create_table_sql(model: type[BaseModel]) -> str:
    if ID_KEY not in model.model_fields:
        msg = f"Creation object {model.__class__} must contain an ID key"
        raise AssertionError(msg)
    base_str = f"""
    CREATE TABLE IF NOT EXISTS {model.__name__}
    """
    sorted_keys = list(model.model_fields.keys())
    id_index = sorted_keys.index(ID_KEY)
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
