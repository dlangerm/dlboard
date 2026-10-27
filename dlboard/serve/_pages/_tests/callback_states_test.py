"""A callback's grouped `State`s: declared once, with where each is read from, and checked where they arrive."""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from dash import Input, State
from pydantic import ValidationError

from dlboard.serve._pages._dash_helpers import CallbackStates


class _Rename(CallbackStates):
    page_json: Annotated[str, State("page", "data")]
    name: Annotated[str | None, Input("name-input", "value")]


def test_a_callback_is_handed_each_field_from_where_it_says_it_is_read() -> None:
    assert {
        field: (type(read_from), read_from.component_id, read_from.component_property)
        for field, read_from in _Rename.states().items()
    } == {"page_json": (State, "page", "data"), "name": (Input, "name-input", "value")}


@pytest.mark.parametrize(
    "sent",
    [
        pytest.param({"page_json": "{}"}, id="a-field-missing"),
        pytest.param({"page_json": "{}", "name": "n", "tab": "t"}, id="a-field-it-does-not-read"),
        pytest.param({"page_json": 7, "name": "n"}, id="a-field-of-the-wrong-type"),
    ],
)
def test_what_the_browser_sent_is_refused_where_it_arrives_when_it_is_malformed(sent: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _Rename.model_validate(sent)
