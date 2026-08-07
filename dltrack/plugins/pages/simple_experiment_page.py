"""An experiment page that displays hyperparameters with selectable runs, and every metric in a data table."""

from __future__ import annotations

import itertools
from typing import Any

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
from dash import ALL, Dash, Input, Output, State, ctx
from dash.exceptions import PreventUpdate

from dltrack.models import HyperParams, constants
from dltrack.plugins.common.metric_chart import BasicExperimentPage, basic_metric_table
from dltrack.plugins.utilities import get_data_store


def _run_toggle_id(run_id: int) -> dict[str, Any]:
    return {"type": "run-toggle", "run": run_id}


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

    # put the hyperparameters + run selection in the sidebar
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

        store = get_data_store()
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        excluded = set(page.page_settings.get(constants.EXCLUDED_RUNS_KEY) or [])

        return dmc.Stack(
            [
                dmc.Table(
                    [
                        dmc.TableThead(
                            dmc.TableTr(
                                children=[
                                    dmc.TableTh(""),
                                    dmc.TableTh("Run"),
                                    *[dmc.TableTh(k) for k in keys],
                                ]
                            )
                        ),
                        dmc.TableTbody(
                            [
                                dmc.TableTr(
                                    [
                                        dmc.TableTd(
                                            dmc.Checkbox(
                                                id=_run_toggle_id(run.id),
                                                checked=run.id not in excluded,
                                                size="xs",
                                            )
                                        ),
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

    # toggling a run's checkbox updates page_settings and re-renders the charts
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.METRIC_CONTENT_ID, component_property="children", allow_duplicate=True),
        Input({"type": "run-toggle", "run": ALL}, "checked"),
        State(constants.STATE_EXPERIMENT_ID, component_property="data"),
        prevent_initial_call=True,
    )
    def toggle_run_exclusion(_checked_values: list[bool], experiment_id: int) -> dmc.Container:
        triggered_id = ctx.triggered_id
        if not triggered_id or ctx.triggered[0]["value"] is None:
            raise PreventUpdate

        run_id = triggered_id["run"]
        included = ctx.triggered[0]["value"]

        store = get_data_store()
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        excluded = set(page.page_settings.get(constants.EXCLUDED_RUNS_KEY) or [])
        if included:
            excluded.discard(run_id)
        else:
            excluded.add(run_id)

        new_settings = {**page.page_settings, constants.EXCLUDED_RUNS_KEY: sorted(excluded)}
        page = page.model_copy(update={"page_settings": new_settings})
        store.update_page(page)

        store = get_data_store()
        return basic_metric_table(store, experiment_id=experiment_id)
