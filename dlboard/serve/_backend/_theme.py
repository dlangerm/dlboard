"""The app's theme, set once at startup by a theme plugin's `plug()`, read by `dlboard.serve.app.app`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard.models import ColorScheme, SchemeColors, ThemeSpec
from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from dash import Dash

THEME: AppSlot[ThemeSpec] = AppSlot("theme")

DEFAULT_THEME = ThemeSpec(
    mantine={},
    default_color_scheme=ColorScheme.LIGHT,
    page_background=SchemeColors(light="#ffffff", dark="#242424"),
)
"""What the core app renders with when no theme plugin is installed: plain Mantine defaults."""

set_theme = THEME.set


def get_theme(app: Dash) -> ThemeSpec:
    """The theme a theme plugin set for `app`, or `DEFAULT_THEME` if none is installed."""
    return THEME.find(app) or DEFAULT_THEME
