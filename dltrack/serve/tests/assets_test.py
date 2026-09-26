from __future__ import annotations

import pytest
from dash import Dash, html

from dltrack.serve import AssetKind, serve_asset


def _app() -> Dash:
    app = Dash(__name__)
    app.layout = html.Div()
    return app


@pytest.mark.parametrize(
    ("kind", "name", "linked_as"),
    [
        (AssetKind.SCRIPT, "thing.js", '<script src="{url}"'),
        (AssetKind.STYLESHEET, "thing.css", '<link rel="stylesheet" href="{url}"'),
        (AssetKind.FONT, "thing.woff2", None),
    ],
)
def test_an_asset_is_served_immutably_and_linked_only_if_the_page_loads_it(
    kind: AssetKind, name: str, linked_as: str | None
) -> None:
    app = _app()
    url = serve_asset(app, kind, name, b"content")
    client = app.server.test_client()

    response = client.get(url)
    page = client.get("/").get_data(as_text=True)

    assert response.data == b"content"
    assert response.mimetype == kind.value
    assert "immutable" in response.headers["Cache-Control"]
    match linked_as:
        case None:
            assert url not in page
        case str():
            assert linked_as.format(url=url) in page


def test_changed_content_gets_a_new_url_so_a_cached_copy_is_never_stale() -> None:
    app = _app()

    first = serve_asset(app, AssetKind.SCRIPT, "thing.js", b"v1")
    second = serve_asset(app, AssetKind.SCRIPT, "thing.js", b"v2")

    assert first != second
    assert first.startswith("/thing.")
    assert first.endswith(".js")
