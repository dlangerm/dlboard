"""The experiment page for dlboard."""

from typing import Any, cast

import dash
import dash_mantine_components as dmc
from dash import dcc, html
from dash.dcc import Store
from dash.development.base_component import Component
from flask import request

from dlboard.serve import _constants as constants
from dlboard.serve import get_current_user, get_data_store
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _live_updates as live
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment import _panel_controls as panel_controls
from dlboard.serve._pages._experiment import _tab_controls as tab_controls
from dlboard.serve._pages._experiment import render_panel
from dlboard.serve._pages._experiment._chart_editor import chart_editor_dock
from dlboard.serve._pages._experiment._chart_suggestions import chart_suggestion_shells
from dlboard.serve._pages._experiment._chart_view_state import chart_view_state_stores
from dlboard.serve._pages._experiment._notes import STATE_NOTES_REVISION, notes_button, notes_drawer
from dlboard.serve._pages._experiment._paging import SIZES_COOKIE, Paging
from dlboard.serve._pages._experiment._run_compare import CompareQuery, compare_components
from dlboard.serve._pages._experiment._runs_table import run_table_shells
from dlboard.serve._pages._experiment._views import resolve_view_id, view_controls
from dlboard.serve._pages._onboarding import first_run_snippet, onboarding_card

# Built once, at this module's import -- inside `build_app` -- so the env it reads is the app's (see tests/conftest.py).
_LIVE_UPDATE_SETTINGS = live.LiveUpdateSettings()

# One dcc.Store per drag-drop gesture a completed drag can report (`_experiment_page_dragdrop.js`,
# via `set_props`) -- listed in the static layout below, not `accordion_view`'s render tree, so the
# drop target always exists regardless of what's currently rendered underneath it.
_DRAG_DROP_STORE_IDS = [
    panel_controls.PANEL_REORDER_STORE_ID,
    panel_controls.CHART_REORDER_STORE_ID,
    tab_controls.TAB_DROP_STORE_ID,
    tab_controls.CHART_TAB_DROP_STORE_ID,
    panel_controls.CHART_PANEL_MOVE_STORE_ID,
]


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
                    # `container` (from `render_panel`, above) is real content from the very first
                    # response now, not a `dmc.Loader()` placeholder -- `dcc.Loading` still wraps it
                    # for every *later* full-panel rebuild (add/delete/rename a panel or chart,
                    # apply suggestions, ...), which still goes through a callback writing this
                    # same `children`. `custom_spinner` matches those rebuilds' spinner to the
                    # app's own `dmc.Loader()` rather than `dcc.Loading`'s unstyled default, so a
                    # rebuild doesn't visibly swap from one loading indicator to an
                    # unrelated-looking second one partway through.
                    #
                    # `target_components` is required now that live updates poll in the
                    # background: without it, `dcc.Loading` shows its overlay for *any* in-flight
                    # callback touching a descendant, which includes `poll_for_updates`'s per-chart
                    # patches (they live inside this subtree, at `CHART_CONTENT.of(...)`) -- a
                    # stop-the-world flash on every poll tick, exactly what per-chart patching
                    # exists to avoid. Scoping it to just this div's own `children` means the
                    # spinner only ever covers a genuine full-panel rebuild; the poll's targeted
                    # patches update their charts silently. Its generated type stub is an empty
                    # `TypedDict` (a known Dash codegen gap for this genuinely dynamic dict) --
                    # `cast` to `Any` rather than fight that, same as `_panel_view.py`'s
                    # `data-*`-attr casts.
                    # The panel area and the chart editor docked beside it, so the charts stay in view while one is
                    # edited (`_experiment_page_hover.css`). The dock is rendered here once, outside `METRIC_CONTENT_ID`:
                    # every structural edit rebuilds that.
                    html.Div(
                        [
                            html.Div(
                                cast("Any", dcc.Loading)(  # pyrefly: ignore [unknown-argument-type]
                                    html.Div(container, id=ids.METRIC_CONTENT_ID),
                                    delay_show=250,
                                    target_components={str(ids.METRIC_CONTENT_ID): "children"},
                                    custom_spinner=dmc.Loader(size="lg"),
                                ),
                                className="dl-experiment-main",
                            ),
                            chart_editor_dock(),
                        ],
                        className="dl-experiment-body",
                    ),
                    # Every modal the panel area can open, each owned by its feature module. Rendered
                    # once, outside `METRIC_CONTENT_ID`: every structural edit rebuilds that, and each re-mount
                    # of a shell set its confirm button's callback off again -- a burst of round trips that
                    # answered nothing but delayed the render they came with. So each confirm closes its own.
                    *chart_suggestion_shells(),
                    *panel_controls.panel_control_modals(),
                    *tab_controls.tab_control_modals(),
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
        *(Store(id=store_id) for store_id in _DRAG_DROP_STORE_IDS),
        *run_table_shells(),
        *chart_view_state_stores(),
    ]


dash.register_page(__name__, path_template="/experiment/<experiment_id>")
