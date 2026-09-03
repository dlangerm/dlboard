"""Small shared helpers: pattern-matched (`ALL`) Dash callbacks, plus a couple of reused components."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dash_mantine_components as dmc
from dash import ctx
from dash.exceptions import PreventUpdate

if TYPE_CHECKING:
    from dash.development.base_component import Component


def tooltipped_action_icon(  # noqa: PLR0913
    icon: str,
    *,
    component_id: str | dict[str, Any],
    label: str,
    color: str | None = None,
    size: str = "sm",
    disabled: bool = False,
    **extra: Any,  # noqa: ANN401
) -> Component:
    """
    A hover-tooltipped `ActionIcon`.

    The shared fixture for every icon-only edit/delete/suggest control across the app, so each one
    doesn't need to remember to wrap itself in a `Tooltip` -- an icon with no visible label is
    otherwise not self-explanatory.
    """
    return dmc.Tooltip(
        dmc.ActionIcon(
            icon,
            id=component_id,
            n_clicks=0,
            variant="subtle",
            size=size,
            color=color,
            disabled=disabled,
            **extra,
        ),
        label=label,
        position="top",
        withArrow=True,
    )


def require_triggered_id() -> Any:  # noqa: ANN401
    """
    Return `ctx.triggered_id`, or raise `PreventUpdate` if nothing meaningfully triggered.

    Dash still fires pattern-matched callbacks when a listened component is created with its
    property at a falsy default (e.g. a freshly-rendered button's `n_clicks=0`); checking
    `ctx.triggered[0]["value"]` distinguishes that no-op firing from an actual click/change.
    Callers `cast(...)` the result to the triggered-id shape they expect.
    """
    if not ctx.triggered_id or not ctx.triggered[0]["value"]:  # pyright: ignore[reportUnknownMemberType]
        raise PreventUpdate
    return ctx.triggered_id  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
