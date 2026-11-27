"""`CommaSeparated`: one env var of comma-separated items reads as those items, however it was spaced."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard._comma_separated import CommaSeparated


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DLBOARD_TEST_")

    items: CommaSeparated[list[str]] = []


_ITEMS = st.lists(st.text(alphabet="ab.:/", min_size=1, max_size=3), max_size=4)
_PADDING = st.text(alphabet=" \t,", max_size=3)


@given(items=_ITEMS, padding=st.data())
def test_an_env_var_reads_as_its_items_whatever_the_spacing_and_empty_items(
    items: list[str], padding: st.DataObject
) -> None:
    text = ",".join(f"{padding.draw(_PADDING)}{item}{padding.draw(_PADDING)}" for item in items)

    with pytest.MonkeyPatch.context() as env:
        env.setenv("DLBOARD_TEST_ITEMS", text)
        assert _Settings().items == items


def test_items_given_in_code_are_left_as_they_are() -> None:
    assert _Settings(items=["a, b"]).items == ["a, b"]
