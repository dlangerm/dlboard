"""A plugin for a basic metric chart using plotly."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc
import pandas as pd
import plotly.express as px
from dash import dcc, html
from dash.dash_table import DataTable
from structlog.stdlib import get_logger

if TYPE_CHECKING:
    from dltrack.models import DataStore

_log = get_logger(__name__)


def basic_metric_table(
    store: DataStore[...],
    experiment_id: int,
) -> dmc.Accordion:
    """Render a metric chart using a selector."""
    _log.info("rendering chart for selection %s", experiment_id)
    data = store.fetch_metrics(experiment_id=experiment_id)
    all_dfs: list[pd.DataFrame] = [
        pd.DataFrame(d.metrics | {"experiment_id": [d.experiment_id], "step": [d.step]}) for d in data
    ]
    if all_dfs:
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
                    children.append(
                        dcc.Graph(figure=px.line(data_frame=full_df[[col, "step"]], x="step", y=col))
                    )
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

        return dmc.Accordion(
            children=container_children,
            multiple=True,
            chevronPosition="right",
            variant="separated",
        )
    return []
