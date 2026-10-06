"""The theme contract: what a theme plugin hands the core app to render with."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from dlboard._compat import StrEnum

if TYPE_CHECKING:
    from dash_mantine_components import MantineProvider

    MantineTheme = MantineProvider.Theme
else:
    # dash-mantine-components' `Theme` TypedDict names its nested types as string forward refs that
    # only resolve inside `MantineProvider`'s class body, which pydantic can't follow -- so a theme
    # is statically checked against it (pyright catches a mistyped key in any theme plugin) but
    # validated as a plain dict at runtime, then handed to Mantine as-is.
    MantineTheme = dict[str, Any]


class ColorScheme(StrEnum):
    """Which Mantine color scheme a page renders in before the viewer has picked one."""

    LIGHT = "light"
    DARK = "dark"
    AUTO = "auto"
    """Follow the viewer's OS setting (`prefers-color-scheme`)."""


class SchemeColors(BaseModel, frozen=True, extra="forbid"):
    """One CSS color per color scheme."""

    light: str
    dark: str


class ThemeSpec(BaseModel, frozen=True, extra="forbid"):
    """
    Everything the core app needs from a theme, set once at startup via `dlboard.serve.set_theme`.

    The core app builds its `MantineProvider` from this directly, in the very first response --
    no callback, so no extra round trip and no flash of the wrong theme while one is in flight.
    """

    mantine: MantineTheme
    """A [Mantine theme object](https://mantine.dev/theming/theme-object/), passed to Mantine as-is."""

    default_color_scheme: ColorScheme = ColorScheme.DARK

    page_background: SchemeColors
    """
    The page background per scheme, painted before any JS (and so any Mantine CSS) has loaded.

    Match it to the theme's `--mantine-color-body`, or the page flashes this color first.
    """
