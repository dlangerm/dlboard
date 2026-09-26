"""
The Inter font the bundled theme asks for, served by the app itself.

`dark.py` sets `fontFamily: "Inter, sans-serif"`, but nothing ever loaded Inter, so every machine
silently fell back to whatever its own `sans-serif` is. This ships Inter's variable Latin subset
(~48 KB woff2, SIL OFL 1.1, license in `inter-OFL.txt`) next to the theme and serves it from the
app -- no external font CDN, so it also works offline, which a self-hosted tool has to.
"""

from pathlib import Path

from dash import Dash

from dltrack.serve import AssetKind, serve_asset

_FONT_PATH = Path(__file__).with_name("inter-latin-wght-normal.woff2")


def install_inter(app: Dash) -> None:
    """Serve the bundled Inter and register the `@font-face` that names it, on `app` only."""
    font_route = serve_asset(app, AssetKind.FONT, _FONT_PATH.name, _FONT_PATH.read_bytes())
    font_face = (
        '@font-face { font-family: "Inter"; font-style: normal; font-weight: 100 900; '
        f'font-display: swap; src: url("{font_route}") format("woff2"); }}'
    )
    serve_asset(app, AssetKind.STYLESHEET, "inter.css", font_face.encode())
