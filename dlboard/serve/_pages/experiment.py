"""The experiment page for dlboard."""

import dash
import dash_mantine_components as dmc
from dash import html
from dash.dcc import Store
from dash.development.base_component import Component
from flask import request

from dlboard.serve import _constants as constants
from dlboard.serve import get_data_store
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _live_updates as live
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._header import experiment_header
from dlboard.serve._pages._experiment._notes import STATE_NOTES_REVISION, notes_drawer
from dlboard.serve._pages._experiment._paging import SIZES_COOKIE, Paging
from dlboard.serve._pages._experiment._panel_area import panel_area, panel_area_stores
from dlboard.serve._pages._experiment._run_compare import CompareQuery, compare_components
from dlboard.serve._pages._experiment._runs_table import run_table_shells
from dlboard.serve._pages._experiment._views import resolve_view_id
from dlboard.serve._pages._onboarding import first_run_snippet, onboarding_card

# Built once, at this module's import -- inside `build_app` -- so the env it reads is the app's (see tests/conftest.py).
_LIVE_UPDATE_SETTINGS = live.LiveUpdateSettings()


def layout(
    experiment_id: str, chart: str | None = None, view: str | None = None, **query: str
) -> list[Component]:
    """
    The experiment page, with its URL's query parameters passed in as keyword arguments by Dash.

    `?view=` (a `Page.id`) shows one of the named views instead of the shared page (see `_views.py`).
    `?chart=` (a `ChartInstance.id`, from a copied chart link) opens that chart's panel and tab, and
    `chart_deep_link.js` scrolls to it. `?compare=`, `?compare_mode=` and `?compare_q=` open the
    run-compare modal on that state (see `CompareQuery`). `?panels_q=`, `?panels_page=` and
    `?charts=` say which page of panels and charts is showing (see `Paging`). Any other query parameter is ignored.

    Everything is drawn here, in the page's first response, rather than by a callback fired once this
    shell mounts: that would cost every load a second round trip, not just hide it behind a spinner.
    """
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]
    view_id = resolve_view_id(store, exp.id, view)
    page = repo.load_page(store, repo.ExperimentRef(exp.id, view_id))
    paging = Paging.from_request(query, request.cookies.get(SIZES_COOKIE))

    return [
        html.Div(
            id=ids.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    experiment_header(store, exp, page, view_id),
                    notes_drawer(),
                    *compare_components(store, page, CompareQuery.from_query(query)),
                    # Nothing logged yet: show how to log the first run, right where its charts will go.
                    onboarding_card(
                        title="No runs yet — log your first one",
                        snippet=first_run_snippet(exp.project_id, exp.id),
                    )
                    if next(store.get_runs(exp.id, limit=1, run_ids=None), None) is None
                    else None,
                    *panel_area(edits.render_panel_area(store, page, paging, focus_chart=chart)),
                ],
                gap="xs",
            ),
        ),
        *live.live_timers(_LIVE_UPDATE_SETTINGS, live.page_revision([exp])),
        Store(id=ids.STATE_PAGE_STORAGE, data=page.model_dump_json()),
        Store(id=ids.STATE_VIEW_ID, data=view_id),
        Store(id=STATE_NOTES_REVISION, data=exp.notes_revision),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=exp.id),
        *panel_area_stores(),
        *run_table_shells(),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")
