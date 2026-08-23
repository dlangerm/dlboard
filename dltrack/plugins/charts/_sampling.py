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
        areas = np.abs((px - avg_x) * (seg_y - py) - (px - seg_x) * (avg_y - py))
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
