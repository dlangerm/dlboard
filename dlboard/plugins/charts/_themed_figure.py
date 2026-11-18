"""
A Plotly figure drawn in the page's theme: series band's, scatter's, parallel coordinates'.

Plotly drops a color it can't parse, and a theme's colors are CSS variables (`series_color`), so the graph starts
empty and its figure waits in a store: `themed_figure.js` hands Plotly the figure with the theme's colors put in place of
them, when it arrives, and again whenever the color scheme changes.
"""

from __future__ import annotations

import typing
from pathlib import Path

from dash import MATCH, Input, Output, dcc, html

from dlboard.plugins.charts._plotly_colors import serve_plotly_colors
from dlboard.serve import ClientsideScript

if typing.TYPE_CHECKING:
    from dash import Dash

_JS: typing.Final = ClientsideScript(Path(__file__).with_name("themed_figure.js"))

Figure = dict[str, typing.Any]
"""A Plotly figure as JSON, its colors named as theme variables (`var(--dl-series-3)`) wherever it likes."""


def themed_graph(kind: str, instance: str, figure: Figure, height: int) -> html.Div:
    """A graph of `kind` (a chart type's own) drawing `figure` in the theme; register `kind` with `register_themed_graphs`."""
    return html.Div(
        [
            dcc.Store(id={"type": f"{kind}-figure", "instance": instance}, data=figure),
            dcc.Graph(
                id={"type": f"{kind}-graph", "instance": instance},
                figure={"data": [], "layout": {"height": height}},
                config={"displayModeBar": False, "responsive": True},
                style={"height": f"{height}px", "width": "100%"},
            ),
        ]
    )


def register_themed_graphs(app: Dash, kind: str) -> None:
    """Draw every `themed_graph` of `kind` in the theme: from its chart type's `plug`, once."""
    serve_plotly_colors(app)
    app.clientside_callback(
        _JS.source,
        Output({"type": f"{kind}-graph", "instance": MATCH}, "figure"),
        Input({"type": f"{kind}-figure", "instance": MATCH}, "data"),
    )


def plotly_layout(height: int, **layout: typing.Any) -> Figure:  # noqa: ANN401 -- Plotly's own layout JSON
    """A layout in the page's colors on a clear background, as every themed chart has; `layout` adds to it."""
    text, grid = "var(--mantine-color-text)", "var(--mantine-color-default-border)"
    axis = {"gridcolor": grid, "linecolor": grid, "zeroline": False}
    return {
        "height": height,
        "margin": {"l": 60, "r": 20, "t": 20, "b": 50},
        "font": {"color": text},
        "xaxis": dict(axis),
        "yaxis": dict(axis),
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        **layout,
    }
