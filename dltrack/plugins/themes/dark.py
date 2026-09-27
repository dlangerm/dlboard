"""A plugin that sets the mantine theme for the dltrack server."""

from dash import Dash

from dltrack.models import ColorScheme, SchemeColors, ThemeSpec
from dltrack.plugins.themes._inter import install_inter
from dltrack.serve import set_theme


def plug(app: Dash) -> None:
    """Set the mantine theme for the dltrack server."""
    install_inter(app)
    set_theme(
        app,
        ThemeSpec(
            mantine={
                "primaryColor": "teal",
                "fontFamily": "Inter, sans-serif",
                "headings": {"fontFamily": "Inter, sans-serif"},
            },
            default_color_scheme=ColorScheme.DARK,
            # Mantine's own default `--mantine-color-body` for each scheme.
            page_background=SchemeColors(light="#ffffff", dark="#242424"),
        ),
    )
