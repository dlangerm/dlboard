"""The bundled theme's font is actually served to the browser, not just named in the theme."""

from __future__ import annotations

from dash import Dash, html

from dltrack.plugins.themes import dark

_WOFF2_MAGIC = b"wOF2"


def test_the_dark_theme_serves_the_inter_it_asks_for() -> None:
    app = Dash(__name__)
    app.layout = html.Div()
    dark.plug(app)
    client = app.server.test_client()

    page = client.get("/").get_data(as_text=True)
    stylesheet = client.get("/inter.css")
    font = client.get("/inter-latin-wght-normal.woff2")

    assert '/inter.css"' in page
    assert 'font-family: "Inter"' in stylesheet.get_data(as_text=True)
    assert stylesheet.mimetype == "text/css"
    assert font.mimetype == "font/woff2"
    assert font.data.startswith(_WOFF2_MAGIC)
