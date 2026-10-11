"""Downsampling helpers for large chart series (no Dash, no chart types)."""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_MAX_POINTS = 500
_MIN_LTTB_THRESHOLD = 3


def _lttb_indices(x: np.ndarray, y: np.ndarray, threshold: int) -> np.ndarray:
    """Largest-Triangle-Three-Buckets: pick `threshold` indices that best preserve the series' shape."""
    n = len(x)
    if threshold >= n or threshold < _MIN_LTTB_THRESHOLD:
        return np.arange(n)

    bucket_size = (n - 2) / (threshold - 2)
    indices = np.empty(threshold, dtype=np.int64)
    indices[0] = 0
    indices[-1] = n - 1

    a = 0
    for i in range(threshold - 2):
        bucket_start = int(i * bucket_size) + 1
        bucket_end = int((i + 1) * bucket_size) + 1

        next_start = bucket_end
        next_end = min(int((i + 2) * bucket_size) + 1, n)

        avg_x = x[next_start:next_end].mean()
        avg_y = y[next_start:next_end].mean()

        px, py = x[a], y[a]
        seg_x = x[bucket_start:bucket_end]
        seg_y = y[bucket_start:bucket_end]
        areas = np.abs(
            # pyrefly: ignore [unknown-argument-type]
            (px - avg_x) * (seg_y - py) - (px - seg_x) * (avg_y - py)
        )
        chosen = bucket_start + int(np.argmax(areas))

        indices[i + 1] = chosen
        a = chosen

    return indices


def downsample_series(
    df: pd.DataFrame, x_col: str, y_col: str, max_points: int = DEFAULT_MAX_POINTS
) -> pd.DataFrame:
    """Downsample one (x, y) series to at most `max_points` rows via LTTB, preserving peaks/valleys."""
    if len(df) <= max_points:
        return df

    x = df[x_col]
    x_numeric = (
        x.astype("int64").to_numpy(dtype=float)
        if pd.api.types.is_datetime64_any_dtype(x)
        else x.to_numpy(dtype=float)
    )
    y_numeric = df[y_col].to_numpy(dtype=float)

    idx = _lttb_indices(x_numeric, y_numeric, max_points)
    return df.iloc[idx]


def downsample_grouped(
    df: pd.DataFrame, x_col: str, y_col: str, group_col: str, max_points: int = DEFAULT_MAX_POINTS
) -> pd.DataFrame:
    """Downsample each group's series (e.g. one line per run) independently, so every line keeps its shape."""
    if df.empty:
        return df
    parts = [
        downsample_series(g, x_col, y_col, max_points)
        for _, g in df.sort_values([group_col, x_col]).groupby(group_col)
    ]
    return pd.concat(parts, ignore_index=True)


def shared_sample_grid(
    df: pd.DataFrame, x_col: str, group_col: str, value_cols: list[str], max_points: int = DEFAULT_MAX_POINTS
) -> pd.DataFrame:
    """
    Pick one shared set of x-values per group, from the union of each value column's own LTTB picks.

    Two line charts showing different metrics but the same x-axis/grouping (e.g. two charts in the
    same panel) that each downsample independently pick different x-values, so a synced tooltip
    only lines up where their sample sets happen to intersect. Sampling every relevant column
    together and taking the union of their picks means every chart built from this grid shares
    the same x-values, so synced tooltips always land on a real point in every chart.

    Returns a `[group_col, x_col]` frame -- inner-join it against a chart's own (x, y) frame to
    apply the shared grid.
    """
    if df.empty or not value_cols:
        return df.loc[:, [group_col, x_col]].drop_duplicates()

    # Every column is handled as one numpy matrix rather than sliced out of the frame one by one: a
    # panel of hundreds of charts runs this once per chart, over every sibling column, so per-column
    # pandas indexing here was by far the biggest cost of rendering a big panel.
    parts: list[pd.DataFrame] = []
    for _, g in df.sort_values([group_col, x_col]).groupby(group_col):
        values = g[value_cols].to_numpy(dtype=float)
        present = ~np.isnan(values)
        counts = present.sum(axis=0)
        # A column within budget keeps every x it has a value at; only longer ones need LTTB.
        selected = present[:, counts <= max_points].any(axis=1)
        too_long = np.flatnonzero(counts > max_points)
        if too_long.size:
            x = g[x_col]
            x_numeric = (
                x.astype("int64").to_numpy(dtype=float)
                if pd.api.types.is_datetime64_any_dtype(x)
                else x.to_numpy(dtype=float)
            )
            for col in too_long:
                rows = np.flatnonzero(present[:, col])
                selected[rows[_lttb_indices(x_numeric[rows], values[rows, col], max_points)]] = True
        if selected.any():
            parts.append(g.loc[selected, [group_col, x_col]].drop_duplicates())
    if not parts:
        return df.loc[:, [group_col, x_col]].drop_duplicates()
    return pd.concat(parts, ignore_index=True)
