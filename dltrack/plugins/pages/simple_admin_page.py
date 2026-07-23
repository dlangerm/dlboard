"""A simple admin homepage."""

from dash import Dash, Output, html

from dltrack.models import constants


def plug(app: Dash) -> None:
    """An admin page."""

    @app.callback(Output(constants.PAGE_ADMIN_ID, component_property="children"))  # pyright: ignore[reportUnknownMemberType]
    def layout() -> html.Div:
        return html.Div("Admin Page")
