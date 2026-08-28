"""The experiment page for dltrack."""

import dash
import dash_mantine_components as dmc
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants
from dltrack.serve import get_data_store


def layout(experiment_id: str) -> list[html.Div | dcc.Store]:
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    hparams = store.fetch_hyperparams(int(experiment_id))

    return [
        html.Div(
            id=constants.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    dmc.Group(
                        [
                            html.Div(id=constants.EXPERIMENT_HEADER_ID, style={"flex": 1}),
                            html.Div(id=constants.EXPERIMENT_HEADER_ACTIONS_ID),
                        ],
                        align="center",
                        wrap="nowrap",
                        gap="sm",
                    ),
                    # Not `dmc.Loader(id=...)` itself -- `render_initial` only ever replaces
                    # `children`, never the element itself, so a `Loader` used as its own
                    # placeholder would keep its spinner styling baked into the DOM node forever,
                    # showing through/behind whatever real content lands in it.
                    html.Div(dmc.Loader(), id=constants.METRIC_CONTENT_ID),
                ],
                gap="xs",
            ),
        ),
        Store(id=constants.STATE_PAGE_STORAGE),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=constants.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
