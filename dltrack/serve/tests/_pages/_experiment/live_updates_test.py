# pyright: reportPrivateUsage=false
"""
Tests for the live-update poll's decision logic and status badge.

`poll_for_updates` itself is a registered Dash callback (see `__init__.py`), following this
codebase's convention of testing the pure logic/render functions a callback delegates to rather
than driving the callback through a running app -- `_needs_rerender` is that decision, and
`live_status_badge` is the small chip it renders.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dltrack import models
from dltrack.conftest import props
from dltrack.serve._pages._experiment import _experiment_page_state as state
from dltrack.serve._pages._experiment import _needs_rerender

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _experiment(revision: int) -> models.Experiment:
    return models.Experiment(id=1, project_id=1, revision=revision, created_at=_TS)


def test_needs_rerender_is_false_when_revision_is_unchanged() -> None:
    assert _needs_rerender(_experiment(3), 3) is False


def test_needs_rerender_is_true_when_revision_has_moved() -> None:
    assert _needs_rerender(_experiment(4), 3) is True


def test_needs_rerender_treats_first_ever_poll_as_unchanged_until_a_write_happens() -> None:
    """Both sides start at the freshly-created experiment's revision (`0`) -- not a mismatch."""
    assert _needs_rerender(_experiment(0), 0) is False


def test_needs_rerender_handles_a_client_that_has_never_polled_yet() -> None:
    """`last_known_revision` is `None` (not `0`) before the client's `dcc.Store` is ever seeded."""
    assert _needs_rerender(_experiment(0), None) is True


def test_needs_rerender_is_false_when_the_experiment_is_gone() -> None:
    assert _needs_rerender(None, 3) is False


def test_live_status_badge_reports_ok_and_failure_distinctly() -> None:
    ok_badge = state.live_status_badge(ok=True)
    failed_badge = state.live_status_badge(ok=False)

    assert props(ok_badge)["color"] == "green"
    assert props(failed_badge)["color"] == "red"
