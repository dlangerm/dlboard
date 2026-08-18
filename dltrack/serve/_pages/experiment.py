"""The experiment page for dltrack."""

import dash
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants
from dltrack.plugins.utilities._data_store import get_data_store


def layout(experiment_id: str) -> list[html.Div | dcc.Store]:

    # retrieve the project id from the experiment id and store it in a dcc.Store component
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    hparams = store.fetch_hyperparams(int(experiment_id))

    return [
        html.Div(id=constants.PAGE_EXPERIMENT_ID),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=constants.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
