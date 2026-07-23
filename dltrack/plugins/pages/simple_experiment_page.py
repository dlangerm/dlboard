"""An experiment page that just displays every metric in a data table."""

from __future__ import annotations

from dash import Dash, Output, State, dcc

from dltrack.models import constants
from dltrack.plugins.common import metric_chart
from dltrack.plugins.utilities import get_data_store


def plug(app: Dash) -> None:
    """Plugin for rendering experiment results."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.PAGE_EXPERIMENT_ID, component_property="children"),
        State(constants.STATE_EXPERIMENT_ID, component_property="data"),
    )
    def render_chart(experiment_id: int) -> list[dcc.Loading]:
        store = get_data_store()
        return [
            dcc.Loading(metric_chart.basic_metric_table(store, experiment_id=experiment_id)),
        ]
