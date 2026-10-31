"""The runs selected in the comparison's list, in the two shapes its grid reports them."""

from __future__ import annotations

import pytest

from dlboard.serve._pages._comparison import _members as members


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        pytest.param(None, set(), id="nothing-yet"),
        pytest.param({"ids": ["4", "9"]}, {4, 9}, id="the-ids-it-was-seeded-with"),
        pytest.param([{"run_id": 4}, {"run_id": 9}], {4, 9}, id="the-rows-once-reported"),
    ],
)
def test_the_selected_runs_are_read_from_either_shape(
    selected: list[dict[str, int]] | dict[str, list[str]] | None, expected: set[int]
) -> None:
    assert members._picked(selected) == frozenset(expected)
