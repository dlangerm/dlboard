"""Grouping helpers shared by chart types that reduce each run to one representative value."""

from __future__ import annotations

import typing

from dlboard.models import RUN_NAME_COLUMN, MetricColumn, hparam_column

if typing.TYPE_CHECKING:
    import pandas as pd


def latest_per_run(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """
    One row per run with a value of every one of `columns`: the latest it logged of each, at whatever step.

    Two metrics logged at different steps (one per epoch, one per step) each contribute their own latest, rather
    than there having to be a step that logged both.
    """
    if "step" in df.columns:
        df = df.sort_values(["run_id", "step"])
    # `run_id` may be one of the columns (a bar chart of runs): it is the grouping, so it comes back as the index.
    return (
        df.groupby("run_id")[[column for column in columns if column != "run_id"]]
        .last()
        .dropna()
        .reset_index()
    )


def frame_column(dataframe: pd.DataFrame, name: str) -> str:
    """
    The panel-frame column a chart setting names: a metric's own column, or a hyperparameter's (`hparam_column`).

    A setting that takes either (a bar chart's x-axis, a scatter's axes) names the key alone, and the frame says which it
    is: a hyperparameter's column only exists under its prefixed name.
    """
    prefixed = hparam_column(name)
    return prefixed if prefixed in dataframe.columns else name


def run_names(dataframe: pd.DataFrame) -> dict[int, str]:
    """What each run of a panel frame is called, by id; a run with no name (from before names) is left out."""
    if RUN_NAME_COLUMN not in dataframe.columns:
        return {}
    named = dataframe.drop_duplicates(MetricColumn.RUN_ID)[[MetricColumn.RUN_ID, RUN_NAME_COLUMN]].dropna()
    return {int(run): str(name) for run, name in named.itertuples(index=False)}
