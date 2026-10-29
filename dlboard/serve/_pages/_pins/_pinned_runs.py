"""
The project page's sidebar: the runs pinned in the project, from any of its experiments, one row to a run.

A run pinned in ten colours by three people is still one row, with all its pins as the one pin it is drawn as. The list is
paged (a project can have as many pinned runs as its people make) and filtered by colour, who pinned, experiment and name,
and the filters and the page are the page URL's query (`PinFilter`, `?pins_color=`, ...), so a link to "Alice's red pins in
this experiment" is the address bar. Each run's name goes to the run, and the bar above the list pins, unpins and deletes
the selected runs, as it does on the experiment page.
"""

from __future__ import annotations

import math
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import parse_qsl

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, ctx, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from dlboard.models import PinColor
from dlboard.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import DivId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import CallbackStates, section_label
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._experiment._focus import experiment_href
from dlboard.serve._pages._pins._pin_menus import pin_menus
from dlboard.serve._pages._pins._pin_view import pin_cells, pin_glyph
from dlboard.serve._pages._pins._row_pins import PINS_FIELD, pin_column_def, pin_or_unpin
from dlboard.serve._pages._run_list import (
    RunAction,
    RunActionRequest,
    RunListSurface,
    action_bar,
    register_run_list,
    request_store_id,
    simple_action,
)
from dlboard.serve._pages._run_list._columns import HREF_FIELD, name_column_def, run_fields, status_column_def
from dlboard.serve._pages._run_list._delete import (
    ask_to_delete,
    delete_outputs,
    delete_shells,
    register_delete,
)
from dlboard.serve._pages._run_list._rows import register_row_updates, row_update_shells, row_updates_id
from dlboard.serve._pages._run_list._table import run_grid

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dash.development.base_component import Component

    from dlboard.models import DataStore, PinnedRuns


class _PinnedRunsPage:
    """Page tag: marks a component id as belonging to the project page's pinned runs."""


PAGE_SIZE: typing.Final = 25
"""How many runs the list shows at once."""

PINNED_PROJECT_ID: StoreId[_PinnedRunsPage] = StoreId("pinned-runs-project")
"""The project whose pinned runs the sidebar lists: in the project page's layout, which the sidebar is drawn for."""
PINNED_GRID_ID: ValueId[_PinnedRunsPage] = ValueId("pinned-runs-grid")
PINNED_COLORS_ID: ValueId[_PinnedRunsPage] = ValueId("pinned-runs-colors")
PINNED_BY_ID: ValueId[_PinnedRunsPage] = ValueId("pinned-runs-by")
PINNED_EXPERIMENT_ID: ValueId[_PinnedRunsPage] = ValueId("pinned-runs-experiment")
PINNED_SEARCH_ID: ValueId[_PinnedRunsPage] = ValueId("pinned-runs-search")
PINNED_PAGER_ID: ValueId[_PinnedRunsPage] = ValueId("pinned-runs-pager")
PINNED_LIST_ID: DivId[_PinnedRunsPage] = DivId("pinned-runs-list")
PINNED_COUNT_ID: DivId[_PinnedRunsPage] = DivId("pinned-runs-count")
PINNED_NOTIFY_ID: DivId[_PinnedRunsPage] = DivId("pinned-runs-notify")
"""Where the project page says why a delete was refused."""

EXPERIMENT_FIELD: typing.Final = "_experiment"
"""The row field holding the name of the run's experiment (reserved like `_swatch`)."""

_SYNC_URL_JS = ClientsideScript(Path(__file__).with_name("sync_pins_url.js"))


def _split(value: object) -> object:
    """A comma-separated query value as its items; anything else is left for validation to judge."""
    return [item for item in value.split(",") if item] if isinstance(value, str) else value


class PinFilter(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """Which runs the list shows and which page of them: the project page URL's `pins_*` query."""

    colors: Annotated[frozenset[PinColor], BeforeValidator(_split)] = Field(
        default=frozenset(), alias="pins_color"
    )
    pinned_by: Annotated[frozenset[int], BeforeValidator(_split)] = Field(
        default=frozenset(), alias="pins_by"
    )
    experiment: int | None = Field(default=None, alias="pins_exp")
    q: str = Field(default="", alias="pins_q")
    page: int = Field(default=1, ge=1, alias="pins_page")

    @classmethod
    def from_query(cls, query: Mapping[str, str]) -> PinFilter:
        """The filter a page URL's query describes; a malformed one just means no filter."""
        try:
            return cls.model_validate(query)
        except ValidationError:
            return cls()

    @classmethod
    def from_search(cls, search: str | None) -> PinFilter:
        """The filter the `?...` of a page URL describes."""
        return cls.from_query(dict(parse_qsl((search or "").lstrip("?"))))

    @property
    def is_set(self) -> bool:
        """Whether any filter narrows the list (the page does not)."""
        return bool(self.colors or self.pinned_by or self.experiment is not None or self.q.strip())


def _fetch(store: DataStore[...], project_id: int, flt: PinFilter) -> tuple[PinnedRuns, int]:
    """The page of runs `flt` asks for (the last one, if it asks past the end), and how many pages there are."""

    def page_of(number: int) -> PinnedRuns:
        return store.list_pinned_runs(
            project_id,
            colors=flt.colors or None,
            pinned_by=flt.pinned_by or None,
            experiment_id=flt.experiment,
            name_contains=flt.q,
            limit=PAGE_SIZE,
            offset=(number - 1) * PAGE_SIZE,
        )

    found = page_of(flt.page)
    pages = max(math.ceil(found.total / PAGE_SIZE), 1)
    return (found, pages) if flt.page <= pages else (page_of(pages), pages)


def _rows(found: PinnedRuns, me: int) -> list[dict[str, Any]]:
    return [
        {
            **run_fields(pinned.run),
            HREF_FIELD: experiment_href(pinned.experiment.id, run_id=pinned.run.id),
            EXPERIMENT_FIELD: experiment_display_name(pinned.experiment),
            PINS_FIELD: [cell.model_dump(mode="json") for cell in pin_cells(pinned.pins, me)],
        }
        for pinned in found.runs
    ]


def _count(found: PinnedRuns) -> str:
    return f"{found.total} run{'s' if found.total != 1 else ''}"


def pinned_runs_list(found: PinnedRuns, me: int, flt: PinFilter) -> Component:
    """The runs of `found` as a list, or what to do if there are none."""
    if not found.runs:
        return dmc.Text(
            "No pinned run matches these filters."
            if flt.is_set
            else "No runs are pinned yet. Pin a run from its experiment's run list.",
            size="sm",
            c="dimmed",
        )
    return run_grid(
        PINNED_GRID_ID,
        [
            name_column_def(swatch=False),
            pin_column_def(),
            status_column_def(),
        ],
        _rows(found, me),
        (),
        swatches=False,
    )


def _pager_props(pages: int, page: int) -> dict[str, Any]:
    """The pager's props for `pages` pages with `page` showing: it is only there to use when there is more than one."""
    return {"total": pages, "value": min(page, pages), "style": {"display": "flex" if pages > 1 else "none"}}


def render_pinned_runs(store: DataStore[...], project_id: int, flt: PinFilter) -> Component:
    """The project page's sidebar, showing the pinned runs `flt` asks for."""
    found, pages = _fetch(store, project_id, flt)
    me = get_current_user().id
    counts = store.pin_counts(project_id)
    # Who has pinned in this project, which is who the filter can usefully name: not every user the viewer can see.
    pinners = sorted(
        store.list_pinners(project_id), key=lambda pinner: (pinner.user_id != me, pinner.username.lower())
    )
    return html.Div(
        [
            dmc.Group(
                [
                    section_label("Pinned runs"),
                    dmc.Text(_count(found), size="xs", c="dimmed", id=PINNED_COUNT_ID),
                ],
                gap="xs",
            ),
            dmc.ChipGroup(
                dmc.Group(
                    [
                        dmc.Chip(
                            pin_glyph(color),
                            value=color.value,
                            size="xs",
                            variant="outline",
                            **typing.cast("dict[str, Any]", {"aria-label": f"Only {color.value} pins"}),
                        )
                        for color in PinColor
                        if color in counts
                    ],
                    gap=4,
                ),
                id=PINNED_COLORS_ID,
                multiple=True,
                value=[color.value for color in PinColor if color in flt.colors],
            ),
            html.Details(
                [
                    html.Summary("More filters", className="dl-run-list-summary"),
                    dmc.Stack(
                        [
                            dmc.MultiSelect(
                                id=PINNED_BY_ID,
                                data=[
                                    {
                                        "value": str(pinner.user_id),
                                        "label": f"{pinner.username} (you)"
                                        if pinner.user_id == me
                                        else pinner.username,
                                    }
                                    for pinner in pinners
                                ],
                                value=[
                                    str(pinner.user_id)
                                    for pinner in pinners
                                    if pinner.user_id in flt.pinned_by
                                ],
                                placeholder="Pinned by anyone",
                                clearable=True,
                                searchable=True,
                                size="xs",
                                **typing.cast("dict[str, Any]", {"aria-label": "Pinned by"}),
                            ),
                            dmc.Select(
                                id=PINNED_EXPERIMENT_ID,
                                data=[
                                    {"value": str(e.id), "label": experiment_display_name(e)}
                                    for e in store.get_experiments(project_id)
                                ],
                                value=None if flt.experiment is None else str(flt.experiment),
                                placeholder="Any experiment",
                                clearable=True,
                                searchable=True,
                                size="xs",
                                **typing.cast("dict[str, Any]", {"aria-label": "Experiment"}),
                            ),
                            dmc.TextInput(
                                id=PINNED_SEARCH_ID,
                                value=flt.q,
                                placeholder="Search runs",
                                leftSection=icon(Icon.SEARCH),
                                debounce=300,
                                size="xs",
                                **typing.cast("dict[str, Any]", {"aria-label": "Search pinned runs"}),
                            ),
                        ],
                        gap="xs",
                        mt="xs",
                    ),
                ],
                open=bool(flt.pinned_by or flt.experiment is not None or flt.q),
            ),
            action_bar(
                RunListSurface.PROJECT,
                *pin_menus(RunListSurface.PROJECT),
                simple_action(
                    RunListSurface.PROJECT, RunAction.DELETE, "Delete", icon_name=Icon.DELETE, color="red"
                ),
            ),
            html.Div(pinned_runs_list(found, me, flt), id=PINNED_LIST_ID),
            dmc.Pagination(
                id=PINNED_PAGER_ID, withEdges=False, size="xs", mt="xs", **_pager_props(pages, flt.page)
            ),
        ],
        className="dl-pinned-runs",
    )


def pinned_runs_shells(project_id: int) -> list[Component]:
    """What the sidebar needs in the project page itself: the project, and the stores and the modal its actions use."""
    return [
        Store(id=PINNED_PROJECT_ID, data=project_id),
        Store(id=request_store_id(RunListSurface.PROJECT)),
        *row_update_shells(RunListSurface.PROJECT),
        *delete_shells(RunListSurface.PROJECT),
        dmc.NotificationContainer(id=PINNED_NOTIFY_ID),
    ]


class _Redraw(CallbackStates):
    """What redraws the list: a filter or the page changing."""

    colors: Annotated[list[PinColor], Input(PINNED_COLORS_ID, "value", allow_optional=True)]
    pinned_by: Annotated[list[int], Input(PINNED_BY_ID, "value", allow_optional=True)]
    experiment: Annotated[int | None, Input(PINNED_EXPERIMENT_ID, "value", allow_optional=True)]
    q: Annotated[str | None, Input(PINNED_SEARCH_ID, "value", allow_optional=True)]
    page: Annotated[int | None, Input(PINNED_PAGER_ID, "value", allow_optional=True)]
    project_id: Annotated[int | None, State(PINNED_PROJECT_ID, "data", allow_optional=True)]


class _Act(CallbackStates):
    """What `act_on_pinned_runs` reads: the request."""

    request: Annotated[
        dict[str, Any] | None, Input(request_store_id(RunListSurface.PROJECT), "data", allow_optional=True)
    ]


def register(app: Dash) -> None:
    """Draw the sidebar for the project page, redraw its list as a filter or the page changes, and act on the selected runs."""
    register_run_list(app, RunListSurface.PROJECT, PINNED_GRID_ID)
    register_row_updates(app, RunListSurface.PROJECT)

    @app.callback(
        Output(constants.NAVBAR_RUN_LIST_ID, "children", allow_duplicate=True),
        Input(PINNED_PROJECT_ID, "data", allow_optional=True),
        State(constants.LOCATION_ID, "search"),
        prevent_initial_call=True,
    )
    def draw_pinned_runs(project_id: int | None, search: str | None) -> Component:
        if project_id is None:
            raise PreventUpdate
        return render_pinned_runs(get_data_store(), project_id, PinFilter.from_search(search))

    @app.callback(
        Output(PINNED_LIST_ID, "children"),
        Output(PINNED_COUNT_ID, "children"),
        Output(PINNED_PAGER_ID, "total"),
        Output(PINNED_PAGER_ID, "value"),
        Output(PINNED_PAGER_ID, "style"),
        _Redraw.states(),
        prevent_initial_call=True,
    )
    def redraw_pinned_runs(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[Component, str, int, int, dict[str, Any]]:
        shown = _Redraw.model_validate(states)
        if shown.project_id is None:
            raise PreventUpdate
        # A new filter starts again from the first page; only the pager itself keeps its place.
        flt = PinFilter(
            colors=frozenset(shown.colors),
            pinned_by=frozenset(shown.pinned_by),
            experiment=shown.experiment,
            q=(shown.q or "").strip(),
            page=(shown.page or 1) if ctx.triggered_id == PINNED_PAGER_ID else 1,
        )
        found, pages = _fetch(get_data_store(), shown.project_id, flt)
        props = _pager_props(pages, flt.page)
        return (
            pinned_runs_list(found, get_current_user().id, flt),
            _count(found),
            props["total"],
            props["value"],
            props["style"],
        )

    app.clientside_callback(
        _SYNC_URL_JS.source,
        Output(PINNED_COUNT_ID, "style"),
        Input(PINNED_COLORS_ID, "value", allow_optional=True),
        Input(PINNED_BY_ID, "value", allow_optional=True),
        Input(PINNED_EXPERIMENT_ID, "value", allow_optional=True),
        Input(PINNED_SEARCH_ID, "value", allow_optional=True),
        Input(PINNED_PAGER_ID, "value", allow_optional=True),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(row_updates_id(RunListSurface.PROJECT), "data", allow_duplicate=True),
        Output(PINNED_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        *delete_outputs(RunListSurface.PROJECT),
        _Act.states(),
        prevent_initial_call=True,
    )
    def act_on_pinned_runs(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[Any, ...]:
        act = _Act.model_validate(states)
        if act.request is None:
            raise PreventUpdate
        request = RunActionRequest.model_validate(act.request)
        match request.action:
            case RunAction.PIN | RunAction.UNPIN:
                pinned = pin_or_unpin(request, PINNED_GRID_ID)
                return pinned.rows, pinned.notice, no_update, no_update, no_update
            case RunAction.DELETE:
                asked = ask_to_delete(request.run_ids)
                return no_update, no_update, asked.opened, asked.body, asked.pending
            case RunAction.DRAW | RunAction.HIDE | RunAction.DIFF | RunAction.REMOVE:
                # The project page draws nothing and holds no comparison, so its list offers none of these.
                raise PreventUpdate

    register_delete(app, RunListSurface.PROJECT, notify=PINNED_NOTIFY_ID)
