"""A plugin for a basic metric chart using plotly."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
import pandas as pd
import plotly.express as px
from dash import Dash, Input, Output, dcc
from structlog.stdlib import get_logger

from dltrack.models import constants
from dltrack.plugins.utilities._data_store import get_data_store

if TYPE_CHECKING:
    from dltrack.models import DataStore

_log = get_logger(__name__)


def basic_metric_table(
    store: DataStore[...],
    experiment_id: int,
) -> dmc.Container:
    """Render a metric chart using an accordion with organized prefixes."""
    _log.info("rendering chart for experiment %s", experiment_id)
    data = store.fetch_metrics(experiment_id=experiment_id)
    all_dfs: list[pd.DataFrame] = [
        pd.DataFrame(d.metrics | {"experiment_id": [d.experiment_id], "step": [d.step]}) for d in data
    ]
    if not all_dfs:
        _log.warning("No metrics")
        return dmc.Container(
            dmc.Stack(
                [dmc.Text("No metrics recorded yet for this experiment.", c="dimmed")],
                gap="md",
            ),
            py="xl",
        )

    full_df = pd.concat(all_dfs).set_index(["experiment_id", "step"])
    common_prefixes: set[str] = set()
    for ax in full_df.columns:
        common_prefixes.add(ax.split("/", maxsplit=1)[0])
    _log.info("Common prefixes %s", common_prefixes)

    full_df = full_df.sort_index().reset_index()
    handled_cols: set[str] = set(full_df.columns)
    handled_cols.remove("step")
    container_children: list[dmc.AccordionItem] = []

    for prefix in sorted(common_prefixes):
        children: list[dcc.Graph] = []
        for col in sorted(full_df.columns):
            if col.startswith(prefix) and col in full_df:
                children.append(dcc.Graph(figure=px.line(data_frame=full_df[[col, "step"]], x="step", y=col)))
                handled_cols.remove(col)
        container_children.append(
            dmc.AccordionItem(
                children=[
                    dmc.AccordionControl(prefix),
                    dmc.AccordionPanel(dmc.Flex(children, justify="space-between", wrap="wrap")),
                ],
                value=prefix,
            )
        )

    return dmc.Container(
        dmc.Stack(
            [
                dmc.Text(f"{len(common_prefixes)} metric group(s)", size="sm", c="dimmed"),
                dmc.Accordion(
                    children=container_children,
                    multiple=True,
                    chevronPosition="right",
                    variant="separated",
                ),
            ],
            gap="md",
        ),
        py="md",
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
