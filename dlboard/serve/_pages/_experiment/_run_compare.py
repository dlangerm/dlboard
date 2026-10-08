"""
The run-compare modal: an A/B view of up to `MAX_COMPARED_RUNS` runs' hyperparameters and latest metrics.

Opened by the "Compare" button above the navbar's run table. Everything the modal shows -- which
runs, a diff or the full list, the search text -- is mirrored into the page URL (`CompareQuery`),
so copying the address bar shares exactly what the viewer sees, and landing on such a link opens
the modal already filled in. The first picked run is the baseline: every other run's cell that
differs from it is highlighted.

Fully self-contained like `_run_comparison_table.py`; the one hook into the page is `compare_components`,
which the experiment layout places beside the notes drawer.
"""

from __future__ import annotations

import typing
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from dlboard.plugins.charts._table_style import themed_grid_kwargs
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon, series_swatch_class
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, ModalId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import tooltipped_action_icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment import _dataframe_helpers as dfh
from dlboard.serve._pages._experiment import _experiment_page_state as core

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dash.development.base_component import Component

    from dlboard.models import DataStore, FlatHparamDict, Run

MAX_COMPARED_RUNS: typing.Final = 5

COMPARE_OPEN_ID: ButtonId[core.ExperimentPage] = ButtonId("compare-open")
COMPARE_MODAL_ID: ModalId[core.ExperimentPage] = ModalId("compare-modal")
COMPARE_RUNS_ID: ValueId[core.ExperimentPage] = ValueId("compare-runs")
COMPARE_MODE_ID: ValueId[core.ExperimentPage] = ValueId("compare-mode")
COMPARE_SEARCH_ID: ValueId[core.ExperimentPage] = ValueId("compare-search")
COMPARE_GRID_ID: ValueId[core.ExperimentPage] = ValueId("compare-grid")
COMPARE_COPY_LINK_ID: ButtonId[core.ExperimentPage] = ButtonId("compare-copy-link")
COMPARE_ROWS_STORE_ID: StoreId[core.ExperimentPage] = StoreId("compare-rows")

_RUNS_LIMIT = 1000
"""Cap on runs offered in the picker -- same discipline as the navbar table's `_HPARAM_ROWS_LIMIT`."""

_CHANGED_CLASS = "dl-compare-changed"
_CHANGED_FIELD = "_changed"
_DIFFERS_FIELD = "_differs"

_SYNC_URL_JS = ClientsideScript(Path(__file__).with_name("sync_compare_url.js"))
_OPEN_JS = ClientsideScript(Path(__file__).with_name("open_on_click.js"))


class CompareMode(StrEnum):
    """Which rows the comparison lists."""

    DIFF = "diff"
    """Only keys whose value isn't the same in every compared run."""

    ALL = "all"
    """Every key any compared run logged."""


class RowKind(StrEnum):
    """What a comparison row's key is."""

    HPARAM = "Hyperparameter"
    METRIC = "Metric (latest)"


def _split_ids(value: object) -> object:
    return [part for part in value.split(",") if part] if isinstance(value, str) else value


class CompareQuery(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """The compare modal's state as it appears in the page URL (`?compare=3,7&compare_mode=all&compare_q=lr`)."""

    runs: Annotated[list[int], BeforeValidator(_split_ids)] = Field(
        default_factory=list[int], alias="compare", max_length=MAX_COMPARED_RUNS
    )
    """The compared runs, baseline first. Empty means the modal is closed."""

    mode: CompareMode = Field(default=CompareMode.DIFF, alias="compare_mode")

    search: str = Field(default="", alias="compare_q")

    @classmethod
    def from_query(cls, query: Mapping[str, str]) -> CompareQuery:
        """Parse a page's query parameters; a malformed link just opens nothing rather than breaking the page."""
        try:
            return cls.model_validate(query)
        except ValidationError:
            return cls()


def run_field(run_id: int) -> str:
    """The row field (and grid column) holding `run_id`'s value."""
    return f"run_{run_id}"


def comparison_rows(
    run_ids: list[int],
    hparams: Mapping[int, FlatHparamDict],
    metrics: Mapping[int, Mapping[str, float]],
) -> list[dict[str, Any]]:
    """
    One row per hyperparameter, then per metric, any of `run_ids` logged, valued per run (`None` where it didn't).

    Each row also records which runs' cells differ from the baseline (the first of `run_ids`), the
    single source of truth for both the diff filter and the highlighting.
    """
    rows: list[dict[str, Any]] = []
    for kind, by_run in ((RowKind.HPARAM, hparams), (RowKind.METRIC, metrics)):
        for key in sorted({key for logged in by_run.values() for key in logged}):
            values = [by_run.get(run_id, {}).get(key) for run_id in run_ids]
            changed = [
                run_field(run_id) for run_id, value in zip(run_ids, values, strict=True) if value != values[0]
            ]
            rows.append(
                {"key": key, "kind": kind.value, _CHANGED_FIELD: changed, _DIFFERS_FIELD: bool(changed)}
                | {run_field(run_id): value for run_id, value in zip(run_ids, values, strict=True)}
            )
    return rows


def visible_rows(rows: list[dict[str, Any]], mode: CompareMode, search: str) -> list[dict[str, Any]]:
    """`rows` narrowed to `mode`, and to those whose key or any run's value contains `search` (case-insensitive)."""
    needle = search.strip().lower()

    def matches(row: dict[str, Any]) -> bool:
        cells = (row["key"], *(value for field, value in row.items() if field.startswith("run_")))
        return any(needle in str(cell).lower() for cell in cells if cell is not None)

    return [
        row
        for row in rows
        if (mode is CompareMode.ALL or row[_DIFFERS_FIELD]) and (not needle or matches(row))
    ]


def comparison_columns(runs: list[Run]) -> list[dict[str, Any]]:
    """Grid columns: the key and its kind, then one per run, highlighted where it departs from the baseline."""
    return [
        {"field": "key", "headerName": "Key", "pinned": "left", "minWidth": 180},
        {"field": "kind", "headerName": "Type", "maxWidth": 150},
        *(
            {
                "field": run_field(run.id),
                "headerName": run_display_name(run),
                "headerClass": series_swatch_class(run.id),
                # Hyperparameters keep their logged precision; floats (metrics) are trimmed to read at a glance.
                "valueFormatter": {
                    "function": "params.value == null ? '' : params.value.toPrecision ? params.value.toPrecision(5) * 1 : params.value"
                },
                "cellClassRules": {
                    _CHANGED_CLASS: f"params.data.{_CHANGED_FIELD}.includes(params.colDef.field)"
                },
            }
            for run in runs
        ),
    ]


def load_comparison(
    store: DataStore[...], experiment_id: int, run_ids: list[int]
) -> tuple[list[Run], list[dict[str, Any]]]:
    """
    The compared runs (those of `run_ids` that belong to this experiment, in order) and their full comparison rows.

    Reads only through existing store calls, scoped to the picked runs: anything else in `run_ids`
    (a stale or hand-edited link) is dropped here, so a link can't reach another experiment's runs.
    """
    if not run_ids:
        return [], []
    by_id = {run.id: run for run in store.get_runs(experiment_id, limit=_RUNS_LIMIT)}
    runs = [by_id[run_id] for run_id in dict.fromkeys(run_ids) if run_id in by_id]
    others = frozenset(by_id) - {run.id for run in runs}
    hparams = {
        h.run_id: h.hparams_dict for h in store.fetch_hyperparams(experiment_id, exclude_run_ids=others)
    }
    metrics = store.fetch_metrics(experiment_id, exclude_run_ids=others).latest_per_run()
    return runs, comparison_rows([run.id for run in runs], hparams, metrics)


def _run_options(runs: list[Run]) -> list[dict[str, str]]:
    return [{"value": str(run.id), "label": run_display_name(run)} for run in runs]


def compare_button() -> Component:
    """The navbar's "Compare" button that opens the modal."""
    return dmc.Button(
        "Compare",
        id=COMPARE_OPEN_ID,
        n_clicks=0,
        variant="subtle",
        size="compact-xs",
        leftSection=icon(Icon.COMPARE),
    )


def compare_components(store: DataStore[...], experiment_id: int, query: CompareQuery) -> list[Component]:
    """
    The modal and the store its rows live in: closed -- or, for a link carrying compare state, already open on it.

    A link's table is rendered right here, in the page's first response, rather than by a callback
    after it: `page_load_budget_test.py` caps the callbacks a page load may cost. Otherwise the
    modal starts empty and the button's callbacks fill it in.
    """
    runs, rows = load_comparison(store, experiment_id, query.runs)
    options = _run_options(list(store.get_runs(experiment_id, limit=_RUNS_LIMIT))) if query.runs else []
    grid = themed_grid_kwargs()
    modal = dmc.Modal(
        id=COMPARE_MODAL_ID,
        title="Compare runs",
        size="90%",
        opened=bool(query.runs),
        children=dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.MultiSelect(
                            id=COMPARE_RUNS_ID,
                            label=f"Runs (up to {MAX_COMPARED_RUNS}; the first is the baseline)",
                            data=options,
                            value=[str(run_id) for run_id in query.runs],
                            maxValues=MAX_COMPARED_RUNS,
                            searchable=True,
                            style={"flex": 1},
                        ),
                        dmc.SegmentedControl(
                            id=COMPARE_MODE_ID,
                            data=[
                                {"value": CompareMode.DIFF.value, "label": "Differences"},
                                {"value": CompareMode.ALL.value, "label": "All"},
                            ],
                            value=query.mode.value,
                        ),
                        dmc.TextInput(
                            id=COMPARE_SEARCH_ID,
                            placeholder="Search keys and values…",
                            leftSection=icon(Icon.SEARCH),
                            value=query.search,
                            debounce=300,
                        ),
                        # Copied client-side by `chart_deep_link.js`: the address bar already holds the state.
                        tooltipped_action_icon(
                            Icon.LINK,
                            component_id=COMPARE_COPY_LINK_ID,
                            label="Copy a link to this comparison",
                            **cast("dict[str, Any]", {"data-dl-copy-page-link": "true"}),
                        ),
                    ],
                    align="flex-end",
                    wrap="nowrap",
                ),
                dag.AgGrid(
                    id=COMPARE_GRID_ID,
                    columnDefs=comparison_columns(runs),
                    rowData=visible_rows(rows, query.mode, query.search),
                    # `cellClassRules` are JS expression strings -- see `_build_hparam_datatable`.
                    dangerously_allow_code=True,
                    columnSize="responsiveSizeToFit",
                    dashGridOptions={"rowHeight": 34, "headerHeight": 34, "domLayout": "autoHeight"},
                    className=f"{grid['className']} dl-compare-grid",
                    style=grid["style"],
                ),
            ]
        ),
    )
    return [modal, Store(id=COMPARE_ROWS_STORE_ID, data=rows)]


def register_run_compare_callbacks(app: Dash) -> None:
    """
    Wire the compare modal: open from the button, load the picked runs, filter, mirror state into the URL.

    Every server callback here is `prevent_initial_call` and listens only to the modal's own
    controls (all in the page's static layout), so none of them costs a request on page load; the
    button, which is mounted later with the navbar, opens the modal clientside for the same reason.
    """
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _OPEN_JS.source,
        Output(COMPARE_MODAL_ID, "opened"),
        Input(COMPARE_OPEN_ID, "n_clicks", allow_optional=True),
        prevent_initial_call=True,
    )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(COMPARE_RUNS_ID, "data"),
        Output(COMPARE_RUNS_ID, "value"),
        Input(COMPARE_MODAL_ID, "opened"),
        State(COMPARE_RUNS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(core.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def prepare_compare(
        opened: bool,  # noqa: FBT001
        picked: list[str],
        experiment_id: int,
        page_json: str,
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not opened:
            raise PreventUpdate
        runs = list(get_data_store().get_runs(experiment_id, limit=_RUNS_LIMIT))
        # Start from the runs the charts are showing, unless a link or an earlier open already chose.
        page = core.BasicExperimentPage.model_validate_json(page_json)
        excluded = cast("list[int]", page.page_settings.get(dfh.EXCLUDED_RUNS_KEY, []))
        charted = [run for run in runs if run.id not in excluded] or runs
        default = [str(run.id) for run in charted[:MAX_COMPARED_RUNS]]
        return _run_options(runs), picked or default

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(COMPARE_ROWS_STORE_ID, "data"),
        Output(COMPARE_GRID_ID, "columnDefs"),
        Input(COMPARE_RUNS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def load_compared_runs(
        picked: list[str], experiment_id: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        runs, rows = load_comparison(get_data_store(), experiment_id, [int(run_id) for run_id in picked])
        return rows, comparison_columns(runs)

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(COMPARE_GRID_ID, "rowData"),
        Input(COMPARE_ROWS_STORE_ID, "data"),
        Input(COMPARE_MODE_ID, "value"),
        Input(COMPARE_SEARCH_ID, "value"),
        prevent_initial_call=True,
    )
    def filter_compared_rows(
        rows: list[dict[str, Any]] | None, mode: str, search: str | None
    ) -> list[dict[str, Any]]:
        return visible_rows(rows or [], CompareMode(mode), search or "")

    # Side-effect only (it writes `history.replaceState`, never the prop): declared as both Input and
    # Output of `opened` for the same reason `sync_view_url.js` is -- Dash has no prop for the address bar.
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _SYNC_URL_JS.source,
        Output(COMPARE_MODAL_ID, "opened", allow_duplicate=True),
        Input(COMPARE_MODAL_ID, "opened"),
        Input(COMPARE_RUNS_ID, "value"),
        Input(COMPARE_MODE_ID, "value"),
        Input(COMPARE_SEARCH_ID, "value"),
        prevent_initial_call=True,
    )
