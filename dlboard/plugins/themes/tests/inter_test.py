"""The bundled theme's font is actually served to the browser, not just named in the theme."""

from __future__ import annotations

import re

from dash import Dash, html

from dlboard.plugins.themes import default

_WOFF2_MAGIC = b"wOF2"


def test_the_default_theme_serves_the_inter_it_asks_for() -> None:
    """Follow the links a browser would: page -> the `@font-face` stylesheet -> the woff2 it names."""
    app = Dash(__name__)
    app.layout = html.Div()
    default.plug(app)
    client = app.server.test_client()

    page = client.get("/").get_data(as_text=True)
    stylesheet_url = re.search(r'href="(/inter\.[0-9a-f]+\.css)"', page)
    assert stylesheet_url
    stylesheet = client.get(stylesheet_url.group(1))
    font_url = re.search(r'url\("([^"]+)"\)', stylesheet.get_data(as_text=True))
    assert font_url
    font = client.get(font_url.group(1))

    assert 'font-family: "Inter"' in stylesheet.get_data(as_text=True)
    assert stylesheet.mimetype == "text/css"
    assert font.mimetype == "font/woff2"
    assert font.data.startswith(_WOFF2_MAGIC)
