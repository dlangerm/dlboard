"""
A chart's step slider, and the lock that moves every step slider of a synced panel together, whatever chart it is of.

Every chart with a slider over the steps (an image chart's, a Plotly chart's) gives it the one id type, under its own
chart's name: its own callbacks match its name, and the lock matches every chart's, so a prediction's picture and its PR
curve move to the same step (#621).
"""

from __future__ import annotations

import typing
from pathlib import Path

import dash

from dlboard.serve import AppSlot, ClientsideScript

STEP_SLIDER: typing.Final = "dl-step-slider"

_LOCK_JS: typing.Final = ClientsideScript(Path(__file__).with_name("step_slider_lock.js"))
_LOCKED: AppSlot[bool] = AppSlot("step slider lock")


def step_slider(chart: str, instance: object) -> dict[str, object]:
    """The id of the step slider of the chart `instance` of the chart type named `chart`, or a pattern over them."""
    return {"type": STEP_SLIDER, "chart": chart, "instance": instance}


def lock_step_sliders(app: dash.Dash) -> None:
    """Move every other step slider of a synced panel (`data-dl-panel-sync`) along with the one dragged: once per app."""
    if _LOCKED.find(app):
        return
    _LOCKED.set(app, value=True)
    every = {"type": STEP_SLIDER, "chart": dash.ALL, "instance": dash.ALL}
    app.clientside_callback(
        _LOCK_JS.source,
        dash.Input(every, "value"),
        dash.State(every, "id"),
        dash.State(every, "marks"),
        prevent_initial_call=True,
    )
