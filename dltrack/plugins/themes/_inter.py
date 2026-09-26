"""
The Inter font the bundled theme asks for, served by the app itself.

`dark.py` sets `fontFamily: "Inter, sans-serif"`, but nothing ever loaded Inter, so every machine
silently fell back to whatever its own `sans-serif` is. This ships Inter's variable Latin subset
(~48 KB woff2, SIL OFL 1.1, license in `inter-OFL.txt`) next to the theme and serves it from the
app -- no external font CDN, so it also works offline, which a self-hosted tool has to.
"""

from pathlib import Path

from dash import Dash
from flask import Response

_FONT_PATH = Path(__file__).with_name("inter-latin-wght-normal.woff2")
_FONT_ROUTE = "inter-latin-wght-normal.woff2"
_CSS_ROUTE = "inter.css"
_FONT_CACHE_SECONDS = 86_400


def install_inter(app: Dash) -> None:
    """
    Serve the bundled Inter and register the `@font-face` that names it, on `app` only.

    Routes and the stylesheet hang off this app instance, not Dash's global `hooks` registry,
    which would leak across every `Dash` app built in the same process (see CLAUDE.md).
    """
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    font_route = f"{prefix}{_FONT_ROUTE}"
    css_route = f"{prefix}{_CSS_ROUTE}"
    font_face = (
        '@font-face { font-family: "Inter"; font-style: normal; font-weight: 100 900; '
        f'font-display: swap; src: url("{font_route}") format("woff2"); }}'
    )
    app.server.add_url_rule(
        font_route,
        endpoint=font_route,
        view_func=lambda: Response(
            _FONT_PATH.read_bytes(),
            mimetype="font/woff2",
            headers={"Cache-Control": f"public, max-age={_FONT_CACHE_SECONDS}"},
        ),
    )
    app.server.add_url_rule(
        css_route, endpoint=css_route, view_func=lambda: Response(font_face, mimetype="text/css")
    )
    app.css.append_css({"external_url": css_route, "external_only": True})
