"""A plugin for a basic metric chart using plotly."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, Input, Output
from structlog.stdlib import get_logger

from dltrack.models import constants
from dltrack.plugins.utilities import get_data_store

if TYPE_CHECKING:
    from dltrack.models import DataStore

_log = get_logger(__name__)


class GroupLogic(StrEnum):
    """Logic for grouping metrics in the chart."""

    NONE = "none"
    PREFIX = "prefix"
    SUFFIX = "suffix"

    def extract(self, name: str, delimiter: str = "/") -> tuple[str, str]:
        """Extract the group and metric name from a metric name."""
        match self:
            case GroupLogic.NONE:
                return "", name
            case GroupLogic.PREFIX:
                parts = name.split(delimiter, 1)
                if len(parts) == 1:
                    return "", name
                return parts[0], parts[1]
            case GroupLogic.SUFFIX:
                parts = name.rsplit(delimiter, 1)
                if len(parts) == 1:
                    return "", name
                return parts[0], parts[1]


def basic_metric_table(store: DataStore[...], experiment_id: int) -> dmc.Container:
    """Render a metric chart using an accordion with organized prefixes."""
    _log.info("rendering chart for experiment %s", experiment_id)
    data = store.fetch_metrics(experiment_id=experiment_id)

    return dmc.Container(
        dmc.Stack(
            [dmc.Text("Chart rendering logic would go here.", c="dimmed")],
        )
    )


def plug(app: Dash) -> None:
    """Plugin for viewing a basic chart for all metrics."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children"),
        Input(constants.STATE_EXPERIMENT_ID, component_property="data"),
    )
    def fun(experiment_id: int) -> dmc.Container:
        store = get_data_store()
        return basic_metric_table(store, experiment_id=experiment_id)
