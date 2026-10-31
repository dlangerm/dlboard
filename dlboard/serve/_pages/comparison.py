"""The comparison page for dlboard."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import dash
import dash_mantine_components as dmc
from dash import html
from dash.dcc import Store
from flask import request

from dlboard.serve import _constants as constants
from dlboard.serve import get_data_store
from dlboard.serve._pages._comparison import _ids as ids
from dlboard.serve._pages._comparison._add_runs import add_runs_modal
from dlboard.serve._pages._comparison._header import comparison_header
from dlboard.serve._pages._comparison._members import members_shells
from dlboard.serve._pages._experiment import _ids as experiment_ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import panel_area, panel_area_stores
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.serve._pages._experiment._paging import SIZES_COOKIE, Paging
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID

if TYPE_CHECKING:
    from dash.development.base_component import Component


def layout(comparison_id: str, chart: str | None = None, **query: str) -> list[Component]:
    """
    The comparison page, with its URL's query parameters passed in as keyword arguments by Dash.

    `?chart=` (a `ChartInstance.id`, from a copied chart link) opens that chart's panel and tab; `?panels_q=`,
    `?panels_page=` and `?charts=` say which page of panels and charts is showing and `?hidden=` which runs are left out of
    them (see `Paging`). Any other query parameter is ignored. Its runs are listed in the sidebar, as an experiment's are.
    """
    store = get_data_store()
    found = store.get_view(BasicExperimentPage, int(comparison_id))
    # An experiment's view is a view too, but it has no project: this page is for the pages that do.
    if found is None or found.project_id is None:
        return [html.Div(f"Comparison {comparison_id} not found")]
    page, project_id = cast("BasicExperimentPage", found), found.project_id
    paging = Paging.from_request(query, request.cookies.get(SIZES_COOKIE))
    container = edits.render_panel_area(store, page, paging, focus_chart=chart)
    return [
        html.Div(
            id=experiment_ids.PAGE_EXPERIMENT_ID,
            children=dmc.Stack(
                [
                    html.Div(comparison_header(store, page)),
                    *(
                        [add_runs_modal(store, project_id)]
                        if store.can_change_comparison_runs(page.id)
                        else []
                    ),
                    *panel_area(container),
                    dmc.NotificationContainer(id=VIEW_NOTIFY_ID),
                ],
                gap="xs",
            ),
        ),
        Store(id=experiment_ids.STATE_PAGE_STORAGE, data=page.model_dump_json()),
        Store(id=constants.STATE_PROJECT_ID, data=project_id),
        Store(id=ids.COMPARISON_ID_STORE, data=page.id),
        *members_shells(),
        *panel_area_stores(),
    ]


dash.register_page(__name__, path_template="/comparison/<comparison_id>")
