"""
`dlboard sync --watch`, `dlboard import tensorboard --watch` and `serve local --logdir` bring in what is new on a loop:
a pass that fails must not end it, while a single run's failure is the command's own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import requests

from dlboard import _cli


class _Stop(BaseException):
    """Ends the loop under test, which otherwise runs for as long as the process does."""


@dataclass
class _Passes:
    """A `bring` whose passes fail or bring something in, as `outcomes` say, and then stop the loop."""

    outcomes: list[Exception | None]
    asked: int = 0
    said: list[str] = field(default_factory=list[str])

    def __call__(self) -> str:
        self.asked += 1
        if not self.outcomes:
            raise _Stop
        if (failure := self.outcomes.pop(0)) is not None:
            raise failure
        return "a pass"


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("not up yet"), requests.HTTPError("500"), ValueError("a bad state file")],
)
def test_a_pass_that_fails_is_followed_by_the_next(failure: Exception) -> None:
    passes = _Passes([failure, None])

    with pytest.raises(_Stop):
        _cli._bring_in(passes, 0.0, passes.said.append)

    assert (passes.asked, passes.said) == (3, ["Brought in a pass"])


def test_run_once_a_failure_is_the_commands() -> None:
    passes = _Passes([requests.ConnectionError("down")])

    with pytest.raises(requests.ConnectionError):
        _cli._bring_in(passes, None, passes.said.append)
