"""A plugin that sets the mantine theme for the dltrack server."""

from dash import Dash, Input, Output
from structlog.stdlib import get_logger

from dltrack.models import constants

_log = get_logger(__name__)


class DarkTheme:
    """A plugin that sets the mantine theme for the dltrack server."""

    @classmethod
    def plug(cls, app: Dash) -> None:
        """Set the mantine theme for the dltrack server."""

        @app.callback(  # pyright: ignore[reportUnknownMemberType]
            Output(component_id=constants.MANTINE_PROVIDER_ID, component_property="theme"),
            Output(component_id=constants.MANTINE_PROVIDER_ID, component_property="forceColorScheme"),
            Input(component_id=constants.MANTINE_PROVIDER_ID, component_property="id"),
        )
        def set_theme(_: str) -> tuple[dict[str, str | dict[str, str]], str]:
            _log.info("Setting the mantine theme for the dltrack server.")
            return {
                "primaryColor": "teal",
                "fontFamily": "Inter, sans-serif",
                "headings": {"fontFamily": "Inter, sans-serif"},
            }, "dark"
