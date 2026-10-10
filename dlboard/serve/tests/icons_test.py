from __future__ import annotations

import re
from typing import TYPE_CHECKING

from dash import Dash, html

from dlboard.conftest import props
from dlboard.serve import Icon, icon
from dlboard.serve._icons import ICONS_DIR, install_icons

if TYPE_CHECKING:
    from flask.testing import FlaskClient


def test_every_icon_has_an_svg_and_every_svg_is_an_icon() -> None:
    assert {path.stem for path in ICONS_DIR.glob("*.svg")} == {name.value for name in Icon}


def test_the_served_stylesheet_draws_every_icon() -> None:
    app = Dash(__name__)
    app.layout = html.Div()
    install_icons(app)
    client: FlaskClient = app.server.test_client()

    stylesheet_url = re.search(r'href="(/icons\.[0-9a-f]+\.css)"', client.get("/").get_data(as_text=True))
    assert stylesheet_url
    stylesheet = client.get(stylesheet_url.group(1)).get_data(as_text=True)

    for name in Icon:
        assert re.search(rf'\.{name.class_name} {{ --dl-icon: url\("data:image/svg\+xml,%3Csvg', stylesheet)


def test_an_icon_is_hidden_from_assistive_tech_since_its_button_carries_the_name() -> None:
    rendered = props(icon(Icon.EDIT))

    assert rendered["aria-hidden"] == "true"
    assert rendered["className"] == f"dl-icon {Icon.EDIT.class_name}"
