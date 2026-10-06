from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from dlboard.models import ChartInstance, NewPage, PanelInstance
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment._views import ViewFile

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.models import User
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_an_exported_view_imports_back_unchanged() -> None:
    page = NewPage[Any, Any](
        experiment_id=1,
        panels=[PanelInstance[Any, Any](name="loss", charts=[ChartInstance[Any, Any](chart_type="line")])],
        page_settings={"excluded_runs": [3]},
    )

    exported = ViewFile.of(page, name="mine").model_dump_json()
    imported = ViewFile.model_validate_json(exported)

    assert imported.name == "mine"
    assert imported.panels == page.panels
    assert imported.page_settings == page.page_settings


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{}",
        '{"name": "x", "panels": [], "dlboard_view": 2}',
        '{"name": "x", "panels": [{"charts": [{"parameters": {}}]}]}',
    ],
    ids=["empty", "not-a-view", "unknown-format-version", "chart-without-a-type"],
)
def test_anything_but_a_valid_view_is_rejected_on_import(raw: str) -> None:
    with pytest.raises(ValidationError):
        ViewFile.model_validate_json(raw)


def test_edits_that_branch_into_a_view_get_distinct_names_per_person(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], User]
) -> None:
    """Never "Copy of Copy of ...": your first branch is "My view", the next "My view 2", and so on."""
    shared = state.load_page(store, state.PageRef(experiment_id, None))

    sign_in_as("alice")
    first = state.save_page(store, shared)
    second = state.save_page(store, shared)
    edited_in_place = state.save_page(store, first.model_copy(update={"page_settings": {"seen": 1}}))
    sign_in_as("bob")
    bobs = state.save_page(store, shared)

    assert [first.name, second.name, bobs.name] == ["My view", "My view 2", "My view"]
    assert (edited_in_place.id, edited_in_place.name) == (first.id, "My view")
