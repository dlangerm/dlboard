"""The heading a metric chart carries above its plot."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc

if TYPE_CHECKING:
    from dash.development.base_component import Component


def titled(title: str, plot: Component, *below: Component) -> dmc.Stack:
    """
    `plot` under `title`, in full, and anything said about it `below`.

    A plot's own axis titles are shortened to fit it (see `fit_axis_label`), so a long metric name
    could not otherwise be read whole.
    """
    return dmc.Stack(
        [dmc.Text(title, fw=700, size="sm", style={"overflowWrap": "anywhere"}), plot, *below], gap="xs"
    )
