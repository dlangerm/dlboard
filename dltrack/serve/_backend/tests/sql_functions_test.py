# pyright: reportPrivateUsage=false
"""Tests for the pure sql-generation/escaping/decoding helpers in `_sql.py`.

These are the functions behind the "loud failures over silent coercion" bug
fixes (bad JSON in a stored column must raise, not silently pass through).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import AwareDatetime, BaseModel

from dltrack.serve._backend import _sql
from dltrack.serve._backend._foreign_keys import ForeignKey, ForeignKeyKind

if TYPE_CHECKING:
    from collections.abc import Callable


class _NoId(BaseModel, frozen=True, extra="forbid"):
    name: str


class _WithId(BaseModel, frozen=True, extra="forbid"):
    id: int
    name: str
    tags: dict[str, str] = {}
    created: AwareDatetime | None = None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "1"),
        (1.5, "1.5"),
        ("hi", "'hi'"),
        (True, "true"),
        (False, "false"),
        (None, "NULL"),
        (datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00+00:00"),
    ],
)
def test_escape_value_sql(value: object, expected: str) -> None:
    assert _sql.escape_value_sql(value) == expected


def test_escape_value_sql_doubles_embedded_single_quotes() -> None:
    assert _sql.escape_value_sql("O'Brien") == "'O''Brien'"


def test_escape_value_sql_unsupported_type_raises() -> None:
    with pytest.raises(NotImplementedError):
        _sql.escape_value_sql([1, 2])


@pytest.mark.parametrize(
    ("annotation", "nullable", "expected"),
    [
        (str, False, "TEXT NOT NULL"),
        (int, False, "INTEGER NOT NULL"),
        (float, False, "REAL NOT NULL"),
        (bytes, False, "BLOB NOT NULL"),
        (dict, False, "TEXT NOT NULL"),
        (int | None, False, "INTEGER"),
    ],
)
def test_annotation_to_sqltype(annotation: type, nullable: bool, expected: str) -> None:
    assert _sql.annotation_to_sqltype(annotation, nullable=nullable) == expected


def test_annotation_to_sqltype_unsupported_raises() -> None:
    with pytest.raises(NotImplementedError):
        _sql.annotation_to_sqltype(complex)


@pytest.mark.parametrize(
    "call_without_id",
    [
        lambda: _sql.create_table_sql(_NoId),
        lambda: _sql.get_by_id(_NoId, 1),
        lambda: _sql.get_all(_NoId),
    ],
)
def test_functions_requiring_id_field_raise_without_one(call_without_id: Callable[[], object]) -> None:
    with pytest.raises(AssertionError, match="must contain an ID key"):
        call_without_id()


def test_create_table_sql_success() -> None:
    statement = _sql.create_table_sql(_WithId)
    assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in statement
    assert "name TEXT NOT NULL" in statement


class _Parent(BaseModel, frozen=True, extra="forbid"):
    id: int
    name: str


class _Child(BaseModel, frozen=True, extra="forbid"):
    id: int
    parent_id: int


def test_create_table_sql_emits_foreign_key_clause() -> None:
    statement = _sql.create_table_sql(_Child, {"parent_id": ForeignKey(_Parent)})
    assert "FOREIGN KEY (parent_id) REFERENCES _Parent(id)" in statement
    assert "ON DELETE CASCADE" not in statement


def test_create_table_sql_emits_on_delete_cascade_for_ownership_foreign_keys() -> None:
    statement = _sql.create_table_sql(_Child, {"parent_id": ForeignKey(_Parent, ForeignKeyKind.OWNERSHIP)})
    assert "FOREIGN KEY (parent_id) REFERENCES _Parent(id) ON DELETE CASCADE" in statement


def test_create_table_sql_foreign_key_unknown_column_raises() -> None:
    with pytest.raises(AssertionError, match="not present in model"):
        _sql.create_table_sql(_Child, {"nope": ForeignKey(_Parent)})


def test_create_index_sql_unknown_column_raises() -> None:
    with pytest.raises(AssertionError, match="not present in model"):
        _sql.create_index_sql(_WithId, ["nope"])


def test_insert_rejects_model_with_id_field() -> None:
    with pytest.raises(AssertionError, match="must not contain an ID key"):
        _sql.insert(_WithId, _WithId(id=1, name="a"))


def test_get_all_builds_select() -> None:
    assert "SELECT id,name,tags,created" in _sql.get_all(_WithId)


def test_select_columns_sql_lists_fields_in_model_order() -> None:
    assert _sql.select_columns_sql(_WithId) == "id,name,tags,created"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"field_name": "nope"}, "must contain key"),
        ({"field_name": "name", "order_by": ["nope"]}, "not present in model"),
        ({"field_name": "name", "match_field": "name"}, "must have values"),
    ],
)
def test_get_all_by_field_validation_errors(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(AssertionError, match=match):
        _sql.get_all_by_field(_WithId, field_value="x", **kwargs)  # pyright: ignore[reportArgumentType]


def test_get_all_by_field_builds_match_clause() -> None:
    statement = _sql.get_all_by_field(
        _WithId, "name", "run-1", match_field="name", match_field_values={"a", "b"}
    )
    assert "name in (" in statement
    assert "'a'" in statement
    assert "'b'" in statement


def test_get_all_by_field_builds_order_limit_and_offset_clauses() -> None:
    statement = _sql.get_all_by_field(
        _WithId, "name", "run-1", order_by=["name"], descending=True, limit=5, offset=10
    )
    assert "ORDER BY name DESC" in statement
    assert "LIMIT 5 OFFSET 10" in statement


def test_get_all_by_field_omits_limit_clause_when_unset() -> None:
    statement = _sql.get_all_by_field(_WithId, "name", "run-1")
    assert "LIMIT" not in statement


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (dict[str, int], '{"a": 1}', {"a": 1}),
        (list[int], "[1, 2]", [1, 2]),
        (datetime, "2026-01-01T00:00:00+00:00", datetime(2026, 1, 1, tzinfo=UTC)),
        (str, "plain", "plain"),
        (str, None, None),
    ],
)
def test_decode_stored_value(annotation: type, value: object, expected: object) -> None:
    assert _sql._decode_stored_value(annotation, value) == expected


def test_decode_stored_value_bad_json_raises_loudly() -> None:
    with pytest.raises(ValueError, match="Expected JSON-decodable"):
        _sql._decode_stored_value(dict[str, int], "not json")


def test_construct_builds_model_from_row_tuple() -> None:
    row = (1, "run-1", '{"env": "prod"}', None)
    model = _sql.construct(_WithId, row)
    assert model == _WithId(id=1, name="run-1", tags={"env": "prod"}, created=None)
