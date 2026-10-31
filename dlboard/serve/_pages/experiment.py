"""The experiment page for dlboard."""

import dash
import dash_mantine_components as dmc
from dash import html
from dash.dcc import Store
from dash.development.base_component import Component
from flask import request

from dlboard.serve import _constants as constants
from dlboard.serve import get_current_user, get_data_store
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _live_updates as live
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import panel_area, panel_area_stores, render_panel
from dlboard.serve._pages._experiment._notes import STATE_NOTES_REVISION, notes_button, notes_drawer
from dlboard.serve._pages._experiment._paging import SIZES_COOKIE, Paging
from dlboard.serve._pages._experiment._run_compare import CompareQuery, compare_components
from dlboard.serve._pages._experiment._runs_table import run_table_shells
from dlboard.serve._pages._experiment._views import resolve_view_id, view_controls
from dlboard.serve._pages._onboarding import first_run_snippet, onboarding_card

# Built once, at this module's import -- inside `build_app` -- so the env it reads is the app's (see tests/conftest.py).
_LIVE_UPDATE_SETTINGS = live.LiveUpdateSettings()


def _live_update_header(header: Component, notes: Component, views: Component) -> dmc.Group:
    """The experiment header, its notes and view picker, and the live-update status (`live.live_controls`)."""
    return dmc.Group(
        [
            html.Div(header, id=ids.EXPERIMENT_HEADER_ID),
            dmc.Group(
                [
                    notes,
                    views,
                    dmc.Divider(orientation="vertical"),
                    *live.live_controls(),
                ],
                gap="xs",
                wrap="nowrap",
            ),
        ],
        justify="space-between",
        wrap="nowrap",
    )


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
    """
    store = get_data_store()
    exp = store.get_experiment(int(experiment_id))
    if not exp:
        return [html.Div(f"Experiment {experiment_id} not found")]

    # Rendered synchronously, here, rather than by a `render_initial`-style callback fired after
    # this static shell mounts -- see `render_panel`'s own docstring for why that's the one thing
    # that actually eliminates the page's extra round trip, not just hides it behind a spinner.
    view_id = resolve_view_id(store, exp.id, view)
    container, header, page_json = render_panel(
        store,
        repo.ExperimentRef(exp.id, view_id),
        Paging.from_request(query, request.cookies.get(SIZES_COOKIE)),
        focus_chart=chart,
    )
    current_page = page_model.BasicExperimentPage.model_validate_json(page_json)
    is_owner = view_id is not None and current_page.owner_id == get_current_user().id

    return [
        html.Div(
            id=ids.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    _live_update_header(
                        header,
                        notes_button(len(store.list_comments(exp.id))),
                        view_controls(
                            store,
                            exp.id,
                            view_id,
                            is_owner=is_owner,
                            view_name=current_page.name,
                            is_shared=current_page.shared,
                        ),
                    ),
                    notes_drawer(),
                    *compare_components(store, exp.id, CompareQuery.from_query(query)),
                    # Nothing logged yet: show how to log the first run, right where its charts will go.
                    onboarding_card(
                        title="No runs yet — log your first one",
                        snippet=first_run_snippet(exp.project_id, exp.id),
                    )
                    if next(store.get_runs(exp.id, limit=1, run_ids=None), None) is None
                    else None,
                    *panel_area(container),
                ],
                gap="xs",
            ),
        ),
        *live.live_timers(_LIVE_UPDATE_SETTINGS, exp.revision),
        Store(id=ids.STATE_PAGE_STORAGE, data=page_json),
        Store(id=ids.STATE_VIEW_ID, data=view_id),
        Store(id=STATE_NOTES_REVISION, data=exp.notes_revision),
        Store(id=constants.STATE_PROJECT_ID, data=exp.project_id),
        Store(id=constants.STATE_EXPERIMENT_ID, data=int(experiment_id)),
        *panel_area_stores(),
        *run_table_shells(),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")
