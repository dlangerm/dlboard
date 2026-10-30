"""Which runs a page draws: an experiment page's selection (else only its newest few), or a comparison's members."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._page_model import BasicExperimentPage, DocumentSettings

type RunScope = Mapping[int, frozenset[int]]
"""The runs a page draws, by the experiment each is of: one experiment's on an experiment page, any number on a comparison."""


class ShownRunsSettings(BaseSettings):
    """`DLBOARD_*` environment variables bounding how many runs a page draws until it is told which."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    default_shown_runs: PositiveInt = 20
    """
    How many of an experiment's newest runs its charts draw until a view selects runs of its own.

    What keeps opening an experiment of thousands of runs from reading all of them: the Runs table lists every run,
    and a run is drawn once it is selected there. Raise it to draw more by default, at the cost of reading that
    many runs' metrics for every chart on every page load and live update.
    """

    max_shown_runs: PositiveInt = 100
    """
    The most runs a view may select in the Runs table to draw. A selection past this is refused, with a notice, so no
    view can ask for every run of an experiment of thousands: each one reads that run's metrics for every chart.
    """


def shown_run_ids(
    store: DataStore[...], experiment_id: int, document: DocumentSettings, focus: int | None = None
) -> frozenset[int]:
    """
    The runs `document` draws on the page: its selection if it has one, else the newest few.

    A view saved before selections left runs out: it draws the newest few it did not leave out. A `focus` (a link to
    one run) draws just that run, if it is a run of the experiment, whatever the view holds.
    """
    if focus is not None and next(store.get_runs(experiment_id, limit=1, run_ids=frozenset({focus})), None):
        return frozenset({focus})
    if document.selected_runs is not None:
        return frozenset(document.selected_runs)
    shown = ShownRunsSettings().default_shown_runs
    excluded = frozenset(document.excluded_runs)
    # Read as many extra runs as are left out, so that the newest `shown` that remain are among them.
    newest = store.get_runs(experiment_id, limit=shown + len(excluded), run_ids=None)
    remaining = [run.id for run in newest if run.id not in excluded]
    return frozenset(remaining[:shown])


def page_scope(
    store: DataStore[...],
    page: BasicExperimentPage,
    hidden: frozenset[int] = frozenset(),
    focus: int | None = None,
) -> RunScope:
    """
    The runs `page` draws, by experiment: the one place that tells an experiment's page from a comparison.

    An experiment page has just its own experiment, whatever runs it shows (`shown_run_ids`, or the `focus`), even none. A comparison has
    the experiment of every member except those the viewer has `hidden`, and none for an experiment all of whose members
    are: which runs a viewer looks at is theirs to choose, and is not saved with the comparison.
    """
    if page.experiment_id is not None:
        return {page.experiment_id: shown_run_ids(store, page.experiment_id, page.document, focus)}
    drawn: dict[int, set[int]] = {}
    for run in store.comparison_runs(page.id):
        if run.id not in hidden:
            drawn.setdefault(run.experiment_id, set()).add(run.id)
    return {experiment_id: frozenset(run_ids) for experiment_id, run_ids in drawn.items()}


def page_experiments(store: DataStore[...], page: BasicExperimentPage) -> list[int]:
    """The experiments `page` draws runs of, for what is asked of an experiment rather than of its runs: its own, always."""
    if page.experiment_id is not None:
        return [page.experiment_id]
    return list(page_scope(store, page))
