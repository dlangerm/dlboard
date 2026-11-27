"""
Values written as one comma-separated string: an env var (`10.0.0.5, 172.18.0.0/16`) or a query value (`?compare=3,7`).

Shared by the client and the server, so written for the client's floor (Python 3.10).
"""

from __future__ import annotations

from typing import Annotated, TypeAlias, TypeVar

from pydantic import BeforeValidator
from pydantic_settings import NoDecode

T = TypeVar("T")


def comma_items(text: str) -> list[str]:
    """`a, b,,c` as `["a", "b", "c"]`: each item stripped, the empty ones dropped."""
    return [item.strip() for item in text.split(",") if item.strip()]


def split_commas(value: str | T) -> list[str] | T:
    """
    A comma-separated string as its items, for a `BeforeValidator` to hand on to the field's own validation.

    A value that isn't a string -- built in code, with the items already apart -- is left for validation to judge.
    """
    return comma_items(value) if isinstance(value, str) else value


CommaSeparated: TypeAlias = Annotated[T, NoDecode, BeforeValidator(split_commas)]
"""
A settings field read from one comma-separated env var, each item validated as `T`'s.

`NoDecode`, so pydantic-settings hands the raw string over instead of reading it as JSON.
"""
