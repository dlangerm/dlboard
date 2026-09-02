"""The experiment page for dltrack."""

import dash
import dash_mantine_components as dmc
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants
from dltrack.plugins.pages.experiment import _experiment_page_state as core
from dltrack.serve import get_data_store

# One dcc.Store per drag-drop gesture a completed drag can report (`_experiment_page_dragdrop.js`,
# via `set_props`) -- listed in the static layout below, not `accordion_view`'s render tree, so the
# drop target always exists regardless of what's currently rendered underneath it.
_DRAG_DROP_STORE_IDS = [
    core.PANEL_REORDER_STORE_ID,
    core.CHART_REORDER_STORE_ID,
    core.TAB_DROP_STORE_ID,
    core.CHART_TAB_DROP_STORE_ID,
    core.CHART_PANEL_MOVE_STORE_ID,
]


def layout(experiment_id: str) -> list[html.Div | dcc.Store]:
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    hparams = store.fetch_hyperparams(int(experiment_id))

    return [
        html.Div(
            id=core.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    html.Div(id=core.EXPERIMENT_HEADER_ID),
                    # Not `dmc.Loader(id=...)` itself -- `render_initial` only ever replaces
                    # `children`, so a `Loader` used as its own placeholder would keep its spinner
                    # styling baked into the DOM node forever. `dcc.Loading` overlays a spinner
                    # instead, for as long as a callback updating these children is in flight.
                    dcc.Loading(html.Div(dmc.Loader(), id=core.METRIC_CONTENT_ID), delay_show=250),
                ],
                gap="xs",
            ),
        ),
        Store(id=core.STATE_PAGE_STORAGE),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=core.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
        *(Store(id=store_id) for store_id in _DRAG_DROP_STORE_IDS),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
