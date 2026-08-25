"""Grouping helpers shared by chart types that reduce each run to one representative value."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    import pandas as pd


def last_row_per_run(df: pd.DataFrame) -> pd.DataFrame:
    """One row per run: its most recently logged step, or its last row if there's no step column."""
    if df.empty:
        return df
    return (
        df.loc[df.groupby("run_id")["step"].idxmax()]
        if "step" in df.columns
        else df.drop_duplicates(subset=["run_id"], keep="last")
    )
