"""
The navbar's run-comparison table: a Columns picker plus a run-selection/deletion data table.

Fully self-contained -- it renders into `constants.NAVBAR_RUN_LIST_ID`, a different container from
`accordion_view`'s `METRIC_CONTENT_ID` tree, so unlike the other controller modules it needs no
static shell declared in `_experiment_page_state.py`.
"""

from __future__ import annotations

import itertools
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
import pandas as pd
from dash import Dash, Input, Output, State, html
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.models import ButtonId, ModalId, StoreId, ValueId, constants
from dltrack.plugins.charts._table_style import column_def, infer_column_dtype, themed_grid_kwargs
from dltrack.serve import ClientsideScript, get_current_user, get_data_store
from dltrack.serve._pages._dash_helpers import tooltipped_action_icon
from dltrack.serve._pages._experiment import _dataframe_helpers as dfh
from dltrack.serve._pages._experiment import _experiment_page_state as core

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import DataStore, Run

_log = get_logger(__name__)

NAVBAR_HPARAM_DATATABLE_ID: ValueId[core.ExperimentPage] = ValueId("navbar-hparam-datatable")
NAVBAR_HPARAM_COL_SELECT_ID: ValueId[core.ExperimentPage] = ValueId("navbar-hparam-col-select")
NAVBAR_HPARAM_TABLE_BODY_ID: typing.Final = "navbar-hparam-table-body"
NAVBAR_HPARAM_CONFIRM_COLS_ID: ButtonId[core.ExperimentPage] = ButtonId("navbar-hparam-confirm-cols")
NAVBAR_HPARAM_COLUMNS_TOGGLE_ID: ButtonId[core.ExperimentPage] = ButtonId("navbar-hparam-columns-toggle")
NAVBAR_HPARAM_APPLIED_COLS_ID: StoreId[core.ExperimentPage] = StoreId("navbar-hparam-applied-cols")
_NAVBAR_HPARAM_PAGE_SIZE = 8
_DELETE_COLUMN_ID = "_delete"
_ROW_ID_FIELD = "_row_id"

_METRIC_BOOKKEEPING_COLS = {"run_id", "step", "index", "timestamp_utc", "experiment_id"}

SELECTED_HPARAM_COLS_KEY: typing.Final = "hparam-table-selected"  # a page_settings dict key

NAVBAR_HPARAM_SIGNATURE_ID: StoreId[core.ExperimentPage] = StoreId("navbar-hparam-signature")

DELETE_RUN_PENDING_STORE_ID: StoreId[core.ExperimentPage] = StoreId("delete-run-pending")
DELETE_RUN_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("delete-run-modal")
DELETE_RUN_CONFIRM_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-run-confirm")
DELETE_RUN_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-run-cancel")

_HPARAM_COLS_CHANGED_JS = ClientsideScript(Path(__file__).with_name("hparam_cols_changed.js"))


def _fetch_last_step_metrics(
    store: DataStore[...], experiment_id: int, metric_name_match: set[str]
) -> dict[int, dict[str, Any]]:
    """One row per run: `metric_name_match`'s values at that run's highest logged step."""
    if not metric_name_match:
        return {}
    df = dfh.build_metrics_dataframe(store.fetch_metrics(experiment_id, metric_name_match=metric_name_match))
    if df.empty:
        return {}
    last_rows = df.loc[df.groupby("run_id")["step"].idxmax()]
    metric_cols = [c for c in df.columns if c not in _METRIC_BOOKKEEPING_COLS]
    return {
        int(row["run_id"]): {c: row[c] for c in metric_cols if pd.notna(row[c])}
        for _, row in last_rows.iterrows()
    }


def _build_hparam_rows(
    runs: list[Run],
    hparams_by_run: dict[int, models.HyperParams],
    last_step_metrics: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per run, whether or not it has logged hyperparameters or metrics yet."""
    rows: list[dict[str, Any]] = []
    for run in runs:
        row: dict[str, Any] = {"run_id": run.id, "run_name": run.name or f"Run {run.id}"}
        hparam = hparams_by_run.get(run.id)
        if hparam is not None:
            row.update(hparam.hparams_dict)
        row.update(last_step_metrics.get(run.id, {}))
        rows.append(row)
    return rows


_HPARAM_ROWS_LIMIT = 1000
"""Cap on runs loaded into the hparam/metric comparison table -- a page, not every row, matching
the discipline every other `list_*` `DataStore` method already follows."""


def _load_hparam_view_data(
    store: DataStore[...], experiment_id: int, hparams: list[str], selected_metrics: set[str]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    Load every run for the experiment, enriched with its hparams and any selected last-step metrics.

    Metric *names* come from a cheap `DISTINCT key` lookup (for the Columns picker); metric
    *values* are only fetched for `selected_metrics` -- the columns the table is actually about to
    show. Fetching every metric ever logged in the experiment just to populate a table nobody's
    added a metric column to yet is the difference between this loading instantly and taking
    seconds once an experiment has any real volume of logged steps.
    """
    hydrated = [models.HyperParams.model_validate_json(run) for run in hparams]
    hparams_by_run = {h.run_id: h for h in hydrated}
    hparam_keys = sorted(set(itertools.chain(*[list(k.hparams_dict.keys()) for k in hydrated])))
    metric_keys = store.list_metric_keys(experiment_id)
    last_step_metrics = _fetch_last_step_metrics(store, experiment_id, selected_metrics & set(metric_keys))
    runs = list(store.get_runs(experiment_id, limit=_HPARAM_ROWS_LIMIT, offset=0))
    rows = _build_hparam_rows(runs, hparams_by_run, last_step_metrics)
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
    excluded: list[int] | list[str],
    *,
    table_id: str,
    page_size: int,
) -> dag.AgGrid:
    column_defs: list[dict[str, Any]] = [{"field": "run_name", "headerName": "Run", "sortable": True}]
    column_defs.extend(column_def(str(key), infer_column_dtype(rows, str(key))) for key in selected)
    column_defs.append(
        {
            "field": _DELETE_COLUMN_ID,
            "headerName": "",
            "sortable": False,
            "filter": False,
            "width": 40,
            "cellStyle": {"textAlign": "center", "cursor": "pointer", "color": "var(--mantine-color-red-6)"},
        }
    )

    selected_ids = [str(row["run_id"]) for row in rows if row["run_id"] not in excluded]
    data = [{**row, _DELETE_COLUMN_ID: "🗑", _ROW_ID_FIELD: str(row["run_id"])} for row in rows]

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
            "pagination": True,
            "paginationPageSize": page_size,
            "paginationPageSizeSelector": sorted({page_size, 20, 50, 100}),
            "domLayout": "autoHeight",
            "rowSelection": {"mode": "multiRow", "checkboxes": True, "headerCheckbox": True},
        },
        **themed_grid_kwargs(),
    )


def _render_hparam_panel(
    rows: list[dict[str, Any]],
    hparam_keys: list[str],
    metric_keys: list[str],
    selected: list[str],
    excluded: list[int] | list[str],
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
                    dmc.Text("Runs", size="xs", fw=600, style={"flex": 1}),
                    dmc.Popover(
                        [
                            dmc.PopoverTarget(
                                tooltipped_action_icon(
                                    "▤",
                                    component_id=NAVBAR_HPARAM_COLUMNS_TOGGLE_ID,
                                    label="Choose columns",
                                )
                            ),
                            dmc.PopoverDropdown(
                                dmc.Group(
                                    [
                                        dmc.MultiSelect(
                                            id=NAVBAR_HPARAM_COL_SELECT_ID,
                                            data=[
                                                {"group": "Hyperparameters", "items": hparam_keys},
                                                {"group": "Metrics (last step)", "items": metric_keys},
                                            ],
                                            value=applied,
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
                gap="xs",
                wrap="nowrap",
            ),
            html.Div(
                id=NAVBAR_HPARAM_TABLE_BODY_ID,
                children=_build_hparam_datatable(
                    rows,
                    selected,
                    excluded,
                    table_id=NAVBAR_HPARAM_DATATABLE_ID,
                    page_size=_NAVBAR_HPARAM_PAGE_SIZE,
                ),
            ),
        ]
    )


def _load_hparam_panel_data(
    store: DataStore[...], experiment_id: int, hparams: list[str]
) -> tuple[list[str], list[str], list[dict[str, Any]], list[int] | list[str], list[str]]:
    page = store.get_or_create_page(core.BasicExperimentPage, experiment_id=experiment_id)
    excluded = page.page_settings.get(dfh.EXCLUDED_RUNS_KEY, [])
    assert isinstance(excluded, list)
    selected_setting = page.page_settings.get(SELECTED_HPARAM_COLS_KEY, [])
    assert isinstance(selected_setting, list)
    selected = [s for s in selected_setting if isinstance(s, str)]

    hparam_keys, metric_keys, rows = _load_hparam_view_data(store, experiment_id, hparams, set(selected))
    return hparam_keys, metric_keys, rows, excluded, selected


def _register_hparam_table(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.NAVBAR_RUN_LIST_ID, "children"),
        Input(core.STATE_HPARAMS, "data"),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
        Input(core.STATE_PAGE_STORAGE, "data", allow_optional=True),
        State(NAVBAR_HPARAM_SIGNATURE_ID, "data", allow_optional=True),
        prevent_initial_callback=True,
    )
    def render_navbar_hparams(
        hparams: list[str],
        experiment_id: int | None,
        _page_json: str | None,
        prev_signature: list[Any] | None,
    ) -> dmc.Stack:
        if experiment_id is None:
            raise PreventUpdate

        store = get_data_store()
        hparam_keys, metric_keys, rows, excluded, selected = _load_hparam_panel_data(
            store, experiment_id, hparams
        )
        # `STATE_PAGE_STORAGE` changes on *every* page-settings write -- a tab switch, a panel
        # rename, a drag-reorder, none of which this table cares about -- so rebuilding on it
        # unconditionally (tearing down and remounting the ag-grid) flickered on every one of
        # those, not just an actual run-selection/column change. Skip the (expensive, chart-
        # remounting) rebuild unless the specific settings this table renders actually changed.
        signature = [experiment_id, hparams, sorted(excluded), selected]
        if signature == prev_signature:
            raise PreventUpdate

        return dmc.Stack(
            [
                _render_hparam_panel(rows, hparam_keys, metric_keys, selected, excluded),
                _delete_run_modal(),
                Store(id=DELETE_RUN_PENDING_STORE_ID),
                Store(id=NAVBAR_HPARAM_SIGNATURE_ID, data=signature),
            ],
            gap="xs",
            p="xs",
        )

    # The Apply button only makes sense once the picked columns diverge from what's applied --
    # comparing client-side (rather than round-tripping through a server callback per keystroke)
    # keeps this instant and avoids yet another spurious-rerender source.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _HPARAM_COLS_CHANGED_JS.source,
        Output(NAVBAR_HPARAM_CONFIRM_COLS_ID, "style"),
        Input(NAVBAR_HPARAM_COL_SELECT_ID, "value"),
        State(NAVBAR_HPARAM_APPLIED_COLS_ID, "data"),
        prevent_initial_call=True,
    )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(NAVBAR_HPARAM_CONFIRM_COLS_ID, "n_clicks", allow_optional=True),
        State(NAVBAR_HPARAM_COL_SELECT_ID, "value", allow_optional=True),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def persist_selected_hparam_cols(
        n_clicks: int | None, selected: list[str] | None, experiment_id: int
    ) -> str:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        page = core.persist_settings(store, experiment_id, {SELECTED_HPARAM_COLS_KEY: selected or []})
        return page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "selectedRows", allow_optional=True),
        State(NAVBAR_HPARAM_DATATABLE_ID, "rowData", allow_optional=True),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.FULL_DF_STORE_ID, "data", allow_optional=True),
        State(core.COLUMN_KINDS_STORE_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def sync_run_selection(
        selected_rows: list[dict[str, Any]] | dict[str, Any] | None,
        table_data: list[dict[str, Any]] | None,
        experiment_id: int,
        full_df_json: str | None,
        column_kinds: dict[str, str] | None,
    ) -> tuple[str, html.Div]:
        if selected_rows is None or table_data is None:
            raise PreventUpdate
        # `_build_hparam_datatable` seeds `selectedRows` with the `{"ids": [...]}` shorthand --
        # ag-grid echoes that same shorthand back on mount (before any real user selection), only
        # switching to a list of full row objects once the user actually (de)selects a row.
        selected_ids = (
            {int(i) for i in selected_rows["ids"]}
            if isinstance(selected_rows, dict)
            else {row["run_id"] for row in selected_rows}
        )
        all_ids = {row["run_id"] for row in table_data}
        excluded = sorted(all_ids - selected_ids)

        store = get_data_store()
        # The table's `selectedRows` is *computed from* the currently-persisted `excluded` set
        # (see `_build_hparam_datatable`), so it mounting for the first time reports a "change"
        # here even though nothing the user did actually changed anything -- Dash fires this even
        # with `prevent_initial_call=True`, because that only suppresses the very first page
        # render, not a dynamically-created component (this navbar table isn't in the static
        # layout) mounting later with an already-computed value. Skip the (expensive, chart-
        # remounting) rebuild when the recomputed set matches what's already persisted.
        current_page = store.get_or_create_page(core.BasicExperimentPage, experiment_id=experiment_id)
        currently_excluded = current_page.page_settings.get(dfh.EXCLUDED_RUNS_KEY, [])
        if excluded == currently_excluded:
            raise PreventUpdate

        page, container = core.persist_settings_and_rerender(
            store,
            experiment_id,
            {dfh.EXCLUDED_RUNS_KEY: excluded},
            view_state=core.EditViewState(
                full_df_json=full_df_json,
                column_kinds=column_kinds,
            ),
        )
        return page.model_dump_json(), container


def _register_delete_run(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Output(DELETE_RUN_PENDING_STORE_ID, "data"),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "cellClicked", allow_optional=True),
        prevent_initial_call=True,
    )
    def open_delete_run_modal(cell_clicked: dict[str, Any] | None) -> tuple[bool, int]:
        if not cell_clicked or cell_clicked["colId"] != _DELETE_COLUMN_ID:
            raise PreventUpdate
        return True, int(cast("str", cell_clicked["rowId"]))

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(DELETE_RUN_MODAL_ID, "opened", allow_duplicate=True),
        Input(DELETE_RUN_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_delete_run(n_clicks: int) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
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
        store.delete_run(run_id, get_current_user(store))
        return f"/experiment/{experiment_id}", True, False


def register_run_comparison_callbacks(app: Dash) -> None:
    _register_hparam_table(app)
    _register_delete_run(app)
