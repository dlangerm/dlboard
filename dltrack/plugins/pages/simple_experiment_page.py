"""An experiment page that just displays every metric in a data table."""

from __future__ import annotations

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, Output, State

from dltrack.models import constants


def plug(app: Dash) -> None:
    """Plugin for rendering experiment results."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.PAGE_EXPERIMENT_ID, component_property="children"),
        State(constants.STATE_EXPERIMENT_ID, component_property="data"),
    )
    def render_chart(experiment_id: int) -> dmc.Container:
        return dmc.Container(
            dmc.Stack(
                [
                    dmc.Title(f"Experiment {experiment_id}", order=2),
                    dmc.Text("Metrics and results"),
                    dmc.Loader(id=constants.METRIC_CONTENT_ID),
                ],
                gap="md",
            ),
            py="xl",
        )
