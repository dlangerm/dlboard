"""
The comparison's runs, as the run list in the sidebar: whose each is, which are drawn, and the actions on the selected ones.

Which runs are drawn is the viewer's choice (`Paging.hidden`), like the tab they are on: hiding one saves nothing, so it never
forks a shared comparison for the people looking at it together. Taking a run out is a change to the comparison itself, which
only its owner and, if it is shared, a project's editors can make (`can_change_comparison_runs`), so everyone else is not
offered it. Either way the page is drawn again without saving it (`_page_edits.refresh`).
"""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import dash_mantine_components as dmc
import pendulum
from dash import Dash, Input, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate

from dlboard import models
from dlboard.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._grid import column_def, infer_column_dtype
from dlboard.serve._pages._comparison import _ids as ids
from dlboard.serve._pages._dash_helpers import CallbackStates, register_navigate, section_label
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._experiment import _ids as experiment_ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment._compare import compare, compare_menu, compare_pins_menu
from dlboard.serve._pages._experiment._focus import experiment_href
from dlboard.serve._pages._experiment._paging import Paging
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID
from dlboard.serve._pages._pins._pin_filter import pin_filter, register_pin_filter
from dlboard.serve._pages._pins._pin_menus import pin_menus
from dlboard.serve._pages._pins._row_pins import PINS_FIELD, pin_cells_by_run, pin_column_def, pin_or_unpin
from dlboard.serve._pages._run_list import (
    RunAction,
    RunActionRequest,
    RunListSurface,
    action_bar,
    register_run_list,
    request_store_id,
    simple_action,
)
from dlboard.serve._pages._run_list._columns import (
    DRAWN_FIELD,
    HREF_FIELD,
    eye_column_def,
    name_column_def,
    run_fields,
    status_column_def,
)
from dlboard.serve._pages._run_list._ids import navigate_id
from dlboard.serve._pages._run_list._rows import register_row_updates, row_update_shells, row_updates_id
from dlboard.serve._pages._run_list._table import run_grid

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dash.development.base_component import Component

    from dlboard.models import DataStore, Run
    from dlboard.serve._pages._pins._pin_view import PinCell

EXPERIMENT_FIELD: typing.Final = "_experiment"
"""The row field holding the name of the run's experiment (reserved like `_swatch`)."""

_FOLLOW_FORK_JS = ClientsideScript(Path(__file__).with_name("follow_fork.js"))


def notice(title: str, message: str, *, color: str = "green") -> list[dict[str, Any]]:
    """A notification for the page's notifications (`VIEW_NOTIFY_ID`)."""
    return [
        {
            "action": "show",
            "id": f"comparison-{title}-{message}",
            "title": title,
            "message": message,
            "color": color,
            "autoClose": 5_000,
        }
    ]


def add_runs_button() -> Component:
    """The button above the list of runs that opens the modal to add runs in."""
    return dmc.Button(
        "Add runs",
        id=ids.ADD_RUNS_OPEN_ID,
        n_clicks=0,
        leftSection=icon(Icon.ADD),
        size="compact-xs",
        variant="light",
    )


def membership_changed(page_json: str, paging_json: str) -> tuple[Component, str, int]:
    """The page after a change to its runs: the panel area and the page's JSON drawn again, saving nothing, and when."""
    page, container = edits.refresh(page_json, paging_json)
    return container, page.model_dump_json(), pendulum.now("UTC").int_timestamp * 1000


def _rows(
    store: DataStore[...],
    runs: list[Run],
    page: page_model.BasicExperimentPage,
    hidden: frozenset[int],
    pins: Mapping[int, list[PinCell]],
) -> list[dict[str, Any]]:
    """
    A row for each run, with the hyperparameters the page's Columns setting chose.

    Across experiments the config is often what tells one run of a name from another.
    """
    experiments = {
        experiment.id: experiment
        for experiment_id in {run.experiment_id for run in runs}
        if (experiment := store.get_experiment(experiment_id)) is not None
    }
    columns = page.document.hparam_columns
    by_experiment: dict[int, set[int]] = {}
    for run in runs:
        by_experiment.setdefault(run.experiment_id, set()).add(run.id)
    hparams = {
        h.run_id: h.hparams_dict
        for experiment_id, run_ids in by_experiment.items()
        for h in (store.fetch_hyperparams(experiment_id, run_ids=frozenset(run_ids)) if columns else [])
    }
    # A column is a hyperparameter or the latest value of a metric, whichever the experiments it is of logged under its name.
    latest: dict[int, dict[str, float]] = {}
    for experiment_id, run_ids in by_experiment.items():
        metric_columns = frozenset(columns) & frozenset(store.summarize_metric_keys(experiment_id))
        if metric_columns:
            latest |= store.fetch_metrics(
                experiment_id,
                keys=metric_columns,
                reduction=models.MetricReduction.LATEST,
                run_ids=frozenset(run_ids),
            ).latest_per_run()
    values = {run.id: hparams.get(run.id, {}) | latest.get(run.id, {}) for run in runs}
    return [
        {
            **run_fields(run),
            DRAWN_FIELD: run.id not in hidden,
            HREF_FIELD: experiment_href(run.experiment_id, run_id=run.id),
            EXPERIMENT_FIELD: experiment_display_name(experiments[run.experiment_id]),
            PINS_FIELD: [cell.model_dump(mode="json") for cell in pins.get(run.id, [])],
            **{column: values[run.id].get(column) for column in columns},
        }
        for run in runs
        if run.experiment_id in experiments
    ]


def _signature(rows: list[dict[str, Any]], offered: object, *, can_edit: bool) -> str:
    """A digest of everything the list is drawn from, so a tab switch does not draw it again."""
    return hashlib.sha256(
        json.dumps([rows, offered, can_edit], sort_keys=True, default=str).encode()
    ).hexdigest()


def render_members(
    store: DataStore[...],
    page: page_model.BasicExperimentPage,
    hidden: frozenset[int],
    picked: frozenset[int],
    *,
    prev_signature: str | None,
) -> Component | None:
    """
    The sidebar of the comparison `page`, or `None` if it is as it was drawn (`prev_signature`).

    `picked` are the runs selected in the list, which stay so when it is drawn again.
    """
    runs = store.comparison_runs(page.id)
    pins = pin_cells_by_run(store, [run.id for run in runs], get_current_user().id)
    can_edit = store.can_change_comparison_runs(page.id)
    project_id = typing.cast("int", page.project_id)
    addable = store.list_comparisons(project_id, get_current_user().id, editable=True)
    counts = store.pin_counts(project_id)
    rows = _rows(store, runs, page, hidden, pins)
    signature = _signature(
        rows,
        [[[c.id, c.name] for c in addable], sorted((color.value, n) for color, n in counts.items())],
        can_edit=can_edit,
    )
    if signature == prev_signature:
        return None
    columns = page.document.hparam_columns
    actions = [
        simple_action(RunListSurface.COMPARISON, RunAction.DRAW, "Show", icon_name=Icon.EYE),
        simple_action(RunListSurface.COMPARISON, RunAction.HIDE, "Hide", icon_name=Icon.EYE_OFF),
        *pin_menus(RunListSurface.COMPARISON),
        compare_menu(RunListSurface.COMPARISON, addable, current=page.id, here=can_edit),
        *(
            [simple_action(RunListSurface.COMPARISON, RunAction.REMOVE, "Remove", icon_name=Icon.CLOSE)]
            if can_edit
            else []
        ),
    ]
    return html.Div(
        [
            dmc.Group(
                [
                    dmc.Group(
                        [
                            section_label("Runs"),
                            dmc.Text(
                                f"{len(rows)} · {len([r for r in rows if not r[DRAWN_FIELD]])} hidden",
                                size="xs",
                                c="dimmed",
                            ),
                        ],
                        gap="xs",
                    ),
                    *([add_runs_button()] if can_edit else []),
                ],
                justify="space-between",
            ),
            compare_pins_menu(RunListSurface.COMPARISON, counts, here=can_edit),
            action_bar(RunListSurface.COMPARISON, *actions),
            pin_filter(RunListSurface.COMPARISON, {cell.color for cells in pins.values() for cell in cells}),
            run_grid(
                ids.COMPARISON_GRID_ID,
                [
                    eye_column_def(),
                    name_column_def(swatch=True),
                    pin_column_def(),
                    status_column_def(),
                    *(column_def(key, infer_column_dtype(rows, key)) for key in columns),
                ],
                rows,
                picked,
                swatches=True,
            )
            if rows
            else dmc.Text(
                "No runs yet. Add some from the pinned runs of the project, or from an experiment.",
                size="sm",
                c="dimmed",
            ),
            Store(id=ids.MEMBERS_SIGNATURE_ID, data=signature),
        ],
        className="dl-pinned-runs",
    )


def members_shells() -> list[Component]:
    """What the list needs in the page itself, not in the list: its request store, and the stores its actions write."""
    return [
        Store(id=request_store_id(RunListSurface.COMPARISON)),
        Store(id=navigate_id(RunListSurface.COMPARISON)),
        Store(id=ids.MEMBERS_REVISION_ID),
        *row_update_shells(RunListSurface.COMPARISON),
    ]


class _Draw(CallbackStates):
    """
    What draws the list: the comparison the page is of, and when its runs or what is hidden last changed.

    Only ids a comparison page alone has are listened to: the page and paging stores are the experiment page's too, and a
    callback listening to them would be called there, to draw a comparison's runs into an experiment's sidebar.
    """

    comparison: Annotated[int | None, Input(ids.COMPARISON_ID_STORE, "data", allow_optional=True)]
    revision: Annotated[int | None, Input(ids.MEMBERS_REVISION_ID, "data", allow_optional=True)]
    page_json: Annotated[str | None, State(experiment_ids.STATE_PAGE_STORAGE, "data", allow_optional=True)]
    paging_json: Annotated[str | None, State(experiment_ids.PANEL_PAGING_ID, "data", allow_optional=True)]
    selected: Annotated[
        list[dict[str, Any]] | dict[str, Any] | None,
        State(ids.COMPARISON_GRID_ID, "selectedRows", allow_optional=True),
    ]
    signature: Annotated[str | None, State(ids.MEMBERS_SIGNATURE_ID, "data", allow_optional=True)]


class _Act(CallbackStates):
    """What `act_on_members` reads: the request, and the page and paging it is made on."""

    request: Annotated[
        dict[str, Any] | None, Input(request_store_id(RunListSurface.COMPARISON), "data", allow_optional=True)
    ]
    page_json: Annotated[str, State(experiment_ids.STATE_PAGE_STORAGE, "data")]
    paging_json: Annotated[str, State(experiment_ids.PANEL_PAGING_ID, "data")]


def _picked(selected: list[dict[str, Any]] | dict[str, Any] | None) -> frozenset[int]:
    """The runs a grid has selected: its rows once ag-grid reports them, the `{"ids": [...]}` it was seeded with until then."""
    if selected is None:
        return frozenset()
    if isinstance(selected, dict):
        return frozenset(int(run_id) for run_id in selected.get("ids", ()))
    return frozenset(int(row["run_id"]) for row in selected)


def _hide_or_show(request: RunActionRequest, act: _Act) -> tuple[Any, ...]:
    """Hide or show the requested runs in the viewer's charts: their paging, so nothing is saved."""
    paging = Paging.model_validate_json(act.paging_json)
    hidden = (
        paging.hidden - request.run_ids
        if request.action is RunAction.DRAW
        else paging.hidden | request.run_ids
    )
    container, page_json, revision = membership_changed(
        act.page_json, paging.with_hidden(hidden).model_dump_json()
    )
    return container, page_json, revision, no_update, no_update, no_update


def _compare(request: RunActionRequest, act: _Act) -> tuple[Any, ...]:
    """Start a comparison of the requested runs from this one's layout, or add them to another, and go to it."""
    page = page_model.BasicExperimentPage.model_validate_json(act.page_json)
    # Runs added to the comparison on screen change its list, which is drawn again, and not the view: no navigation.
    editable = get_data_store().can_change_comparison_runs(page.id)
    compared = compare(
        request,
        project_id=typing.cast("int", page.project_id),
        layout=page,
        here=page.id if editable else None,
    )
    if compared.changed:
        container, page_json, revision = membership_changed(act.page_json, act.paging_json)
        return container, page_json, revision, compared.notice, no_update, no_update
    return (
        no_update,
        no_update,
        no_update,
        no_update if compared.notice is None else compared.notice,
        no_update,
        no_update if compared.destination is None else compared.destination,
    )


def _remove(request: RunActionRequest, act: _Act) -> tuple[Any, ...]:
    """Take the requested runs out of the comparison, if the viewer may."""
    page = page_model.BasicExperimentPage.model_validate_json(act.page_json)
    try:
        get_data_store().remove_comparison_runs(page.id, request.run_ids)
    except PermissionError:
        refused = notice(
            "Not yours to change",
            "Only its owner, or an editor of a shared comparison, can take runs out of it.",
            color="red",
        )
        return no_update, no_update, no_update, refused, no_update, no_update
    container, page_json, revision = membership_changed(act.page_json, act.paging_json)
    return container, page_json, revision, no_update, no_update, no_update


def _act(request: RunActionRequest, act: _Act) -> tuple[Any, ...]:
    """What to change on the page for `request`, in the order of `act_on_members`'s `Output`s."""
    match request.action:
        case RunAction.DRAW | RunAction.HIDE:
            return _hide_or_show(request, act)
        case RunAction.REMOVE:
            return _remove(request, act)
        case RunAction.PIN | RunAction.UNPIN:
            pinned = pin_or_unpin(request, ids.COMPARISON_GRID_ID)
            return no_update, no_update, no_update, pinned.notice, pinned.rows, no_update
        case RunAction.COMPARE_NEW | RunAction.COMPARE_ADD | RunAction.COMPARE_PINS:
            return _compare(request, act)
        case RunAction.DIFF | RunAction.DELETE:
            # Not on a comparison yet (the diff reads one experiment), and its runs are deleted from their experiments.
            raise PreventUpdate


def register_members_callbacks(app: Dash) -> None:
    """Draw the sidebar's list, act on the selected runs, and follow the comparison to the copy an edit forked."""
    register_run_list(app, RunListSurface.COMPARISON, ids.COMPARISON_GRID_ID)
    register_row_updates(app, RunListSurface.COMPARISON)
    register_navigate(app, navigate_id(RunListSurface.COMPARISON))
    register_pin_filter(app, RunListSurface.COMPARISON, ids.COMPARISON_GRID_ID)

    @app.callback(
        Output(constants.NAVBAR_RUN_LIST_ID, "children", allow_duplicate=True),
        _Draw.states(),
        prevent_initial_call=True,
    )
    def draw_members(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> Component:
        drawn = _Draw.model_validate(states)
        if drawn.page_json is None or drawn.paging_json is None:
            raise PreventUpdate
        page = page_model.BasicExperimentPage.model_validate_json(drawn.page_json)
        members = render_members(
            get_data_store(),
            page,
            Paging.model_validate_json(drawn.paging_json).hidden,
            _picked(drawn.selected),
            prev_signature=drawn.signature,
        )
        if members is None:
            raise PreventUpdate
        return members

    @app.callback(
        Output(experiment_ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(experiment_ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ids.MEMBERS_REVISION_ID, "data", allow_duplicate=True),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        Output(row_updates_id(RunListSurface.COMPARISON), "data", allow_duplicate=True),
        Output(navigate_id(RunListSurface.COMPARISON), "data"),
        _Act.states(),
        prevent_initial_call=True,
    )
    def act_on_members(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[Any, ...]:
        act = _Act.model_validate(states)
        if act.request is None:
            raise PreventUpdate
        return _act(RunActionRequest.model_validate(act.request), act)

    app.clientside_callback(
        _FOLLOW_FORK_JS.source,
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),  # always there, never written
        Input(experiment_ids.STATE_PAGE_STORAGE, "data"),
        State(ids.COMPARISON_ID_STORE, "data", allow_optional=True),
        prevent_initial_call=True,
    )
