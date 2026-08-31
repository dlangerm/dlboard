"""The experiment page for dltrack."""

import dash
import dash_mantine_components as dmc
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants
from dltrack.plugins.pages import experiment
from dltrack.serve import get_data_store


def layout(experiment_id: str) -> list[html.Div | dcc.Store]:
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    hparams = store.fetch_hyperparams(int(experiment_id))

    return [
        html.Div(
            id=experiment.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    dmc.Group(
                        [
                            html.Div(id=experiment.EXPERIMENT_HEADER_ID, style={"flex": 1}),
                            html.Div(id=experiment.NEW_PANEL_GROUP_ID),
                        ],
                        align="center",
                        wrap="nowrap",
                        gap="sm",
                    ),
                    # Not `dmc.Loader(id=...)` itself -- `render_initial` only ever replaces
                    # `children`, never the element itself, so a `Loader` used as its own
                    # placeholder would keep its spinner styling baked into the DOM node forever,
                    # showing through/behind whatever real content lands in it.
                    #
                    # `dcc.Loading` overlays a spinner over its children for as long as any
                    # callback updating one of their props is in flight -- panel/chart drag-reorder
                    # and other mutations rebuild this whole tree server-side, which can take a
                    # couple of seconds on a large experiment, and without this the page just
                    # looked frozen for that whole stretch.
                    dcc.Loading(html.Div(dmc.Loader(), id=experiment.METRIC_CONTENT_ID), delay_show=250),
                ],
                gap="xs",
            ),
        ),
        Store(id=experiment.STATE_PAGE_STORAGE),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=experiment.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
        # A completed panel/chart drag reports here (`_experiment_page_dragdrop.js`, via
        # `set_props`) -- lives in the static layout, not `accordion_view`'s render tree, so the
        # drop target always exists regardless of what's currently rendered underneath it.
        Store(id=experiment.PANEL_REORDER_STORE_ID),
        Store(id=experiment.CHART_REORDER_STORE_ID),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
