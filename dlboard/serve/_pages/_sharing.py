"""
A page's Private/Shared switch, in its header, for its owner: a view of an experiment, or a comparison.

It replaces a "Share with project" item in the page's ⋯ menu, which nobody found: that a view is its owner's alone until
they share it was invisible, and so was how to share it.
"""

from __future__ import annotations

from typing import Any, cast

import dash_mantine_components as dmc
from dash import html

from dlboard.serve import Icon, icon


def sharing_switch(component_id: str, *, shared: bool, noun: str) -> html.Div:
    """
    The switch of a page `shared` or not: its thumb a lock, or people, and a tooltip saying what each means.

    Its `checked` is whether the page is shared. No words beside it: the header it sits in is full, and its tooltip says
    more than a word would. In a `Div`, which the tooltip can hold on to.
    """
    return html.Div(
        dmc.Tooltip(
            dmc.Switch(
                id=component_id,
                checked=shared,
                onLabel=icon(Icon.SHARED),
                offLabel=icon(Icon.KEPT),
                size="md",
                **cast("dict[str, Any]", {"aria-label": f"Share this {noun} with the project"}),
            ),
            label=f"A lock: private, only you can open this {noun}. People: shared, everyone in the project can open "
            "it too, and what they change goes into a copy of their own.",
            multiline=True,
            w=280,
            withArrow=True,
            openDelay=300,
        )
    )
