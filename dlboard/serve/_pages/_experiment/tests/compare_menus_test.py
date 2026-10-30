"""The menus that start a comparison from runs: how many comparisons they offer, and that a colour is only ever a pin."""

from __future__ import annotations

import pytest

from dlboard.models import ComparisonSummary, PinColor
from dlboard.serve._pages._experiment._compare import OFFERED_COMPARISONS, compare_menu, compare_pins_menu
from dlboard.serve._pages._run_list import RunListSurface


@pytest.fixture(autouse=True)
def _viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The viewer is user 1, with no Flask request for `get_current_user` to read."""
    monkeypatch.setattr(
        "dlboard.serve._pages._experiment._compare.get_current_user", lambda: type("User", (), {"id": 1})()
    )


def _summary(comparison_id: int, owner_id: int = 1) -> ComparisonSummary:
    return ComparisonSummary(
        id=comparison_id,
        name=f"comparison {comparison_id}",
        owner_id=owner_id,
        owner_name="alice" if owner_id == 1 else "bob",
        shared=owner_id != 1,
        run_count=2,
    )


def _item_ids(menu: object) -> list[str]:
    """The colour or comparison each item of the menu's dropdown that has an id names, empty for a new comparison."""
    return [
        node.id["color"] or node.id["comparison_id"]
        for node in menu._traverse()  # type: ignore[attr-defined]
        if isinstance(getattr(node, "id", None), dict)
    ]


def test_the_menu_offers_a_new_comparison_and_a_bounded_few_to_add_to_but_not_the_one_on_screen() -> None:
    addable = [_summary(i) for i in range(1, OFFERED_COMPARISONS + 6)]

    menu = compare_menu(RunListSurface.EXPERIMENT, addable, current=2)

    args = _item_ids(menu)
    assert args[0] == ""  # the new comparison
    assert "2" not in args
    assert len(args) == 1 + OFFERED_COMPARISONS


def test_someone_elses_shared_comparison_is_offered_with_whose_it_is() -> None:
    menu = compare_menu(RunListSurface.EXPERIMENT, [_summary(7, owner_id=2)])

    assert "comparison 7 · bob" in str(menu.to_plotly_json())


def test_the_pins_menu_is_there_only_for_a_colour_with_runs_pinned_in_it() -> None:
    assert compare_pins_menu(RunListSurface.PROJECT, {}) is None

    menu = compare_pins_menu(RunListSurface.PROJECT, {PinColor.RED: 3, PinColor.TEAL: 1})

    assert menu is not None
    assert _item_ids(menu) == ["red", "teal"]
