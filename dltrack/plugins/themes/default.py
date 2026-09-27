"""The built-in dltrack theme: slate neutrals, one indigo-violet accent, Inter, light and dark."""

from pathlib import Path
from typing import Any, cast

from dash import Dash

from dltrack.models import ColorScheme, SchemeColors, ThemeSpec
from dltrack.plugins.themes._inter import install_inter
from dltrack.serve import AssetKind, serve_asset, set_theme

_CSS_PATH = Path(__file__).with_name("default.css")

# Mantine's `dark` scale (index 0 = text, 7 = body) re-tinted from neutral gray to cool slate.
_SLATE = [
    "#d5d9e2",
    "#b4bac7",
    "#8f97a8",
    "#6b7386",
    "#454c5c",
    "#323845",
    "#232833",
    "#1a1e27",
    "#14171f",
    "#0e1016",
]
_BRAND = [
    "#eef0ff",
    "#dde0ff",
    "#bcc0fb",
    "#9ea2f8",
    "#8487f4",
    "#7174f1",
    "#6264ec",
    "#5557dc",
    "#4a4bc9",
    "#3c3da8",
]
_SANS = (
    "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif, "
    "'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji'"
)
_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"


def plug(app: Dash) -> None:
    """Set the dltrack theme, and serve the font and stylesheet it relies on."""
    install_inter(app)
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
    set_theme(
        app,
        ThemeSpec(
            mantine={
                # Mantine wants each color as a 10-shade array; DMC's generated `Theme` stub says dict.
                "colors": cast("Any", {"dark": _SLATE, "brand": _BRAND}),
                "primaryColor": "brand",
                "fontFamily": _SANS,
                "fontFamilyMonospace": _MONO,
                "fontSmoothing": True,
                "defaultRadius": "md",
                # The one decorative flourish: "magic" actions (auto-generate/suggest charts).
                "defaultGradient": {"from": "brand.5", "to": "grape.6", "deg": 135},
                "cursorType": "pointer",
                "headings": {
                    "fontFamily": _SANS,
                    "fontWeight": "600",
                    "sizes": {
                        "h1": {"fontSize": "1.75rem", "lineHeight": "1.3"},
                        "h2": {"fontSize": "1.375rem", "lineHeight": "1.35"},
                        "h3": {"fontSize": "1.125rem", "lineHeight": "1.4"},
                        "h4": {"fontSize": "1rem", "lineHeight": "1.45"},
                    },
                },
                "components": {
                    "Card": {"defaultProps": {"withBorder": True}},
                    "Tooltip": {"defaultProps": {"openDelay": 300, "withArrow": True}},
                },
            },
            default_color_scheme=ColorScheme.DARK,
            page_background=SchemeColors(light="#ffffff", dark=_SLATE[7]),
        ),
    )
