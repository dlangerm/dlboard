"""The experiment page for dltrack."""

import dash
import dash_mantine_components as dmc
from dash import dcc, html
from dash.dcc import Store

from dltrack.models import constants
from dltrack.serve import get_data_store
from dltrack.serve._pages._experiment import _experiment_page_state as core
from dltrack.serve._pages._experiment import render_panel

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
    # Rendered synchronously, here, rather than by a `render_initial`-style callback fired after
    # this static shell mounts -- see `render_panel`'s own docstring for why that's the one thing
    # that actually eliminates the page's extra round trip, not just hides it behind a spinner.
    container, header, page_json = render_panel(store, int(experiment_id))

    return [
        html.Div(
            id=core.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    html.Div(header, id=core.EXPERIMENT_HEADER_ID),
                    # `container` (from `render_panel`, above) is real content from the very first
                    # response now, not a `dmc.Loader()` placeholder -- `dcc.Loading` still wraps it
                    # for every *later* full-panel rebuild (add/delete/rename a panel or chart,
                    # apply suggestions, ...), which still goes through a callback writing this
                    # same `children`. `custom_spinner` matches those rebuilds' spinner to the
                    # app's own `dmc.Loader()` rather than `dcc.Loading`'s unstyled default, so a
                    # rebuild doesn't visibly swap from one loading indicator to an
                    # unrelated-looking second one partway through.
                    dcc.Loading(
                        html.Div(container, id=core.METRIC_CONTENT_ID),
                        delay_show=250,
                        custom_spinner=dmc.Loader(size="lg"),
                    ),
                ],
                gap="xs",
            ),
        ),
        Store(id=core.STATE_PAGE_STORAGE, data=page_json),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        Store(id=core.STATE_HPARAMS, data=[h.model_dump_json() for h in hparams]),
        *(Store(id=store_id) for store_id in _DRAG_DROP_STORE_IDS),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")  # pyright: ignore[reportUnknownMemberType]
