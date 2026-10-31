"""
The sidebar of an experiment page: its runs, as a list the charts and the actions on runs are driven from.

A run has an eye, to draw it in the charts or not (saved with the view, as a selection of runs always was), and a
checkbox, to select it for the bar of actions above the list (`RunAction`): show, hide, diff or delete the selected runs.
A Columns picker adds hyperparameters and metrics. It renders into `constants.NAVBAR_RUN_LIST_ID`, a different container
from `accordion_view`'s `METRIC_CONTENT_ID` tree; what it asks for goes through `request_store_id`, in the page's layout.
"""

from __future__ import annotations

import hashlib
import json
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.models import MetricColumn
from dlboard.serve import ClientsideScript, Icon, get_current_user, get_data_store
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, StoreId, ValueId
from dlboard.serve._grid import column_def, infer_column_dtype
from dlboard.serve._pages._dash_helpers import (
    CallbackStates,
    register_navigate,
    section_label,
    tooltipped_action_icon,
)
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._column_catalog import (
    KEY_PICKER_LIMIT,
    first_matches,
    summary_hparam_keys,
)
from dlboard.serve._pages._experiment._compare import compare, compare_menu, compare_pins_menu
from dlboard.serve._pages._experiment._focus import experiment_href
from dlboard.serve._pages._experiment._live_updates import STATE_LAST_KNOWN_REVISION
from dlboard.serve._pages._experiment._paging import Paging
from dlboard.serve._pages._experiment._run_compare import COMPARE_MODAL_ID, COMPARE_RUNS_ID, MAX_COMPARED_RUNS
from dlboard.serve._pages._experiment._shown_runs import ShownRunsSettings, shown_run_ids
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID
from dlboard.serve._pages._pins._pin_filter import pin_filter, pin_filter_id, register_pin_filter
from dlboard.serve._pages._pins._pin_menus import pin_menus
from dlboard.serve._pages._pins._row_pins import (
    PINS_FIELD,
    experiment_pin_cells,
    pin_column_def,
    pin_or_unpin,
)
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
    FOCUSED_FIELD,
    HREF_FIELD,
    eye_column_def,
    name_column_def,
    run_fields,
    status_column_def,
)
from dlboard.serve._pages._run_list._delete import (
    ask_to_delete,
    delete_outputs,
    delete_shells,
    register_delete,
)
from dlboard.serve._pages._run_list._ids import navigate_id
from dlboard.serve._pages._run_list._rows import (
    RowUpdate,
    RowUpdates,
    register_row_updates,
    row_update_shells,
    row_updates_id,
)
from dlboard.serve._pages._run_list._table import run_grid

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import dash_ag_grid as dag
    from dash.development.base_component import Component

    from dlboard.models import DataStore, Run
    from dlboard.serve._pages._pins._pin_view import PinCell

_log = get_logger(__name__)

NAVBAR_HPARAM_DATATABLE_ID: ValueId[ids.ExperimentPage] = ValueId("navbar-hparam-datatable")
NAVBAR_HPARAM_COL_SELECT_ID: ValueId[ids.ExperimentPage] = ValueId("navbar-hparam-col-select")
NAVBAR_HPARAM_TABLE_BODY_ID: typing.Final = "navbar-hparam-table-body"
NAVBAR_HPARAM_CONFIRM_COLS_ID: ButtonId[ids.ExperimentPage] = ButtonId("navbar-hparam-confirm-cols")
NAVBAR_HPARAM_COLUMNS_TOGGLE_ID: ButtonId[ids.ExperimentPage] = ButtonId("navbar-hparam-columns-toggle")
NAVBAR_NEWEST_RUNS_ID: ButtonId[ids.ExperimentPage] = ButtonId("navbar-newest-runs")
NAVBAR_HPARAM_APPLIED_COLS_ID: StoreId[ids.ExperimentPage] = StoreId("navbar-hparam-applied-cols")
NAVBAR_HPARAM_SIGNATURE_ID: StoreId[ids.ExperimentPage] = StoreId("navbar-hparam-signature")
NAVBAR_RUN_HEAD_ID: typing.Final = "navbar-run-head"
"""What is said about what is drawn (how many runs are not, the way back to the view's): redrawn by the actions that change it."""

_HPARAM_COLS_CHANGED_JS = ClientsideScript(Path(__file__).with_name("hparam_cols_changed.js"))


class _Listed(typing.NamedTuple):
    """What the table lists, read afresh on every tick that saw a change: its runs, their hyperparameters and which are drawn."""

    runs: list[Run]
    hparams: list[models.HyperParams]
    selection: _Selection
    view_id: int | None
    """The view the page is of, which a link to a run stays in."""


def _build_hparam_rows(
    experiment_id: int,
    listed: _Listed,
    hparams_by_run: dict[int, models.HyperParams],
    latest_metrics: dict[int, dict[str, float]],
    pins: Mapping[int, list[PinCell]],
) -> list[dict[str, Any]]:
    """One row per run, whether or not it has logged hyperparameters or metrics yet."""
    rows: list[dict[str, Any]] = []
    for run in listed.runs:
        row: dict[str, Any] = {
            DRAWN_FIELD: run.id in listed.selection.shown,
            FOCUSED_FIELD: run.id == listed.selection.focus,
            HREF_FIELD: experiment_href(experiment_id, view_id=listed.view_id, run_id=run.id),
            PINS_FIELD: [cell.model_dump(mode="json") for cell in pins.get(run.id, [])],
            **run_fields(run),
        }
        hparam = hparams_by_run.get(run.id)
        if hparam is not None:
            row.update(hparam.hparams_dict)
        row.update(latest_metrics.get(run.id, {}))
        rows.append(row)
    return rows


def _load_hparam_view_data(
    store: DataStore[...],
    experiment_id: int,
    listed: _Listed,
    selected: list[str],
    pins: Mapping[int, list[PinCell]],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    Build displayable rows for the `listed` runs, enriched with hparams and any selected last-step metrics.

    Key *names* are what a picker offers (`summary_hparam_keys`, and a cheap `DISTINCT key` lookup of metrics), plus any
    selected key a run holds, so a column picked from an older run stays applied; metric *values* are only fetched for
    the selected metrics -- the columns the table is actually about to show. Takes what is `listed` rather than
    querying it itself: `render_navbar_hparams` already needs it to decide *whether* to call this at all (see
    `_render_signature`), on every live-update poll tick that saw a change.
    """
    hparams_by_run = {h.run_id: h for h in listed.hparams}
    held = {key for h in listed.hparams for key in h.hparams_dict}
    hparam_keys = sorted(
        {*summary_hparam_keys(store, experiment_id), *(key for key in selected if key in held)}
    )
    metric_keys = store.summarize_metric_keys(experiment_id)
    selected_metrics = set(selected)
    wanted = frozenset(selected_metrics & set(metric_keys))
    latest: dict[int, dict[str, float]] = (
        store.fetch_metrics(
            experiment_id, keys=wanted, reduction=models.MetricReduction.LATEST, run_ids=None
        ).latest_per_run()
        if wanted
        else {}
    )
    rows = _build_hparam_rows(experiment_id, listed, hparams_by_run, latest, pins)
    return hparam_keys, metric_keys, rows


def run_table_shells() -> list[Component]:
    """
    What the table's actions use that must not mount with the table.

    The store they are requested through, and the modal that asks before a delete. In the page's layout once, like every
    modal the panel area can open: a callback listening to something that mounts with the table is called as it mounts,
    though nobody asked anything.
    """
    return [
        Store(id=request_store_id(RunListSurface.EXPERIMENT)),
        Store(id=navigate_id(RunListSurface.EXPERIMENT)),
        *row_update_shells(RunListSurface.EXPERIMENT),
        *delete_shells(RunListSurface.EXPERIMENT),
    ]


def _build_hparam_datatable(
    rows: list[dict[str, Any]],
    selected: list[str],
    picked: frozenset[int],
    *,
    table_id: str,
) -> dag.AgGrid:
    column_defs = [
        eye_column_def(),
        name_column_def(swatch=True),
        pin_column_def(),
        status_column_def(),
        *(column_def(key, infer_column_dtype(rows, key)) for key in selected),
    ]
    return run_grid(table_id, column_defs, rows, picked, swatches=True)


def _render_hparam_panel(
    table: Component,
    hparam_keys: list[str],
    metric_keys: list[str],
    selected: list[str],
    head: Sequence[Component],
) -> Component:
    """
    The runs table, with the bar of actions on the rows selected in it, plus a Columns picker collapsed behind an icon so it doesn't permanently eat a row above the table it configures.

    Column changes only take effect on "Apply" (not per-tick) -- computing the table involves a
    hyperparameter/metric fetch per run, so committing it once per intended change instead of once
    per checkbox click keeps a multi-column edit from queueing up a burst of redundant store reads.
    "Apply" itself only appears once the picked columns actually differ from what's applied (a
    clientside callback compares against `NAVBAR_HPARAM_APPLIED_COLS_ID`'s baseline).
    """
    applied = [k for k in selected if k in hparam_keys or k in metric_keys]
    return html.Div(
        [
            dmc.Group(
                [
                    section_label("Runs"),
                    dmc.Popover(
                        [
                            dmc.PopoverTarget(
                                tooltipped_action_icon(
                                    Icon.COLUMNS,
                                    component_id=NAVBAR_HPARAM_COLUMNS_TOGGLE_ID,
                                    label="Choose columns",
                                )
                            ),
                            dmc.PopoverDropdown(
                                dmc.Group(
                                    [
                                        dmc.MultiSelect(
                                            id=NAVBAR_HPARAM_COL_SELECT_ID,
                                            data=_column_options(hparam_keys, metric_keys, keep=applied),
                                            value=applied,
                                            limit=KEY_PICKER_LIMIT,
                                            searchable=True,
                                            clearable=True,
                                            size="xs",
                                            style={"flex": 1, "minWidth": 220},
                                            # Render this dropdown inline (not its own portal) so a
                                            # click on one of its options still lands inside the
                                            # outer Popover's own DOM subtree -- otherwise the
                                            # Popover's own "click outside closes it" detection sees
                                            # the option click as outside and closes before the
                                            # selection is ever applied.
                                            comboboxProps={"withinPortal": False},
                                        ),
                                        dmc.Button(
                                            "Apply",
                                            id=NAVBAR_HPARAM_CONFIRM_COLS_ID,
                                            size="xs",
                                            style={"display": "none", "flexShrink": 0},
                                        ),
                                    ],
                                    align="center",
                                    gap="xs",
                                    wrap="nowrap",
                                )
                            ),
                        ],
                        position="bottom-end",
                        withinPortal=True,
                    ),
                    Store(id=NAVBAR_HPARAM_APPLIED_COLS_ID, data=applied),
                ],
                align="center",
                justify="space-between",
                gap="xs",
                wrap="nowrap",
            ),
            html.Div(head, id=NAVBAR_RUN_HEAD_ID),
            html.Div(table, id=NAVBAR_HPARAM_TABLE_BODY_ID),
        ]
    )


def _project_of(store: DataStore[...], experiment_id: int) -> int:
    """The project an experiment is of."""
    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        msg = f"Experiment {experiment_id} is gone, or is not yours to see"
        raise LookupError(msg)
    return experiment.project_id


def _run_head(
    store: DataStore[...], experiment_id: int, selection: _Selection, chips: Sequence[str]
) -> list[Component]:
    """
    What sits above the table and changes with what is drawn, compared or pinned.

    The bar of actions on the selected runs, the menu that compares the runs pinned in a colour, the runs logged after the
    drawn ones with the way back to the view's own, and the pin chips that find a pinned run. Redrawn by the actions that
    change any of it, in place of the list. Read after the list's signature check, so a list nobody has changed reads none
    of the comparisons or pin counts.
    """
    surface = RunListSurface.EXPERIMENT
    project_id = _project_of(store, experiment_id)
    drawn: list[Component] = (
        [
            dmc.Group(
                [
                    dmc.Text("Drawing the runs you picked", size="xs", c="dimmed"),
                    *(
                        [dmc.Text(f"{selection.newer_hidden} newer not drawn", size="xs", c="orange")]
                        if selection.newer_hidden
                        else []
                    ),
                    dmc.Button(
                        f"Newest {ShownRunsSettings().default_shown_runs}",
                        id=NAVBAR_NEWEST_RUNS_ID,
                        n_clicks=0,
                        variant="subtle",
                        size="compact-xs",
                    ),
                ],
                justify="space-between",
                gap="xs",
                wrap="nowrap",
            )
        ]
        if selection.chosen
        else []
    )
    compare_pins = compare_pins_menu(surface, store.pin_counts(project_id))
    return [
        action_bar(
            surface,
            simple_action(surface, RunAction.DRAW, "Show", icon_name=Icon.EYE),
            simple_action(surface, RunAction.HIDE, "Hide", icon_name=Icon.EYE_OFF),
            simple_action(surface, RunAction.DIFF, "Diff", icon_name=Icon.COMPARE),
            *pin_menus(surface),
            compare_menu(surface, store.list_comparisons(project_id, get_current_user().id, editable=True)),
            simple_action(surface, RunAction.DELETE, "Delete", icon_name=Icon.DELETE, color="red"),
        ),
        *([compare_pins] if compare_pins is not None else []),
        *drawn,
        *_pin_chips(store, experiment_id, chips),
    ]


def _pin_chips(store: DataStore[...], experiment_id: int, chips: Sequence[str]) -> list[Component]:
    """The pin chips for the colours some run of the experiment is pinned in, with the ones the grid is filtered to picked."""
    present = {
        cell.color
        for cells in experiment_pin_cells(store, experiment_id, get_current_user().id).values()
        for cell in cells
    }
    return [
        chip for chip in [pin_filter(RunListSurface.EXPERIMENT, present, value=chips)] if chip is not None
    ]


def _head_and_signature(
    store: DataStore[...], page: page_model.BasicExperimentPage, experiment_id: int, chips: Sequence[str]
) -> tuple[list[Component], str]:
    """
    The head as `page` draws it now, and the signature the list is drawn under.

    Nothing is focused: a draw or hide clears a focus. The action that changed what is drawn or pinned stores this signature
    itself, so the list's own render sees nothing to draw again.
    """
    ref = repo.ref_of(page)
    runs, selection, selected = _load_hparam_panel_settings(store, experiment_id, ref, None)
    hparams = list(store.fetch_hyperparams(experiment_id, run_ids=None))
    listed = _Listed(runs, hparams, selection, ref.view_id if isinstance(ref, repo.ExperimentRef) else None)
    return _run_head(store, experiment_id, selection, chips), _render_signature(
        experiment_id, listed, selected
    )


def _head_fields(
    store: DataStore[...], page: page_model.BasicExperimentPage, experiment_id: int, chips: Sequence[str]
) -> dict[str, Any]:
    """The head and the signature `_head_and_signature` gives, as the fields of an action's `_Outputs`."""
    head, signature = _head_and_signature(store, page, experiment_id, chips)
    return {"head": head, "signature": signature}


def _drawn_rows(old_drawn: frozenset[int], new_drawn: frozenset[int], focus: int | None) -> RowUpdates:
    """
    The rows whose drawn state changed from `old_drawn` to `new_drawn`, and the focused row (if a focus was cleared), in place.

    A row is patched, not drawn again: its eye (`_drawn`) and its link's focus (`_focused`) are the only fields that change.
    """
    values: dict[int, dict[str, Any]] = {
        run: {DRAWN_FIELD: run in new_drawn} for run in old_drawn ^ new_drawn
    }
    if focus is not None:
        values.setdefault(focus, {})[FOCUSED_FIELD] = False
    return RowUpdates(
        grid=NAVBAR_HPARAM_DATATABLE_ID,
        rows=[RowUpdate(run_id=run, values=fields) for run, fields in sorted(values.items())],
    )


class _Selection(typing.NamedTuple):
    """The runs the charts draw, and whether a view selected them (rather than their being the newest)."""

    shown: frozenset[int]
    """The runs drawn: the view's own, or just the `focus`."""
    chosen: bool
    focus: int | None
    """The one run drawn instead of the view's, if the page was opened on a run that is the experiment's (`Paging.focus`)."""
    newer_hidden: int
    """Runs logged after the newest run a selection draws, which that selection leaves out. Zero without a selection."""


def _newer_hidden(runs: list[Run], shown: frozenset[int]) -> int:
    """How many runs were logged after the newest one drawn, and are not drawn: a selection made earlier leaves them out."""
    drawn = [run.created_at for run in runs if run.id in shown]
    if not drawn:
        return 0
    newest_drawn = max(drawn)
    return sum(1 for run in runs if run.id not in shown and run.created_at > newest_drawn)


def _load_hparam_panel_settings(
    store: DataStore[...], experiment_id: int, ref: repo.PageRef, focus: int | None
) -> tuple[list[Run], _Selection, list[str]]:
    """
    The cheap, always-needed inputs for the navbar table: its run list and its persisted settings.

    Deliberately doesn't touch metric keys/values (`_load_hparam_view_data`, the expensive part) --
    `render_navbar_hparams` needs exactly this much to decide *whether* a re-render is even
    happening before paying for that, since it's re-evaluated on every live-update poll tick, not
    just on a real settings change.
    """
    page = repo.load_page(store, ref)
    view_shown, columns = shown_run_ids(store, experiment_id, page.document), page.document.hparam_columns
    runs = list(store.get_runs(experiment_id, limit=None, run_ids=None))
    chosen = page.document.selected_runs is not None
    focused = focus if focus in {run.id for run in runs} else None
    selection = _Selection(
        frozenset({focused}) if focused is not None else view_shown,
        chosen=chosen,
        focus=focused,
        newer_hidden=_newer_hidden(runs, view_shown) if chosen else 0,
    )
    return runs, selection, columns


def _render_signature(experiment_id: int, listed: _Listed, selected: list[str]) -> str:
    """
    A digest of everything `render_navbar_hparams`'s output actually depends on, as one comparable value.

    Stored in (and read back from) `NAVBAR_HPARAM_SIGNATURE_ID`'s `dcc.Store`. A digest, not the values: they are every
    run's, and the browser only needs to hand back whether they changed (#454). Naming every tracked field in one place
    means a future addition to what this table renders from has one obvious place to also add it, instead of a
    silently-still-passing equality check against a signature nobody remembered to extend. The runs (not just
    `hparams`) must be included: a run with no hyperparameters yet still needs to appear the moment `store.get_runs`
    (queried fresh in `_load_hparam_panel_settings`, *before* this signature is even built) reports it, even though
    `hparams` itself hasn't changed -- and so must each one's status and end time, or a run finishing would never
    redraw its row.
    """
    run_states = [
        [run.id, run.status, run.ended_at.isoformat() if run.ended_at else None] for run in listed.runs
    ]
    selection = listed.selection
    # Drawing a run (the eye) does not come through here: its action patches the rows that changed, rewrites the head, and
    # stores this signature itself (`_head_and_signature`), so the list is not drawn again and keeps its filters and scroll.
    signed = [
        experiment_id,
        listed.view_id,
        [[h.run_id, h.hparams_dict] for h in sorted(listed.hparams, key=lambda h: h.run_id)],
        sorted(selection.shown),
        selection.focus,
        selection.chosen,
        selection.newer_hidden,
        selected,
        run_states,
    ]
    return hashlib.sha256(json.dumps(signed, sort_keys=True, default=str).encode()).hexdigest()


def _picked_ids(selected_rows: list[dict[str, Any]] | dict[str, Any] | None) -> frozenset[int]:
    """
    The runs a grid has selected.

    Its `selectedRows` is the rows once ag-grid reports them, and the `{"ids": [...]}` it was seeded with until it does.
    """
    if selected_rows is None:
        return frozenset()
    if isinstance(selected_rows, dict):
        return frozenset(int(run_id) for run_id in selected_rows.get("ids", ()))
    return frozenset(int(row[MetricColumn.RUN_ID]) for row in selected_rows)


def _register_hparam_table(app: Dash) -> None:
    @app.callback(
        Output(constants.NAVBAR_RUN_LIST_ID, "children"),
        # The experiment's revision, which the live poll moves on: what the table holds may have changed (#454).
        Input(STATE_LAST_KNOWN_REVISION, "data", allow_optional=True),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
        Input(ids.STATE_PAGE_STORAGE, "data", allow_optional=True),
        State(NAVBAR_HPARAM_SIGNATURE_ID, "data", allow_optional=True),
        State(ids.STATE_VIEW_ID, "data", allow_optional=True),
        State(ids.PANEL_PAGING_ID, "data", allow_optional=True),
        State(NAVBAR_HPARAM_DATATABLE_ID, "selectedRows", allow_optional=True),
        State(pin_filter_id(RunListSurface.EXPERIMENT), "value", allow_optional=True),
        prevent_initial_call=True,
    )
    def render_navbar_hparams(  # noqa: PLR0913 -- the inputs the table is drawn from
        _revision: int | None,
        experiment_id: int | None,
        page_json: str | None,
        prev_signature: str | None,
        view_id: int | None,
        paging_json: str | None,
        selected_rows: list[dict[str, Any]] | dict[str, Any] | None,
        chips: list[str] | None,
    ) -> dmc.Stack:
        if experiment_id is None:
            raise PreventUpdate

        store = get_data_store()
        # `page_json` (this callback's own `STATE_PAGE_STORAGE` trigger) is the freshest known page,
        # ahead of `STATE_VIEW_ID` when a structural edit just branched into a new view of its own
        # (`_page_repository.save_page`) -- reading `view_id` instead here would still find
        # the page that edit branched *away* from, since `sync_view_after_edit` only catches
        # `STATE_VIEW_ID` up to it a separate, slightly later round trip.
        ref = (
            repo.ref_of(page_model.BasicExperimentPage.model_validate_json(page_json))
            if page_json is not None
            else repo.ExperimentRef(experiment_id, view_id)
        )
        focus = Paging.model_validate_json(paging_json).focus if paging_json is not None else None
        runs, selection, selected = _load_hparam_panel_settings(store, experiment_id, ref, focus)
        # `STATE_PAGE_STORAGE` changes on *every* page-settings write -- a tab switch, a panel
        # rename, a drag-reorder, none of which this table cares about -- so rebuilding on it
        # unconditionally (tearing down and remounting the ag-grid) flickered on every one of
        # those, not just an actual run-selection/column change. Skip the (expensive, chart-
        # remounting) rebuild unless the specific settings this table renders actually changed --
        # see `_render_signature` for exactly what "changed" is measured against.
        #
        # Checked *before* `_load_hparam_view_data` below (not after): the revision moves on every
        # live-update poll tick that finds *any* change in the experiment, metrics included -- so
        # this callback re-fires roughly as often as metrics get flushed during active training,
        # not just when a run/hparam/column-selection actually changes. `_load_hparam_view_data`'s
        # key reads and (if a metric column is selected) latest-value read would otherwise run on
        # every one of those ticks only to almost always end in the `PreventUpdate` below anyway.
        # The runs and their hyperparameters are enough to detect every case this table needs to
        # react to, so the rest only runs on the ticks that actually need a re-render.
        hparams = list(store.fetch_hyperparams(experiment_id, run_ids=None))
        # The view of the freshest page, not `STATE_VIEW_ID`: after an edit that branched into a view that store is a round trip behind,
        # and a link to a run must stay in the view the charts are in.
        listed = _Listed(
            runs, hparams, selection, ref.view_id if isinstance(ref, repo.ExperimentRef) else None
        )
        signature = _render_signature(experiment_id, listed, selected)
        if signature == prev_signature:
            raise PreventUpdate

        # The pins are read only for a list that is drawn; the chips are what the grid is filtered to, kept across a redraw.
        pins = experiment_pin_cells(store, experiment_id, get_current_user().id)
        hparam_keys, metric_keys, rows = _load_hparam_view_data(store, experiment_id, listed, selected, pins)

        # Selecting runs is work in progress, and the table is drawn again when a run starts or a choice changes under it:
        # what was selected stays so.
        picked = _picked_ids(selected_rows)
        return dmc.Stack(
            [
                _render_hparam_panel(
                    _build_hparam_datatable(rows, selected, picked, table_id=NAVBAR_HPARAM_DATATABLE_ID),
                    hparam_keys,
                    metric_keys,
                    selected,
                    _run_head(store, experiment_id, selection, chips or []),
                ),
                Store(id=NAVBAR_HPARAM_SIGNATURE_ID, data=signature),
            ],
            gap="xs",
        )

    # The Apply button only makes sense once the picked columns diverge from what's applied --
    # comparing client-side (rather than round-tripping through a server callback per keystroke)
    # keeps this instant and avoids yet another spurious-rerender source.
    app.clientside_callback(
        _HPARAM_COLS_CHANGED_JS.source,
        Output(NAVBAR_HPARAM_CONFIRM_COLS_ID, "style"),
        Input(NAVBAR_HPARAM_COL_SELECT_ID, "value"),
        State(NAVBAR_HPARAM_APPLIED_COLS_ID, "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(NAVBAR_HPARAM_CONFIRM_COLS_ID, "n_clicks", allow_optional=True),
        State(NAVBAR_HPARAM_COL_SELECT_ID, "value", allow_optional=True),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def persist_selected_hparam_cols(n_clicks: int | None, selected: list[str] | None, page_json: str) -> str:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        # `ref_of`, not `STATE_VIEW_ID` -- see `render_navbar_hparams`'s own comment on why.
        curr_page = page_model.BasicExperimentPage.model_validate_json(page_json)
        page = repo.save_document_settings(
            store,
            repo.ref_of(curr_page),
            page_model.DocumentSettings(hparam_columns=selected or []),
        )
        return page.model_dump_json()


def _register_newest_runs(app: Dash) -> None:
    @app.callback(
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(row_updates_id(RunListSurface.EXPERIMENT), "data", allow_duplicate=True),
        Output(NAVBAR_RUN_HEAD_ID, "children", allow_duplicate=True),
        Output(NAVBAR_HPARAM_SIGNATURE_ID, "data", allow_duplicate=True),
        Input(NAVBAR_NEWEST_RUNS_ID, "n_clicks", allow_optional=True),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        State(pin_filter_id(RunListSurface.EXPERIMENT), "value", allow_optional=True),
        prevent_initial_call=True,
    )
    def show_newest_runs(
        n_clicks: int | None, page_json: str, paging_json: str, chips: list[str] | None
    ) -> tuple[str, html.Div, dict[str, Any], list[Component], str]:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        ref = repo.ref_of(page_model.BasicExperimentPage.model_validate_json(page_json))
        before = repo.load_page(store, ref)
        if before.document.selected_runs is None:
            raise PreventUpdate
        experiment_id = typing.cast("int", before.experiment_id)
        focus = Paging.model_validate_json(paging_json).focus
        shown_before = shown_run_ids(store, experiment_id, before.document, focus)
        page, container = edits.commit(
            page_json,
            paging_json,
            edits.PageEdit(document=page_model.DocumentSettings(selected_runs=None, excluded_runs=[])),
        )
        shown_now = shown_run_ids(store, experiment_id, page.document)
        return (
            page.model_dump_json(),
            container,
            _drawn_rows(shown_before, shown_now, focus).dump(),
            *_head_and_signature(store, page, experiment_id, chips or []),
        )


def _notice(title: str, message: str, *, color: str = "yellow") -> list[dict[str, Any]]:
    """A notification for the page's notifications (`VIEW_NOTIFY_ID`)."""
    return [
        {
            "action": "show",
            "id": f"run-list-{title}",
            "title": title,
            "message": message,
            "color": color,
            "autoClose": 6_000,
        }
    ]


@dataclass(frozen=True)
class _Outputs:
    """What `act_on_runs` changes on the page: whatever it leaves out stays as it was."""

    page_json: Any = no_update
    container: Any = no_update
    notices: Any = no_update
    delete_opened: Any = no_update
    delete_body: Any = no_update
    delete_pending: Any = no_update
    diff_runs: Any = no_update
    diff_opened: Any = no_update
    row_updates: Any = no_update
    head: Any = no_update
    signature: Any = no_update
    destination: Any = no_update

    def outputs(self) -> tuple[Any, ...]:
        """The values, in the order of `act_on_runs`'s `Output`s."""
        return (
            self.page_json,
            self.container,
            self.notices,
            self.delete_opened,
            self.delete_body,
            self.delete_pending,
            self.diff_runs,
            self.diff_opened,
            self.row_updates,
            self.head,
            self.signature,
            self.destination,
        )


class _Act(CallbackStates):
    """What `act_on_runs` reads: the request, and the page and paging it is made on."""

    request: Annotated[dict[str, Any] | None, Input(request_store_id(RunListSurface.EXPERIMENT), "data")]
    page_json: Annotated[str, State(ids.STATE_PAGE_STORAGE, "data")]
    paging_json: Annotated[str, State(ids.PANEL_PAGING_ID, "data")]
    experiment_id: Annotated[int, State(constants.STATE_EXPERIMENT_ID, "data")]
    chips: Annotated[
        list[str] | None, State(pin_filter_id(RunListSurface.EXPERIMENT), "value", allow_optional=True)
    ]


def _draw_or_hide(store: DataStore[...], request: RunActionRequest, act: _Act) -> _Outputs:
    """
    Draw or hide the requested runs: the view's selection of runs, saved with it, so it is the same for everyone who opens it.

    Always from the view's own runs, never from one focused run (`Paging.focus`): a link to a run is how someone looks at
    it, and drawing another beside it is then a change to what the view draws, which leaves the focus.
    """
    document = repo.load_page(
        store, repo.ref_of(page_model.BasicExperimentPage.model_validate_json(act.page_json))
    ).document
    paging = Paging.model_validate_json(act.paging_json)
    asked = frozenset(
        run.id for run in store.get_runs(act.experiment_id, limit=None, run_ids=request.run_ids)
    )
    drawn = shown_run_ids(store, act.experiment_id, document)
    now = drawn | asked if request.action is RunAction.DRAW else drawn - asked
    # What the list shows now: a focused run (`?run=`) or the view's runs, whichever is drawn.
    shown_before = shown_run_ids(store, act.experiment_id, document, paging.focus)
    if now == drawn and paging.focus is None:
        raise PreventUpdate
    cap = ShownRunsSettings().max_shown_runs
    if len(now) > cap:
        return _Outputs(
            notices=_notice(
                "Too many runs to draw", f"A view draws at most {cap} runs. Hide some to draw others."
            )
        )
    page, container = edits.commit(
        act.page_json,
        paging.with_focus(None).model_dump_json(),
        # A selection replaces what a view left out before selections existed, so that is cleared with it.
        edits.PageEdit(document=page_model.DocumentSettings(selected_runs=sorted(now), excluded_runs=[])),
    )
    return _Outputs(
        page_json=page.model_dump_json(),
        container=container,
        row_updates=_drawn_rows(shown_before, now, paging.focus).dump(),
        **_head_fields(store, page, act.experiment_id, act.chips or []),
    )


def _compare(request: RunActionRequest, act: _Act) -> _Outputs:
    """Start a comparison of the requested runs, or add them to one, and go to it."""
    store = get_data_store()
    layout = repo.load_page(
        store, repo.ref_of(page_model.BasicExperimentPage.model_validate_json(act.page_json))
    )
    compared = compare(request, project_id=_project_of(store, act.experiment_id), layout=layout)
    return _Outputs(
        destination=no_update if compared.destination is None else compared.destination,
        notices=no_update if compared.notice is None else compared.notice,
    )


def _diff(request: RunActionRequest) -> _Outputs:
    """Open the A/B diff on the requested runs, if there are few enough to read side by side."""
    if len(request.run_ids) > MAX_COMPARED_RUNS:
        return _Outputs(
            notices=_notice("Too many runs to diff", f"The diff compares up to {MAX_COMPARED_RUNS} runs.")
        )
    # Newest first, as the modal has always listed the runs it was opened on: the first is the one the others are read against.
    return _Outputs(
        diff_runs=[str(run_id) for run_id in sorted(request.run_ids, reverse=True)], diff_opened=True
    )


def _register_run_actions(app: Dash) -> None:
    @app.callback(
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        *delete_outputs(RunListSurface.EXPERIMENT),
        Output(COMPARE_RUNS_ID, "value", allow_duplicate=True),
        Output(COMPARE_MODAL_ID, "opened", allow_duplicate=True),
        Output(row_updates_id(RunListSurface.EXPERIMENT), "data", allow_duplicate=True),
        Output(NAVBAR_RUN_HEAD_ID, "children", allow_duplicate=True),
        Output(NAVBAR_HPARAM_SIGNATURE_ID, "data", allow_duplicate=True),
        Output(navigate_id(RunListSurface.EXPERIMENT), "data"),
        _Act.states(),
        prevent_initial_call=True,
    )
    def act_on_runs(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[Any, ...]:
        act = _Act.model_validate(states)
        if act.request is None:
            raise PreventUpdate
        request = RunActionRequest.model_validate(act.request)
        match request.action:
            case RunAction.DRAW | RunAction.HIDE:
                return _draw_or_hide(get_data_store(), request, act).outputs()
            case RunAction.DIFF:
                return _diff(request).outputs()
            case RunAction.DELETE:
                asked = ask_to_delete(request.run_ids)
                return _Outputs(
                    delete_opened=asked.opened, delete_body=asked.body, delete_pending=asked.pending
                ).outputs()
            case RunAction.PIN | RunAction.UNPIN:
                pinned = pin_or_unpin(request, NAVBAR_HPARAM_DATATABLE_ID)
                page = page_model.BasicExperimentPage.model_validate_json(act.page_json)
                return _Outputs(
                    row_updates=pinned.rows,
                    notices=pinned.notice,
                    **_head_fields(get_data_store(), page, act.experiment_id, act.chips or []),
                ).outputs()
            case RunAction.COMPARE_NEW | RunAction.COMPARE_ADD | RunAction.COMPARE_PINS:
                return _compare(request, act).outputs()
            case RunAction.REMOVE:
                # Only a comparison holds runs that can be taken out of it.
                raise PreventUpdate

    register_delete(app, RunListSurface.EXPERIMENT, notify=VIEW_NOTIFY_ID)


def _column_options(
    hparam_keys: Sequence[str], metric_keys: Sequence[str], *, search: str = "", keep: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """
    The Columns picker's options: the first `KEY_PICKER_LIMIT` keys `search` finds, hyperparameters first, and `keep`.

    Never every name: an experiment of 11,000 metrics would send each of them to the browser (#416); the rest are a
    search away (`search_columns`, as someone types). `keep` is what the picker holds, so it keeps its picks.
    """
    hparams = first_matches(hparam_keys, search)
    metrics = first_matches(metric_keys, search, KEY_PICKER_LIMIT - len(hparams))
    kept_hparams = [key for key in keep if key in hparam_keys and key not in hparams]
    kept_metrics = [key for key in keep if key not in hparam_keys and key not in metrics]
    return [
        {"group": "Hyperparameters", "items": [*kept_hparams, *hparams]},
        {"group": "Metrics (latest)", "items": [*kept_metrics, *metrics]},
    ]


def _register_column_search(app: Dash) -> None:
    @app.callback(
        Output(NAVBAR_HPARAM_COL_SELECT_ID, "data"),
        Input(NAVBAR_HPARAM_COL_SELECT_ID, "searchValue"),
        State(NAVBAR_HPARAM_COL_SELECT_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def search_columns(
        search: str | None, picked: list[str] | None, experiment_id: int
    ) -> list[dict[str, Any]]:
        """The Columns picker's options for what is typed in it, searched on the server."""
        store = get_data_store()
        return _column_options(
            summary_hparam_keys(store, experiment_id),
            store.summarize_metric_keys(experiment_id),
            search=search or "",
            keep=picked or (),
        )


def register_runs_table_callbacks(app: Dash) -> None:
    _register_hparam_table(app)
    _register_newest_runs(app)
    register_run_list(app, RunListSurface.EXPERIMENT, NAVBAR_HPARAM_DATATABLE_ID)
    register_navigate(app, navigate_id(RunListSurface.EXPERIMENT))
    register_row_updates(app, RunListSurface.EXPERIMENT)
    register_pin_filter(app, RunListSurface.EXPERIMENT, NAVBAR_HPARAM_DATATABLE_ID)
    _register_run_actions(app)
    _register_column_search(app)
