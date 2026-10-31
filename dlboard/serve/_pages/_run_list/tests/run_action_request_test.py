"""What a run list's buttons may ask for, validated where it arrives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dlboard.models import PinColor
from dlboard.serve._pages._run_list import RunAction, RunActionRequest


def test_a_request_names_its_runs_once_however_the_browser_listed_them() -> None:
    request = RunActionRequest.model_validate({"action": "hide", "run_ids": [3, 5, 3], "at": 1})

    assert (request.action, request.run_ids) == (RunAction.HIDE, frozenset({3, 5}))


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"action": "explode", "run_ids": [1], "at": 1}, id="an-action-that-does-not-exist"),
        pytest.param(
            {"action": "delete", "run_ids": [1], "color": "red", "at": 1}, id="a-colour-it-does-not-take"
        ),
        pytest.param({"action": "draw", "run_ids": ["a"], "at": 1}, id="a-run-that-is-not-a-number"),
        pytest.param(
            {"action": "draw", "run_ids": [1], "at": 1, "extra": 1}, id="something-it-did-not-ask-for"
        ),
    ],
)
def test_a_malformed_request_is_refused_where_it_arrives(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunActionRequest.model_validate(raw)


def test_a_pin_names_its_colour_in_its_argument() -> None:
    request = RunActionRequest.model_validate({"action": "pin", "run_ids": [1], "color": "red", "at": 1})

    assert request.color is PinColor.RED


def test_comparing_the_runs_pinned_in_a_colour_names_a_colour_and_no_runs() -> None:
    request = RunActionRequest.model_validate(
        {"action": "compare_pins", "run_ids": [], "color": "red", "at": 1}
    )

    assert (request.color, request.run_ids) == (PinColor.RED, frozenset())


def test_adding_runs_to_a_comparison_names_it() -> None:
    request = RunActionRequest.model_validate(
        {"action": "compare_add", "run_ids": [1, 2], "comparison_id": 12, "at": 1}
    )

    assert request.comparison_id == 12
