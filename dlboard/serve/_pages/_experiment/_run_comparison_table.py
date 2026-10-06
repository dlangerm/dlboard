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
from dash import Dash, Input, Output, State, html
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.models import RUN_NAME_COLUMN
from dlboard.plugins.charts._table_style import column_def, infer_column_dtype, themed_grid_kwargs
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
from dlboard.serve._pages._dash_helpers import section_label, tooltipped_action_icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment import _dataframe_helpers as dfh
from dlboard.serve._pages._experiment import _experiment_page_state as core
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore, Run

_log = get_logger(__name__)

NAVBAR_HPARAM_DATATABLE_ID: ValueId[core.ExperimentPage] = ValueId("navbar-hparam-datatable")
NAVBAR_HPARAM_COL_SELECT_ID: ValueId[core.ExperimentPage] = ValueId("navbar-hparam-col-select")
NAVBAR_HPARAM_TABLE_BODY_ID: typing.Final = "navbar-hparam-table-body"
NAVBAR_HPARAM_CONFIRM_COLS_ID: ButtonId[core.ExperimentPage] = ButtonId("navbar-hparam-confirm-cols")
NAVBAR_HPARAM_COLUMNS_TOGGLE_ID: ButtonId[core.ExperimentPage] = ButtonId("navbar-hparam-columns-toggle")
NAVBAR_HPARAM_APPLIED_COLS_ID: StoreId[core.ExperimentPage] = StoreId("navbar-hparam-applied-cols")
_ROW_HEIGHT = 34
_MAX_TABLE_HEIGHT = "50vh"
_DELETE_COLUMN_ID = "_delete"
_ROW_ID_FIELD = "_row_id"
_SWATCH_FIELD = "_swatch"

SELECTED_HPARAM_COLS_KEY: typing.Final = "hparam-table-selected"  # a page_settings dict key

NAVBAR_HPARAM_SIGNATURE_ID: StoreId[core.ExperimentPage] = StoreId("navbar-hparam-signature")

DELETE_RUN_PENDING_STORE_ID: StoreId[core.ExperimentPage] = StoreId("delete-run-pending")
DELETE_RUN_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("delete-run-modal")
DELETE_RUN_CONFIRM_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-run-confirm")
DELETE_RUN_CANCEL_ID: ButtonId[core.ExperimentPage] = ButtonId("delete-run-cancel")

_HPARAM_COLS_CHANGED_JS = ClientsideScript(Path(__file__).with_name("hparam_cols_changed.js"))


def _build_hparam_rows(
    runs: list[Run],
    hparams_by_run: dict[int, models.HyperParams],
    latest_metrics: dict[int, dict[str, float]],
) -> list[dict[str, Any]]:
    """One row per run, whether or not it has logged hyperparameters or metrics yet."""
    rows: list[dict[str, Any]] = []
    for run in runs:
        row: dict[str, Any] = {"run_id": run.id, RUN_NAME_COLUMN: run_display_name(run)}
        hparam = hparams_by_run.get(run.id)
        if hparam is not None:
            row.update(hparam.hparams_dict)
        row.update(latest_metrics.get(run.id, {}))
        rows.append(row)
    return rows


_HPARAM_ROWS_LIMIT = 1000
"""Cap on runs loaded into the hparam/metric comparison table -- a page, not every row, matching
the discipline every other `list_*` `DataStore` method already follows."""


def _load_hparam_view_data(
    store: DataStore[...], experiment_id: int, hparams: list[str], selected_metrics: set[str], runs: list[Run]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    Build displayable rows for `runs`, enriched with hparams and any selected last-step metrics.

    Metric *names* come from a cheap `DISTINCT key` lookup (for the Columns picker); metric
    *values* are only fetched for `selected_metrics` -- the columns the table is actually about to
    show. Fetching every metric ever logged in the experiment just to populate a table nobody's
    added a metric column to yet is the difference between this loading instantly and taking
    seconds once an experiment has any real volume of logged steps. Takes `runs` rather than
    querying them itself: `render_navbar_hparams` already needs that query to decide *whether* to
    call this at all (see `_render_signature`), and this is also the callback that fires on every
    live-update poll tick -- querying twice would mean paying for it even on the many ticks that
    end up not calling this function at all.
    """
    hydrated = [models.HyperParams.model_validate_json(run) for run in hparams]
    hparams_by_run = {h.run_id: h for h in hydrated}
    hparam_keys = sorted(set(itertools.chain(*[list(k.hparams_dict.keys()) for k in hydrated])))
    metric_keys = [s.key for s in store.summarize_metric_keys(experiment_id)]
    wanted = frozenset(selected_metrics & set(metric_keys))
    latest = store.fetch_metrics(experiment_id, keys=wanted).latest_per_run() if wanted else {}
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
    excluded: list[int] | list[str],
    *,
    table_id: str,
) -> dag.AgGrid:
    column_defs: list[dict[str, Any]] = [
        {
            "field": RUN_NAME_COLUMN,
            "headerName": "Run",
            "sortable": True,
            # Each run's chart color as a dot before its name, so this table doubles as the legend.
            "cellClass": {"function": f"params.data.{_SWATCH_FIELD}"},
        }
    ]
    column_defs.extend(column_def(str(key), infer_column_dtype(rows, str(key))) for key in selected)
    column_defs.append(
        {
            "field": _DELETE_COLUMN_ID,
            "headerName": "",
            "sortable": False,
            "filter": False,
            "width": 40,
            "cellClass": icon_cell_class(Icon.DELETE),
            "cellStyle": {"textAlign": "center", "cursor": "pointer", "color": "var(--mantine-color-red-6)"},
        }
    )

    selected_ids = [str(row["run_id"]) for row in rows if row["run_id"] not in excluded]
    data = [
        {**row, _ROW_ID_FIELD: str(row["run_id"]), _SWATCH_FIELD: series_swatch_class(int(row["run_id"]))}
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
            "rowSelection": {"mode": "multiRow", "checkboxes": True, "headerCheckbox": True},
        },
        **grid,
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
                                            data=[
                                                {"group": "Hyperparameters", "items": hparam_keys},
                                                {"group": "Metrics (latest)", "items": metric_keys},
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
                justify="space-between",
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
                ),
            ),
        ]
    )


def _load_hparam_panel_settings(
    store: DataStore[...], ref: core.PageRef
) -> tuple[list[Run], list[int] | list[str], list[str]]:
    """
    The cheap, always-needed inputs for the navbar table: its run list and its persisted settings.

    Deliberately doesn't touch metric keys/values (`_load_hparam_view_data`, the expensive part) --
    `render_navbar_hparams` needs exactly this much to decide *whether* a re-render is even
    happening before paying for that, since it's re-evaluated on every live-update poll tick, not
    just on a real settings change.
    """
    page = core.load_page(store, ref)
    excluded = page.page_settings.get(dfh.EXCLUDED_RUNS_KEY, [])
    assert isinstance(excluded, list)
    selected_setting = page.page_settings.get(SELECTED_HPARAM_COLS_KEY, [])
    assert isinstance(selected_setting, list)
    selected = [s for s in selected_setting if isinstance(s, str)]
    runs = list(store.get_runs(ref.experiment_id, limit=_HPARAM_ROWS_LIMIT, offset=0))
    return runs, excluded, selected


def _render_signature(
    experiment_id: int,
    hparams: list[str],
    excluded: list[int] | list[str],
    selected: list[str],
    run_ids: list[int],
) -> list[Any]:
    """
    Everything `render_navbar_hparams`'s output actually depends on, as one comparable value.

    Stored in (and read back from) `NAVBAR_HPARAM_SIGNATURE_ID`'s `dcc.Store`, so it round-trips
    through JSON either way -- a typed structure here couldn't stay typed past that store anyway,
    but naming every tracked field in one place (rather than as an inline list literal at the call
    site) means a future addition to what this table renders from has one obvious place to also add
    it, instead of a silently-still-passing equality check against a signature nobody remembered to
    extend. `run_ids` (not just `hparams`) must be included: a run with no hyperparameters yet still
    needs to appear the moment `store.get_runs` (queried fresh in `_load_hparam_panel_settings`,
    *before* this signature is even built) reports it, even though `hparams` itself hasn't changed.
    """
    return [experiment_id, hparams, sorted(excluded), selected, run_ids]


def _register_hparam_table(app: Dash) -> None:
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.NAVBAR_RUN_LIST_ID, "children"),
        Input(core.STATE_HPARAMS, "data"),
        Input(constants.STATE_EXPERIMENT_ID, "data", allow_optional=True),
        Input(core.STATE_PAGE_STORAGE, "data", allow_optional=True),
        State(NAVBAR_HPARAM_SIGNATURE_ID, "data", allow_optional=True),
        State(core.STATE_VIEW_ID, "data", allow_optional=True),
        prevent_initial_call=True,
    )
    def render_navbar_hparams(
        hparams: list[str],
        experiment_id: int | None,
        page_json: str | None,
        prev_signature: list[Any] | None,
        view_id: int | None,
    ) -> dmc.Stack:
        if experiment_id is None:
            raise PreventUpdate

        store = get_data_store()
        # `page_json` (this callback's own `STATE_PAGE_STORAGE` trigger) is the freshest known page,
        # ahead of `STATE_VIEW_ID` when a structural edit just branched into a new view of its own
        # (`_experiment_page_state.save_page`) -- reading `view_id` instead here would still find
        # the page that edit branched *away* from, since `sync_view_after_edit` only catches
        # `STATE_VIEW_ID` up to it a separate, slightly later round trip.
        ref = (
            core.ref_of(experiment_id, core.BasicExperimentPage.model_validate_json(page_json))
            if page_json is not None
            else core.PageRef(experiment_id, view_id)
        )
        runs, excluded, selected = _load_hparam_panel_settings(store, ref)
        # `STATE_PAGE_STORAGE` changes on *every* page-settings write -- a tab switch, a panel
        # rename, a drag-reorder, none of which this table cares about -- so rebuilding on it
        # unconditionally (tearing down and remounting the ag-grid) flickered on every one of
        # those, not just an actual run-selection/column change. Skip the (expensive, chart-
        # remounting) rebuild unless the specific settings this table renders actually changed --
        # see `_render_signature` for exactly what "changed" is measured against.
        #
        # Checked *before* `_load_hparam_view_data` below (not after): `STATE_HPARAMS` is rewritten
        # on every live-update poll tick that finds *any* change in the experiment, metrics
        # included -- so this callback re-fires roughly as often as metrics get flushed during
        # active training, not just when a run/hparam/column-selection actually changes.
        # `_load_hparam_view_data`'s `summarize_metric_keys` scan and (if a metric column is selected)
        # full metric-history refetch would otherwise run on every one of those ticks only to
        # almost always end in the `PreventUpdate` below anyway -- a cost every open experiment
        # page would pay constantly during training, not just the ones actually using the
        # metric-column feature. `runs` (from `_load_hparam_panel_settings`, already cheap/bounded)
        # is enough to detect every case this table needs to react to, so the expensive part only
        # runs on the (comparatively rare) ticks that actually need a re-render.
        signature = _render_signature(experiment_id, hparams, excluded, selected, [r.id for r in runs])
        if signature == prev_signature:
            raise PreventUpdate

        hparam_keys, metric_keys, rows = _load_hparam_view_data(
            store, experiment_id, hparams, set(selected), runs
        )

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
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def persist_selected_hparam_cols(
        n_clicks: int | None, selected: list[str] | None, experiment_id: int, page_json: str
    ) -> str:
        if not n_clicks:
            raise PreventUpdate
        store = get_data_store()
        # `ref_of`, not `STATE_VIEW_ID` -- see `render_navbar_hparams`'s own comment on why.
        curr_page = core.BasicExperimentPage.model_validate_json(page_json)
        page = core.persist_settings(
            store, core.ref_of(experiment_id, curr_page), {SELECTED_HPARAM_COLS_KEY: selected or []}
        )
        return page.model_dump_json()

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(core.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(core.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Input(NAVBAR_HPARAM_DATATABLE_ID, "selectedRows", allow_optional=True),
        State(NAVBAR_HPARAM_DATATABLE_ID, "rowData", allow_optional=True),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def sync_run_selection(
        selected_rows: list[dict[str, Any]] | dict[str, Any] | None,
        table_data: list[dict[str, Any]] | None,
        experiment_id: int,
        page_json: str,
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
        # remounting) rebuild when the recomputed set matches what's already persisted. `ref_of`,
        # not `STATE_VIEW_ID` -- see `render_navbar_hparams`'s own comment on why.
        ref = core.ref_of(experiment_id, core.BasicExperimentPage.model_validate_json(page_json))
        current_page = core.load_page(store, ref)
        currently_excluded = current_page.page_settings.get(dfh.EXCLUDED_RUNS_KEY, [])
        if excluded == currently_excluded:
            raise PreventUpdate

        page, container = core.persist_settings_and_rerender(
            store,
            ref,
            {dfh.EXCLUDED_RUNS_KEY: excluded},
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
        store.delete_run(run_id, get_current_user())
        return relative_path(f"/experiment/{experiment_id}"), True, False


def register_run_comparison_callbacks(app: Dash) -> None:
    _register_hparam_table(app)
    _register_delete_run(app)
