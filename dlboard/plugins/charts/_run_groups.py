"""
A line chart's runs grouped by a hyperparameter's value: a mean line per value, in a band from its lowest run to its highest.

Deliberately narrow (#699): one key, its values compared as they read, at most `max_groups` groups, over the runs the panel
already draws -- a pandas reduction of the chart's own frame, with no read of its own. Each run counts at every x
within the range it logged over, by its nearest logged value there (`_nearest_fill_pivot`'s `__y` columns), so runs
that logged at different steps still share a mean. Drawn with Plotly (`_themed_figure`), since Mantine's line chart
draws no band; a group's color comes from its value, the same on every chart and page, unless another value on the chart
takes it first (`_slots`).
"""

from __future__ import annotations

import typing
import zlib

import pandas as pd

from dlboard.models import MetricColumn, hparam_column
from dlboard.serve import SeriesDash, series_color, series_dash
from dlboard.serve._series import SERIES_SLOTS

_NONE: typing.Final = "(none)"
"""The group of the runs that were not given the hyperparameter."""
_BAND_OPACITY: typing.Final = 0.15


def groups_of(dataframe: pd.DataFrame, key: str) -> dict[int, str]:
    """Each run's group: its value of hyperparameter `key`, as it reads, or `_NONE`."""
    column = hparam_column(key)
    runs = dataframe.drop_duplicates(MetricColumn.RUN_ID)
    values = runs[column] if column in runs.columns else pd.Series(None, index=runs.index, dtype=object)
    return {
        int(run): _NONE if pd.isna(value) else str(value)
        for run, value in zip(runs[MetricColumn.RUN_ID], values, strict=True)
    }


def _slots(groups: list[str]) -> dict[str, int]:
    """
    Each group's series slot (`series_color`, then `series_dash`), no two alike, in the order given.

    A group's own color is its value's hash's, so it is the same wherever the value is drawn; one an earlier group on
    the chart has taken (two hashes can share a color: there are only `SERIES_SLOTS`) moves on to the next color free,
    and with every color taken the colors come round again dashed.
    """
    taken: dict[str, int] = {}
    for group in groups:
        own = zlib.crc32(group.encode()) % SERIES_SLOTS
        free = (
            slot
            for dash in range(len(SeriesDash))
            for color in range(SERIES_SLOTS)
            if (slot := dash * SERIES_SLOTS + (own + color) % SERIES_SLOTS) not in taken.values()
        )
        taken[group] = next(free)
    return taken


def grouped_traces(
    pivot: pd.DataFrame, x_axis: str, groups: dict[int, str], key: str, max_groups: int
) -> tuple[list[dict[str, typing.Any]], int]:
    """
    Plotly traces of each group's band and mean over `pivot` (`_nearest_fill_pivot`'s), and how many groups were left out.

    Groups are drawn in the order of their values, the first `max_groups` of them.
    """
    members: dict[str, list[str]] = {}
    for run, group in groups.items():
        if f"{run}__y" in pivot.columns:
            members.setdefault(group, []).append(f"{run}__y")
    drawn = sorted(members)[:max_groups]
    x = pivot[x_axis].tolist()
    traces: list[dict[str, typing.Any]] = []
    for group, slot in _slots(drawn).items():
        values = pivot[members[group]]
        color = series_color(slot)
        hidden = {"type": "scatter", "mode": "lines", "x": x, "line": {"width": 0}, "hoverinfo": "skip"}
        shared = {"legendgroup": group, "showlegend": False}
        name = f"{key}={group} ({len(members[group])} runs)"
        traces += [
            hidden | shared | {"y": values.max(axis=1).tolist()},
            hidden
            | shared
            | {"y": values.min(axis=1).tolist(), "fill": "tonexty", "fillcolor": f"{color}/{_BAND_OPACITY}"},
            {
                "type": "scatter",
                "mode": "lines",
                "x": x,
                "y": values.mean(axis=1).tolist(),
                "name": name,
                "legendgroup": group,
                "line": {"color": color, "width": 2, "dash": series_dash(slot).plotly},
                "hovertemplate": f"{name}<br>{x_axis}: %{{x}}<br>mean: %{{y}}<extra></extra>",
            },
        ]
    return traces, len(members) - len(drawn)
