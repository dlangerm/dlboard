"""
The navbar's run-comparison table: a Columns picker plus a run-selection/deletion data table.

Fully self-contained -- it renders into `constants.NAVBAR_RUN_LIST_ID`, a different container from
`accordion_view`'s `METRIC_CONTENT_ID` tree, and has no modal of its own to render beside it.
"""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.models import RUN_NAME_COLUMN, MetricColumn
from dlboard.serve import (
    ClientsideScript,
    Icon,
    get_current_user,
    get_data_store,
    icon_cell_class,
    series_swatch_class,
)
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, ModalId, StoreId, ValueId
from dlboard.serve._format import format_duration
from dlboard.serve._grid import column_def, infer_column_dtype, themed_grid_kwargs
from dlboard.serve._pages._dash_helpers import section_label, tooltipped_action_icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._column_catalog import (
    KEY_PICKER_LIMIT,
    first_matches,
    summary_hparam_keys,
)
from dlboard.serve._pages._experiment._live_updates import STATE_LAST_KNOWN_REVISION
from dlboard.serve._pages._experiment._run_compare import compare_button
from dlboard.serve._pages._experiment._shown_runs import ShownRunsSettings, shown_run_ids
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dash.development.base_component import Component

    from dlboard.models import DataStore, Run

_log = get_logger(__name__)

NAVBAR_HPARAM_DATATABLE_ID: ValueId[ids.ExperimentPage] = ValueId("navbar-hparam-datatable")
NAVBAR_HPARAM_COL_SELECT_ID: ValueId[ids.ExperimentPage] = ValueId("navbar-hparam-col-select")
NAVBAR_HPARAM_TABLE_BODY_ID: typing.Final = "navbar-hparam-table-body"
NAVBAR_HPARAM_CONFIRM_COLS_ID: ButtonId[ids.ExperimentPage] = ButtonId("navbar-hparam-confirm-cols")
NAVBAR_HPARAM_COLUMNS_TOGGLE_ID: ButtonId[ids.ExperimentPage] = ButtonId("navbar-hparam-columns-toggle")
NAVBAR_NEWEST_RUNS_ID: ButtonId[ids.ExperimentPage] = ButtonId("navbar-newest-runs")
NAVBAR_HPARAM_APPLIED_COLS_ID: StoreId[ids.ExperimentPage] = StoreId("navbar-hparam-applied-cols")
_ROW_HEIGHT = 34
_MAX_TABLE_HEIGHT = "50vh"
_DELETE_COLUMN_ID = "_delete"
_ROW_ID_FIELD = "_row_id"
_SWATCH_FIELD = "_swatch"
_STATUS_FIELD = "_status"
_DURATION_FIELD = "_duration_s"
_STATUS_TEXT_FIELD = "_status_text"
_STATUS_DETAIL_FIELD = "_status_detail"
"""Reserved row fields (underscore-prefixed, like `_swatch`): a hyperparameter or metric named "status" can't shadow them."""
_STATUS_MIN_WIDTH = 74
"""Room for "✕ 1h 2m" in the narrow sidebar the table lives in."""
_STATUS_STYLES: dict[models.RunStatus, str] = {
    models.RunStatus.RUNNING: "var(--mantine-color-blue-5)",
    models.RunStatus.FINISHED: "var(--mantine-color-dimmed)",
    models.RunStatus.FAILED: "var(--mantine-color-red-6)",
    models.RunStatus.UNKNOWN: "var(--mantine-color-yellow-6)",
}


def _duration_seconds(run: Run) -> int | None:
    """How long the run took, if it has ended."""
    return int((run.ended_at - run.created_at).total_seconds()) if run.ended_at else None


def _status_text(run: Run) -> str:
    """The cell's text, short enough for the sidebar: "running", how long it took, marked if it failed or is unknown."""
    seconds = _duration_seconds(run)
    took = "" if seconds is None else format_duration(seconds)
    match run.status:
        case None:
            return ""
        case models.RunStatus.RUNNING:
            return "running"
        case models.RunStatus.FINISHED:
            return took
        case models.RunStatus.FAILED:
            return f"✕ {took}".strip()
        case models.RunStatus.UNKNOWN:
            return f"? {took}".strip()


def _status_detail(run: Run) -> str:
    """The cell's tooltip: how the run ended and how long it took, in words. Empty for a run with no status."""
    seconds = _duration_seconds(run)
    after = "" if seconds is None else f" after {format_duration(seconds)}"
    match run.status:
        case None:
            return ""
        case models.RunStatus.RUNNING:
            return "running"
        case models.RunStatus.FINISHED | models.RunStatus.FAILED:
            return f"{run.status.value}{after}"
        case models.RunStatus.UNKNOWN:
            return f"ended without saying how{after}"


NAVBAR_HPARAM_SIGNATURE_ID: StoreId[ids.ExperimentPage] = StoreId("navbar-hparam-signature")

DELETE_RUN_PENDING_STORE_ID: StoreId[ids.ExperimentPage] = StoreId("delete-run-pending")
DELETE_RUN_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("delete-run-modal")
DELETE_RUN_CONFIRM_ID: ButtonId[ids.ExperimentPage] = ButtonId("delete-run-confirm")
DELETE_RUN_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("delete-run-cancel")

NAVBAR_HPARAM_DRAWN_RUNS_ID: StoreId[ids.ExperimentPage] = StoreId("navbar-hparam-drawn-runs")
NAVBAR_RUN_SELECTION_ID: StoreId[ids.ExperimentPage] = StoreId("navbar-run-selection")
"""
The run ids the table's selection holds once it differs from `NAVBAR_HPARAM_DRAWN_RUNS_ID`: what the user chose.

In the experiment page's static layout, not the table's own render tree: a store that mounted with the table would
set `sync_run_selection` off itself, which is the round trip this store exists to avoid.
"""

_HPARAM_COLS_CHANGED_JS = ClientsideScript(Path(__file__).with_name("hparam_cols_changed.js"))
_RUN_SELECTION_GATE_JS = ClientsideScript(Path(__file__).with_name("run_selection_gate.js"))


def _build_hparam_rows(
    runs: list[Run],
    hparams_by_run: dict[int, models.HyperParams],
    latest_metrics: dict[int, dict[str, float]],
) -> list[dict[str, Any]]:
    """One row per run, whether or not it has logged hyperparameters or metrics yet."""
    rows: list[dict[str, Any]] = []
    for run in runs:
        row: dict[str, Any] = {
            MetricColumn.RUN_ID: run.id,
            RUN_NAME_COLUMN: run_display_name(run),
            _STATUS_FIELD: run.status.value if run.status else None,
            _DURATION_FIELD: _duration_seconds(run),
            _STATUS_TEXT_FIELD: _status_text(run),
            _STATUS_DETAIL_FIELD: _status_detail(run),
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
    hparams: list[models.HyperParams],
    selected: list[str],
    runs: list[Run],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    Build displayable rows for `runs`, enriched with hparams and any selected last-step metrics.

    Key *names* are what a picker offers (`summary_hparam_keys`, and a cheap `DISTINCT key` lookup of metrics), plus any
    selected key a run holds, so a column picked from an older run stays applied; metric *values* are only fetched for
    the selected metrics -- the columns the table is actually about to show. Takes `runs` and `hparams` rather than
    querying them itself: `render_navbar_hparams` already needs them to decide *whether* to call this at all (see
    `_render_signature`), on every live-update poll tick that saw a change.
    """
    hparams_by_run = {h.run_id: h for h in hparams}
    held = {key for h in hparams for key in h.hparams_dict}
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
    rows = _build_hparam_rows(runs, hparams_by_run, latest)
    return hparam_keys, metric_keys, rows


def _delete_run_modal() -> Component:
    """
    Confirmation modal for deleting a run.

    Opened by clicking a row's trash icon in the table (see `_DELETE_COLUMN_ID` in
    `_build_hparam_datatable`) -- mirrors the project/experiment delete modals `render_delete_control`
    builds, but doesn't use that helper directly since its confirm target here
    (`DELETE_RUN_PENDING_STORE_ID`) is resolved per-row rather than from a fixed State.
    """
    return dmc.Modal(
        id=DELETE_RUN_MODAL_ID,
        title="Delete this run?",
        opened=False,
        children=[
            dmc.Text(
                "This soft-deletes the run and everything under it. It can be restored from the "
                "admin Trash until it's permanently purged."
            ),
            dmc.Group(
                [
                    dmc.Button("Cancel", id=DELETE_RUN_CANCEL_ID, variant="default"),
                    dmc.Button("Delete", id=DELETE_RUN_CONFIRM_ID, color="red"),
                ],
                justify="flex-end",
                mt="sm",
            ),
        ],
    )


def _build_hparam_datatable(
    rows: list[dict[str, Any]],
    selected: list[str] | list[int],
    shown: frozenset[int],
    *,
    table_id: str,
) -> dag.AgGrid:
    column_defs: list[dict[str, Any]] = [
        {
            "field": RUN_NAME_COLUMN,
            "headerName": "Run",
            "sortable": True,
            "filter": True,
            # Each run's chart color as a dot before its name, so this table doubles as the legend.
            "cellClass": {"function": f"params.data.{_SWATCH_FIELD}"},
        },
        {
            # Sorts by how long the run took; filters (as text) by its status.
            "field": _DURATION_FIELD,
            "headerName": "Status",
            "headerTooltip": "How the run ended, and how long it took",
            "sortable": True,
            "filter": True,
            "filterValueGetter": {"function": f"params.data.{_STATUS_FIELD}"},
            "valueFormatter": {"function": f"params.data.{_STATUS_TEXT_FIELD}"},
            "tooltipField": _STATUS_DETAIL_FIELD,
            # The run name gets what is left, but this has to stay readable ("✕ 1h 2m") in a narrow sidebar.
            "minWidth": _STATUS_MIN_WIDTH,
            "cellStyle": {
                "styleConditions": [
                    {
                        "condition": f"params.data.{_STATUS_FIELD} == '{status.value}'",
                        "style": {"color": color},
                    }
                    for status, color in _STATUS_STYLES.items()
                ]
            },
        },
    ]
    column_defs.extend(column_def(str(key), infer_column_dtype(rows, str(key))) for key in selected)
    column_defs.append(
        {
            "field": _DELETE_COLUMN_ID,
            "headerName": "",
            "sortable": False,
            "filter": False,
            "minWidth": 36,
            "maxWidth": 40,
            "cellClass": icon_cell_class(Icon.DELETE),
            "cellStyle": {"textAlign": "center", "cursor": "pointer", "color": "var(--mantine-color-red-6)"},
        }
    )

    selected_ids = [str(row[MetricColumn.RUN_ID]) for row in rows if row[MetricColumn.RUN_ID] in shown]
    data = [
        {
            **row,
            _ROW_ID_FIELD: str(row[MetricColumn.RUN_ID]),
            _SWATCH_FIELD: series_swatch_class(int(row[MetricColumn.RUN_ID])),
        }
        for row in rows
    ]

    # Sized to fit its rows (header included, and room for the empty-table overlay), up to a cap
    # past which it scrolls -- and since it isn't `autoHeight`, ag-grid then only renders the rows
    # actually in view, so a 500-run sweep costs the DOM what a 15-run one does. No pagination:
    # scrolling a long list is fewer clicks than paging through it.
    grid = themed_grid_kwargs()
    grid["className"] = f"{grid['className']} dl-run-table"
    grid["style"] = {
        **grid["style"],
        # Less than ag-grid's default padding a side: in a sidebar this narrow it is a third of every column.
        "--ag-cell-horizontal-padding": "8px",
        "height": f"min({(max(len(rows), 2) + 1) * _ROW_HEIGHT + 2}px, {_MAX_TABLE_HEIGHT})",
    }
    return dag.AgGrid(
        id=table_id,
        columnDefs=column_defs,
        rowData=data,
        # A dedicated already-stringified field, not `params.data.run_id` directly -- dash-ag-grid's
        # string-prop JS parser only supports bare expressions (no function calls like `String(...)`),
        # so casting has to happen on the Python side instead.
        getRowId=f"params.data.{_ROW_ID_FIELD}",
        # `getRowId` (and the numeric columns' `valueFormatter`, via `column_def`) are JS expression
        # strings -- dash-ag-grid silently no-ops any string-valued JS prop unless this is set. Safe
        # here: every such string is static and written by us, never derived from user/request data.
        dangerously_allow_code=True,
        selectedRows={"ids": selected_ids},
        columnSize="responsiveSizeToFit",
        dashGridOptions={
            "rowHeight": _ROW_HEIGHT,
            "headerHeight": _ROW_HEIGHT,
            "rowSelection": {
                "mode": "multiRow",
                "checkboxes": True,
                "headerCheckbox": True,
                # With a filter on, the header checkbox is the runs it matches: every run of a long experiment is
                # one click from being drawn, and so should never be what that click means.
                "selectAll": "filtered",
            },
        },
        **grid,
    )


def _render_hparam_panel(
    rows: list[dict[str, Any]],
    hparam_keys: list[str],
    metric_keys: list[str],
    selected: list[str],
    selection: _Selection,
) -> Component:
    """
    The comparison table, plus a Columns picker collapsed behind an icon so it doesn't permanently eat a row above the table it configures.

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
                    compare_button(),
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
            *(
                [
                    dmc.Group(
                        [
                            dmc.Text("Runs selected in the table", size="xs", c="dimmed"),
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
            ),
            html.Div(
                id=NAVBAR_HPARAM_TABLE_BODY_ID,
                children=_build_hparam_datatable(
                    rows,
                    selected,
                    selection.shown,
                    table_id=NAVBAR_HPARAM_DATATABLE_ID,
                ),
            ),
        ]
    )


class _Selection(typing.NamedTuple):
    """The runs the charts draw, and whether a view selected them (rather than their being the newest)."""

    shown: frozenset[int]
    chosen: bool
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
    store: DataStore[...], experiment_id: int, ref: repo.PageRef
) -> tuple[list[Run], _Selection, list[str]]:
    """
    The cheap, always-needed inputs for the navbar table: its run list and its persisted settings.

    Deliberately doesn't touch metric keys/values (`_load_hparam_view_data`, the expensive part) --
    `render_navbar_hparams` needs exactly this much to decide *whether* a re-render is even
    happening before paying for that, since it's re-evaluated on every live-update poll tick, not
    just on a real settings change.
    """
    page = repo.load_page(store, ref)
    shown, columns = shown_run_ids(store, experiment_id, page.document), page.document.hparam_columns
    runs = list(store.get_runs(experiment_id, limit=None, run_ids=None))
    chosen = page.document.selected_runs is not None
    selection = _Selection(shown, chosen=chosen, newer_hidden=_newer_hidden(runs, shown) if chosen else 0)
    return runs, selection, columns


def _render_signature(
    experiment_id: int,
    hparams: list[models.HyperParams],
    selection: _Selection,
    selected: list[str],
    runs: list[Run],
) -> str:
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
    run_states = [[run.id, run.status, run.ended_at.isoformat() if run.ended_at else None] for run in runs]
    signed = [
        experiment_id,
        [[h.run_id, h.hparams_dict] for h in sorted(hparams, key=lambda h: h.run_id)],
        sorted(selection.shown),
        selection.chosen,
        selection.newer_hidden,
        selected,
        run_states,
    ]
    return hashlib.sha256(json.dumps(signed, sort_keys=True, default=str).encode()).hexdigest()


def _register_hparam_table(app: Dash) -> None:
    @app.callback(
        Output(constants.NAVBAR_RUN_LIST_ID, "children"),
        # The experiment's revision, which the live poll moves on: what the table holds may have changed (#454).
        Input(STATE_LAST_KNOWN_REVISION, "data"),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
        Input(ids.STATE_PAGE_STORAGE, "data", allow_optional=True),
        State(NAVBAR_HPARAM_SIGNATURE_ID, "data", allow_optional=True),
        State(ids.STATE_VIEW_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def render_navbar_hparams(
        _revision: int | None,
        experiment_id: int | None,
        page_json: str | None,
        prev_signature: str | None,
        view_id: int | None,
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
        runs, selection, selected = _load_hparam_panel_settings(store, experiment_id, ref)
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
        signature = _render_signature(experiment_id, hparams, selection, selected, runs)
        if signature == prev_signature:
            raise PreventUpdate

        hparam_keys, metric_keys, rows = _load_hparam_view_data(store, experiment_id, hparams, selected, runs)

        return dmc.Stack(
            [
                _render_hparam_panel(rows, hparam_keys, metric_keys, selected, selection),
                _delete_run_modal(),
                Store(id=DELETE_RUN_PENDING_STORE_ID),
                Store(id=NAVBAR_HPARAM_SIGNATURE_ID, data=signature),
                Store(id=NAVBAR_HPARAM_DRAWN_RUNS_ID, data=sorted(selection.shown)),
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

    # The table's `selectedRows` is *computed from* the runs the page currently shows (see
    # `_build_hparam_datatable`), so ag-grid reports it on mount -- twice -- though nobody chose
    # anything. Dash fires a callback on a dynamically-created component mounting with a value even
    # with `prevent_initial_call=True` (that only suppresses the very first page render), and each
    # one would be a server round trip that ends in `PreventUpdate`. So the browser compares the
    # selection with the runs drawn first, and only a real change reaches `sync_run_selection`.
    app.clientside_callback(
        _RUN_SELECTION_GATE_JS.source,
        Output(NAVBAR_RUN_SELECTION_ID, "data"),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "selectedRows", allow_optional=True),
        State(NAVBAR_HPARAM_DRAWN_RUNS_ID, "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        Input(NAVBAR_RUN_SELECTION_ID, "data", allow_optional=True),
        State(NAVBAR_HPARAM_DATATABLE_ID, "rowData", allow_optional=True),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def sync_run_selection(
        selection: list[int] | None,
        table_data: list[dict[str, Any]] | None,
        experiment_id: int,
        page_json: str,
        paging_json: str,
    ) -> tuple[Any, ...]:
        if selection is None or table_data is None:
            raise PreventUpdate
        selected_ids = set(selection)

        store = get_data_store()
        # Still checked here: the browser compared against the runs drawn when the table was built, and
        # the page may have changed since. `ref_of`, not `STATE_VIEW_ID` -- see `render_navbar_hparams`'s
        # own comment on why.
        ref = repo.ref_of(page_model.BasicExperimentPage.model_validate_json(page_json))
        current_page = repo.load_page(store, ref)
        listed = {row[MetricColumn.RUN_ID] for row in table_data}
        if selected_ids == shown_run_ids(store, experiment_id, current_page.document) & listed:
            raise PreventUpdate

        cap = ShownRunsSettings().max_shown_runs
        if len(selected_ids) > cap:
            # Refused: the page keeps the runs it drew. The table still shows the larger selection, so the notice says so.
            notice = {
                "action": "show",
                "id": f"run-cap-{len(selected_ids)}",
                "title": "Too many runs to draw",
                "message": f"A view draws at most {cap} runs. Deselect some to change what the charts draw.",
                "color": "yellow",
                "autoClose": 8_000,
            }
            return no_update, no_update, [notice]

        page, container = edits.commit(
            page_json,
            paging_json,
            # A selection replaces what a view left out before selections existed, so that is cleared with it.
            edits.PageEdit(
                document=page_model.DocumentSettings(selected_runs=sorted(selected_ids), excluded_runs=[]),
            ),
        )
        return page.model_dump_json(), container, no_update


def _register_newest_runs(app: Dash) -> None:
    @app.callback(
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Input(NAVBAR_NEWEST_RUNS_ID, "n_clicks", allow_optional=True),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def show_newest_runs(n_clicks: int | None, page_json: str, paging_json: str) -> tuple[str, html.Div]:
        if not n_clicks:
            raise PreventUpdate
        ref = repo.ref_of(page_model.BasicExperimentPage.model_validate_json(page_json))
        if repo.load_page(get_data_store(), ref).document.selected_runs is None:
            raise PreventUpdate
        page, container = edits.commit(
            page_json,
            paging_json,
            edits.PageEdit(document=page_model.DocumentSettings(selected_runs=None, excluded_runs=[])),
        )
        return page.model_dump_json(), container


class _CellClicked(BaseModel, frozen=True, extra="ignore"):
    """The cell of the run table ag-grid says was clicked: its column, and its row (a run id). It says more, unread."""

    column: str = Field(alias="colId")
    row: int = Field(alias="rowId")


def _register_delete_run(app: Dash) -> None:
    @app.callback(
        Output(DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Output(DELETE_RUN_PENDING_STORE_ID, "data"),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "cellClicked", allow_optional=True),
        prevent_initial_call=True,
    )
    def open_delete_run_modal(cell_clicked: dict[str, Any] | None) -> tuple[bool, int]:
        if not cell_clicked:
            raise PreventUpdate
        clicked = _CellClicked.model_validate(cell_clicked)
        if clicked.column != _DELETE_COLUMN_ID:
            raise PreventUpdate
        return True, clicked.row

    @app.callback(
        Output(DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_RUN_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_run(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Output(DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_RUN_CONFIRM_ID, "n_clicks"),
        State(DELETE_RUN_PENDING_STORE_ID, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def confirm_delete_run(n_clicks: int, run_id: int | None, experiment_id: int) -> tuple[str, bool, bool]:
        if not n_clicks or run_id is None:
            raise PreventUpdate
        store = get_data_store()
        store.delete_run(run_id, get_current_user())
        return relative_path(f"/experiment/{experiment_id}"), True, False


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
    _register_delete_run(app)
    _register_column_search(app)
