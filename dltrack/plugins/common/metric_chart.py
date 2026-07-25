"""A plugin for a basic metric chart using plotly."""

from __future__ import annotations

import itertools
import typing
from typing import TYPE_CHECKING, cast

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
import pandas as pd
from dash import Dash, Input, Output, html
from structlog.stdlib import get_logger

from dltrack.models import Page, constants
from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.utilities import get_data_store

if TYPE_CHECKING:
    from dltrack.models import DataStore

_log = get_logger(__name__)


class BasicExperimentPage(Page[pd.DataFrame, dmc.Accordion, html.Div], frozen=True, extra="forbid"):
    """Basic experiment page."""

    @typing.override
    def retrieve_dataframes(
        self,
        store: DataStore[...],
        experiment_id: int,
    ) -> list[pd.DataFrame]:
        """Retrieve the dataframes for each panel one dataframe per panel."""
        required_columns = set(itertools.chain(*[p.hint_required_columns() or {} for p in self.panels]))
        if any(hint is None for hint in required_columns):
            _log.warning("A panel had a missing hint, fetching every metric, this can be expensive")
            dfs = [
                pd.DataFrame.from_dict(
                    d.metrics
                    | {
                        "run_id": [d.run_id],
                        "experiment_id": [d.experiment_id],
                        "step": [d.step],
                    },
                )
                for d in store.fetch_metrics(experiment_id)
            ]
            df = pd.concat(dfs).reset_index()
            return [df] * len(self.panels)

        cols = cast("set[str]", required_columns)
        dfs = [
            pd.DataFrame.from_dict(
                d.metrics
                | {
                    "run_id": [d.run_id],
                    "experiment_id": [d.experiment_id],
                    "step": [d.step],
                },
            )
            for d in store.fetch_metrics(experiment_id=experiment_id, metric_name_match=cols)
        ]
        df = pd.concat(dfs).reset_index()
        return [df] * len(self.panels)

    @typing.override
    def render(
        self,
        data_store: DataStore[...],
        experiment_id: int,
    ) -> dmc.Accordion:
        """Render."""
        return dmc.Accordion(
            id=f"experiment-{experiment_id}",
            value="panel",
            children=[
                dmc.AccordionItem(
                    [
                        dmc.AccordionControl(p.id),
                        dmc.AccordionPanel(
                            dmc.Flex(
                                p.render(d),
                                justify="flex-start",
                                gap="sm",
                            ),
                        ),
                    ],
                    p.id,
                )
                for p, d in zip(
                    self.panels,
                    self.retrieve_dataframes(data_store, experiment_id),
                    strict=True,
                )
            ],
        )


def basic_metric_table(store: DataStore[...], experiment_id: int) -> dmc.Container:
    """Render a metric chart using an accordion with organized prefixes."""
    _log.info("rendering chart for experiment %s", experiment_id)

    page = BasicExperimentPage(
        panels=[
            PanelInstance(
                id="acc",
                charts=[
                    ChartInstance(
                        chart_type="line",
                        parameters={
                            "column": "train/acc_step",
                            "x_axis": "step",
                            "height": 100,
                        },
                    ),
                    ChartInstance(
                        chart_type="line",
                        parameters={
                            "column": "val/acc_epoch",
                            "x_axis": "epoch",
                            "height": 100,
                        },
                    ),
                ],
            ),
            PanelInstance(
                id="loss",
                charts=[
                    ChartInstance(
                        chart_type="line",
                        parameters={
                            "column": "train/loss_step",
                            "x_axis": "step",
                            "height": 300,
                        },
                    ),
                    ChartInstance(
                        chart_type="line",
                        parameters={
                            "column": "val/loss_epoch",
                            "x_axis": "epoch",
                            "height": 200,
                        },
                    ),
                ],
            ),
        ]
    )

    return dmc.Container(page.render(store, experiment_id))


def plug(app: Dash) -> None:
    """Plugin for viewing a basic chart for all metrics."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children"),
        Input(constants.STATE_EXPERIMENT_ID, component_property="data"),
    )
    def fun(experiment_id: int) -> dmc.Container:
        store = get_data_store()
        return basic_metric_table(store, experiment_id=experiment_id)
