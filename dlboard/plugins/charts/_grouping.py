"""Grouping helpers shared by chart types that reduce each run to one representative value."""

from __future__ import annotations

import typing

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
