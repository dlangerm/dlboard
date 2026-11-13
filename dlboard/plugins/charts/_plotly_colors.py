"""The browser's resolver of the CSS-variable colors a figure dlboard draws names (`plotly_colors.js`), shared by its charts."""

from __future__ import annotations

import typing
from pathlib import Path

from dlboard.serve import AppSlot, AssetKind, serve_asset

if typing.TYPE_CHECKING:
    from dash import Dash

_PATH: typing.Final = Path(__file__).with_name("plotly_colors.js")
_SERVED: AppSlot[str] = AppSlot("plotly colors")
"""The URL `plotly_colors.js` is served from, once it is."""


def serve_plotly_colors(app: Dash) -> None:
    """Serve `window.dlPlotlyColors` from `app`, once, whichever chart plugin asks first."""
    if _SERVED.find(app) is None:
        _SERVED.set(app, serve_asset(app, AssetKind.SCRIPT, _PATH.name, _PATH.read_bytes()))
