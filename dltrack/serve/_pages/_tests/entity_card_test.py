from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dltrack import models
from dltrack.conftest import props
from dltrack.serve._pages._dash_helpers import entity_card

if TYPE_CHECKING:
    from collections.abc import Callable


def _rendered(node: Any) -> str:  # noqa: ANN401
    """Every text node and class name in a component tree, space-joined -- what a reader would see."""
    match node:
        case str():
            return node
        case list():
            return " ".join(_rendered(child) for child in node)  # pyright: ignore[reportUnknownVariableType]
        case None:
            return ""
        case _:
            node_props = props(node)
            return f"{node_props.get('className', '')} {_rendered(node_props.get('children'))}"


def _card(stats: models.ActivityStats) -> str:
    return _rendered(entity_card(title="t", description="d", href="/x", stats=stats, class_name="c"))


@pytest.mark.parametrize(
    ("stats_factory", "line"),
    [
        (lambda: models.ActivityStats(), "0 runs · no activity yet"),
        (lambda: models.ActivityStats(run_count=1), "1 run · no activity yet"),
        (
            lambda: models.ProjectStats(experiment_count=2, run_count=5),
            "2 experiments · 5 runs · no activity yet",
        ),
        (
            lambda: models.ActivityStats(
                run_count=3, last_activity_at=pendulum.now("UTC").subtract(minutes=2)
            ),
            "3 runs · active 2 minutes ago",
        ),
        (
            lambda: models.ActivityStats(run_count=3, last_activity_at=pendulum.now("UTC").subtract(days=3)),
            "3 runs · active 3 days ago",
        ),
    ],
)
def test_the_activity_line_reads_naturally(
    stats_factory: Callable[[], models.ActivityStats], line: str
) -> None:
    """
    `stats_factory` builds `last_activity_at` just before rendering, not at collection time.

    `entity_card` diffs it against a fresh `pendulum.now()` -- built at collection time instead, a
    slow CI run could let real time elapse past a minute boundary between the two, flaking "2
    minutes ago" into "3 minutes ago".
    """
    assert line in _card(stats_factory())


@pytest.mark.parametrize(
    ("minutes_ago", "live"),
    [(1, True), (10, False)],
)
def test_only_recent_activity_shows_the_live_dot(minutes_ago: int, *, live: bool) -> None:
    stats = models.ActivityStats(last_activity_at=pendulum.now("UTC").subtract(minutes=minutes_ago))

    assert ("dl-live-dot" in _card(stats)) is live


def test_the_whole_card_is_one_link_named_after_the_entity() -> None:
    card = entity_card(
        title="lr-sweep", description="", href="/experiment/3", stats=models.ActivityStats(), class_name="c"
    )

    link = props(card)
    assert link["href"] == "/experiment/3"
    assert link["aria-label"] == "lr-sweep"
