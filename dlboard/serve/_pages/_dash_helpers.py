"""Small shared helpers: pattern-matched (`ALL`) Dash callbacks, plus a couple of reused components."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pendulum
from dash import ctx, html
from dash.exceptions import PreventUpdate

from dlboard import models
from dlboard.serve import Icon, icon

if TYPE_CHECKING:
    from dash.development.base_component import Component


def tooltipped_action_icon(  # noqa: PLR0913
    name: Icon,
    *,
    component_id: str | dict[str, Any],
    label: str,
    color: str = "gray",
    size: str = "sm",
    disabled: bool = False,
    **extra: Any,  # noqa: ANN401
) -> Component:
    """
    A hover-tooltipped `ActionIcon`.

    The shared fixture for every icon-only edit/delete/suggest control across the app, so each one
    doesn't need to remember to wrap itself in a `Tooltip` -- an icon with no visible label is
    otherwise not self-explanatory. The tooltip text doubles as the button's accessible name.
    """
    return dmc.Tooltip(
        dmc.ActionIcon(
            icon(name),
            id=component_id,
            n_clicks=0,
            variant="subtle",
            size=size,
            color=color,
            disabled=disabled,
            **{"aria-label": label},
            **extra,
        ),
        label=label,
        position="top",
        withArrow=True,
    )


LIVE_WINDOW = pendulum.duration(minutes=5)
"""Data arriving within this long ago marks a project/experiment as live (a pulsing dot)."""


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def entity_card(
    *,
    title: str,
    description: str,
    href: str,
    stats: models.ActivityStats,
    class_name: str,
) -> Component:
    """
    A whole-card link to a project/experiment: name, description, and an at-a-glance activity line.

    The entire card is the click target (not a button inside it), and `stats` reads as e.g.
    "3 experiments · 12 runs · active 3 minutes ago", with a live dot when data arrived within
    `LIVE_WINDOW`.
    """
    match stats:
        case models.ProjectStats():
            counts = {"experiment": stats.experiment_count, "run": stats.run_count}
        case models.ActivityStats():
            counts = {"run": stats.run_count}
    # Measured against an explicit UTC "now": left implicit (no `other`), pendulum reported a
    # 2-minutes-old UTC timestamp as "1 hour ago" here (see `entity_card_test.py`).
    now = pendulum.now("UTC")
    last_seen = pendulum.instance(stats.last_activity_at) if stats.last_activity_at else None
    activity = (
        f"active {last_seen.diff_for_humans(now, absolute=True)} ago" if last_seen else "no activity yet"
    )
    live = last_seen is not None and now - last_seen < LIVE_WINDOW
    return dmc.Anchor(
        dmc.Card(
            [
                dmc.Group(
                    [
                        dmc.Text(title, fw=600, truncate="end"),
                        dmc.Tooltip(html.Span(className="dl-live-dot"), label="Receiving data")
                        if live
                        else None,
                    ],
                    justify="space-between",
                    wrap="nowrap",
                ),
                dmc.Text(description or "No description", size="sm", c="dimmed", lineClamp=2, mt=4),
                dmc.Text(
                    " · ".join([*(_plural(n, noun) for noun, n in counts.items()), activity]),
                    size="xs",
                    c="dimmed",
                    mt="auto",
                    pt="md",
                    className="dl-tabular",
                ),
            ],
            padding="lg",
            h="100%",
        ),
        href=href,
        underline="never",
        c="inherit",
        className=f"dl-card-link {class_name}",
        **cast("dict[str, Any]", {"aria-label": title}),
    )


def section_label(text: str) -> dmc.Text:
    """A small, quiet uppercase heading that groups a list (the navbar's Experiments, Runs, ...)."""
    return dmc.Text(text, size="xs", fw=600, c="dimmed", tt="uppercase", className="dl-section-label")


def require_triggered_id() -> Any:  # noqa: ANN401
    """
    Return `ctx.triggered_id`, or raise `PreventUpdate` if nothing meaningfully triggered.

    Dash still fires pattern-matched callbacks when a listened component is created with its
    property at a falsy default (e.g. a freshly-rendered button's `n_clicks=0`); checking
    `ctx.triggered[0]["value"]` distinguishes that no-op firing from an actual click/change.
    Callers `cast(...)` the result to the triggered-id shape they expect.
    """
    if not ctx.triggered_id or not ctx.triggered[0]["value"]:  # pyrefly: ignore [unsupported-operation]
        raise PreventUpdate
    return ctx.triggered_id
