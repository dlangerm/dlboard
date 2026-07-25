"""An experiment page that just displays every metric in a data table."""

from __future__ import annotations

import itertools

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import Dash, Input, Output
from dash.exceptions import PreventUpdate

from dltrack.models import HyperParams, constants


def plug(app: Dash) -> None:
    """Plugin for rendering experiment results."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.PAGE_EXPERIMENT_ID, component_property="children"),
        Input(constants.STATE_EXPERIMENT_ID, component_property="data"),
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

    # put the hyperparameters in the sidebar
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.NAVBAR_ID, component_property="children"),
        Input(constants.STATE_HPARAMS, component_property="data"),
        Input(constants.STATE_EXPERIMENT_ID, component_property="data", allow_optional=True),
        prevent_initial_callback=True,
    )
    def render_hparams(hparams: list[str], experiment_id: int | None) -> dmc.Stack:
        if experiment_id is None:
            raise PreventUpdate
        hydrated = [HyperParams.model_validate_json(run) for run in hparams]
        keys = set(itertools.chain(*[list(k.hparams_dict.keys()) for k in hydrated]))
        return dmc.Stack(
            [
                dmc.Table(
                    [
                        dmc.TableThead(
                            dmc.TableTr(children=[dmc.TableTh("Run"), *[dmc.TableTh(k) for k in keys]])
                        ),
                        dmc.TableTbody(
                            [
                                dmc.TableTr(
                                    [
                                        dmc.TableTd(run.id),
                                        *[dmc.TableTd(str(run.hparams_dict[k])) for k in keys],
                                    ]
                                )
                                for run in hydrated
                            ]
                        ),
                        dmc.TableCaption("Hyperparameters"),
                    ],
                    striped=True,
                ),
            ],
        )
