"""The homepage for dltrack."""

import dash
import dash_mantine_components as dmc

from dltrack.serve._pages._simple_homepage import render_homepage


def layout() -> dmc.Container:
    return render_homepage()


dash.register_page(__name__, path="/", layout=layout)  # pyright: ignore[reportUnknownMemberType]
