"""Which runs an experiment page draws: the ones a view selected, else only the newest few."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._page_model import DocumentSettings


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


def shown_run_ids(store: DataStore[...], experiment_id: int, document: DocumentSettings) -> frozenset[int]:
    """
    The runs `document` draws on the page: its selection if it has one, else the newest few.

    A view saved before selections left runs out: it draws the newest few it did not leave out.
    """
    if document.selected_runs is not None:
        return frozenset(document.selected_runs)
    shown = ShownRunsSettings().default_shown_runs
    excluded = frozenset(document.excluded_runs)
    # Read as many extra runs as are left out, so that the newest `shown` that remain are among them.
    newest = store.get_runs(experiment_id, limit=shown + len(excluded), run_ids=None)
    remaining = [run.id for run in newest if run.id not in excluded]
    return frozenset(remaining[:shown])
