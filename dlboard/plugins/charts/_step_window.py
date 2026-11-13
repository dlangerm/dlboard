"""
The window of steps a chart with a slider has the artifacts of (#364, #619), and the read of the next as the slider leaves.

Its panel hands such a chart every step its key logged, and the artifacts of the first steps alone (`WindowAt.FIRST`):
what it reads does not grow with the steps logged. Moving the slider outside them asks for the steps around where it
went (`step_window.js`), read here.
"""

from __future__ import annotations

import bisect
import typing
from pathlib import Path

from pydantic import BaseModel

from dlboard.models import EXPERIMENT_COLUMN, StepRange
from dlboard.serve import ClientsideScript, build_artifacts_dataframe

if typing.TYPE_CHECKING:
    import pandas as pd

    from dlboard.models import ArtifactWindow, DataStore

WINDOW_JS: typing.Final = ClientsideScript(Path(__file__).with_name("step_window.js"))
"""Asks for the next window when the slider leaves the one the chart has: (slider value, `LoadedSteps`) -> `WindowAsked`."""


class LoadedSteps(BaseModel, frozen=True, extra="forbid"):
    """The steps a chart has the artifacts of, and the runs it reads the next ones of."""

    first: int
    last: int
    runs: dict[int, list[int]]
    """The runs it draws, by the experiment each is of: a chart can draw the runs of several (a comparison), and a read is one's."""


class WindowAsked(BaseModel, frozen=True, extra="forbid"):
    """The step the slider went to, outside the window the chart has: the next window is read around it."""

    step: int


def runs_by_experiment(df: pd.DataFrame) -> dict[int, list[int]] | None:
    """The runs of `df` by their experiment, which a frame of a page's panel names beside each run; `None` if it does not."""
    if EXPERIMENT_COLUMN not in df.columns:
        return None
    grouped: dict[int, list[int]] = {}
    for run_id, experiment_id in df.drop_duplicates("run_id")[["run_id", EXPERIMENT_COLUMN]].itertuples(
        index=False
    ):
        grouped.setdefault(int(experiment_id), []).append(int(run_id))
    return grouped


def read_around(
    store: DataStore[...], runs: dict[int, list[int]], window: ArtifactWindow, step: int
) -> tuple[pd.DataFrame, StepRange] | None:
    """
    `window`'s artifacts at about `window.steps` of the steps `window.key` logged, around `step`, rather than the first.

    Centred on the step asked for, so scrubbing on either side of it asks for nothing more for a while. Reads each
    experiment's runs as a read of it alone: which experiment a run is of is already known. `None` if no run logged any.
    """
    logged = sorted(
        {
            at
            for experiment_id, run_ids in runs.items()
            for steps in store.artifact_steps(
                experiment_id, key=window.key, run_ids=frozenset(run_ids)
            ).values()
            for at in steps
        }
    )
    if not logged:
        return None
    size = window.steps
    start = max(min(bisect.bisect_left(logged, step) - size // 2, len(logged) - size), 0)
    shown = StepRange(logged[start], logged[min(start + size, len(logged)) - 1])
    artifacts = build_artifacts_dataframe(
        artifact
        for experiment_id, run_ids in runs.items()
        for artifact in store.fetch_artifacts(
            experiment_id,
            keys=frozenset({window.key, *window.with_keys}),
            key_prefixes=window.with_prefixes,
            run_ids=frozenset(run_ids),
            steps=shown,
        )
    )
    return artifacts, shown
