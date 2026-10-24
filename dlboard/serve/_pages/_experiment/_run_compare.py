"""
The run-compare modal: an A/B view of up to `MAX_COMPARED_RUNS` runs' hyperparameters and latest metrics, a page of keys at a time.

Opened by the "Compare" button above the navbar's run table. Everything the modal shows -- which
runs, a diff or the full list, the search text, the page -- is mirrored into the page URL (`CompareQuery`),
so copying the address bar shares exactly what the viewer sees, and landing on such a link opens
the modal already filled in. The first picked run is the baseline: every other run's cell that
differs from it is highlighted.

Reading a comparison has two costs, and each is paid where it belongs. Which keys there are, and whether
each one's values differ across the runs (`comparison_index`), needs every key's latest value: that is read
when the picked runs change, and the browser keeps only the keys and those flags. Each page then reads only
its own keys' values (`comparison_page`), so paging and filtering never read the rest.

Fully self-contained like `_run_comparison_table.py`; the one hook into the page is `compare_components`,
which the experiment layout places beside the notes drawer.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, ctx
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from dlboard.models import MetricReduction
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon, series_swatch_class
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId, ValueId
from dlboard.serve._grid import themed_grid_kwargs
from dlboard.serve._pages._dash_helpers import tooltipped_action_icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment._paging import page_slice
from dlboard.serve._pages._experiment._shown_runs import shown_run_ids

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dash.development.base_component import Component

    from dlboard.models import DataStore, FlatHparamDict, Run

MAX_COMPARED_RUNS: typing.Final = 5

COMPARE_PAGE_SIZE: typing.Final = 50
"""How many keys the comparison lists at once. A page reads only its own keys' values."""

COMPARE_OPEN_ID: ButtonId[ids.ExperimentPage] = ButtonId("compare-open")
COMPARE_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("compare-modal")
COMPARE_RUNS_ID: ValueId[ids.ExperimentPage] = ValueId("compare-runs")
COMPARE_MODE_ID: ValueId[ids.ExperimentPage] = ValueId("compare-mode")
COMPARE_SEARCH_ID: ValueId[ids.ExperimentPage] = ValueId("compare-search")
COMPARE_PAGER_ID: ValueId[ids.ExperimentPage] = ValueId("compare-pager")
COMPARE_PAGE_SLOT_ID: DivId[ids.ExperimentPage] = DivId("compare-page-slot")
COMPARE_GRID_ID: ValueId[ids.ExperimentPage] = ValueId("compare-grid")
COMPARE_COPY_LINK_ID: ButtonId[ids.ExperimentPage] = ButtonId("compare-copy-link")
COMPARE_KEYS_STORE_ID: StoreId[ids.ExperimentPage] = StoreId("compare-keys")

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
    """
    The compare modal's state as it appears in the page URL.

    For example `?compare=3,7&compare_mode=all&compare_q=lr`, with `&compare_page=2` past the first page.
    """

    runs: Annotated[list[int], BeforeValidator(_split_ids)] = Field(
        default_factory=list[int], alias="compare", max_length=MAX_COMPARED_RUNS
    )
    """The compared runs, baseline first. Empty means the modal is closed."""

    mode: CompareMode = Field(default=CompareMode.DIFF, alias="compare_mode")

    search: str = Field(default="", alias="compare_q")

    page: int = Field(default=1, alias="compare_page")
    """The page of keys shown, from 1. Past the last page, the last one is shown."""

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


def comparison_index(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The keys of a comparison, in order, each with whether its values differ: all the browser holds to page through them."""
    return [{"key": row["key"], "kind": row["kind"], _DIFFERS_FIELD: row[_DIFFERS_FIELD]} for row in rows]


def filtered_index(
    index: list[dict[str, Any]], mode: CompareMode, search: str, hparams: Mapping[int, FlatHparamDict]
) -> list[dict[str, Any]]:
    """
    The entries of `index` that `mode` and `search` leave, in order.

    A search matches a key's name, and a hyperparameter's values across the runs. A metric's values are not
    searched: that would need every metric's latest value on each keystroke, which the index does not hold.
    """
    needle = search.strip().lower()
    values: dict[str, list[object]] = {}
    for logged in hparams.values():
        for key, value in logged.items():
            values.setdefault(key, []).append(value)

    def matches(entry: dict[str, Any]) -> bool:
        if needle in entry["key"].lower():
            return True
        return entry["kind"] == RowKind.HPARAM.value and any(
            value is not None and needle in str(value).lower() for value in values.get(entry["key"], [])
        )

    return [
        entry
        for entry in index
        if (mode is CompareMode.ALL or entry[_DIFFERS_FIELD]) and (not needle or matches(entry))
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


def _compared_runs(store: DataStore[...], experiment_id: int, run_ids: list[int]) -> list[Run]:
    """The picked runs that belong to this experiment, in the order picked (the first is the baseline)."""
    if not run_ids:
        return []
    by_id = {run.id: run for run in store.get_runs(experiment_id, limit=None, run_ids=frozenset(run_ids))}
    return [by_id[run_id] for run_id in dict.fromkeys(run_ids) if run_id in by_id]


def load_comparison(
    store: DataStore[...], experiment_id: int, run_ids: list[int]
) -> tuple[list[Run], list[dict[str, Any]]]:
    """
    The compared runs (those of `run_ids` that belong to this experiment, in order) and their full comparison rows.

    Reads only through existing store calls, scoped to the picked runs: anything else in `run_ids`
    (a stale or hand-edited link) is dropped here, so a link can't reach another experiment's runs.
    """
    runs = _compared_runs(store, experiment_id, run_ids)
    if not runs:
        return [], []
    compared = frozenset(run.id for run in runs)
    hparams = {h.run_id: h.hparams_dict for h in store.fetch_hyperparams(experiment_id, run_ids=compared)}
    metrics = store.fetch_metrics(
        experiment_id, run_ids=compared, reduction=MetricReduction.LATEST
    ).latest_per_run()
    return runs, comparison_rows([run.id for run in runs], hparams, metrics)


def _only[V](by_run: Mapping[int, Mapping[str, V]], keys: frozenset[str]) -> dict[int, dict[str, V]]:
    return {
        run_id: {key: value for key, value in logged.items() if key in keys}
        for run_id, logged in by_run.items()
    }


@dataclass(frozen=True)
class ComparePage:
    """One page of a comparison: its rows, the runs they are valued for, and where the page sits among the keys."""

    runs: list[Run]
    rows: list[dict[str, Any]]
    matching: int
    """How many keys the filter leaves, over every page."""
    number: int
    """The (clamped) page shown, from 1."""
    total: int
    """How many pages there are."""


def comparison_page(  # noqa: PLR0913 -- the store and the picked runs, then what the page is of
    store: DataStore[...],
    experiment_id: int,
    run_ids: list[int],
    index: list[dict[str, Any]],
    mode: CompareMode,
    search: str,
    number: int,
) -> ComparePage:
    """
    Page `number` of the comparison `index` describes, for the picked runs: those keys' values, and no other key's.

    The runs' hyperparameters are read whole (a few per run) and the page's metrics by key, one probe per run and key
    on the page. The index already says which keys are listed, so a page never reads the others.
    """
    runs = _compared_runs(store, experiment_id, run_ids)
    if not runs:
        return ComparePage(runs=[], rows=[], matching=0, number=1, total=1)
    compared = frozenset(run.id for run in runs)
    hparams = {h.run_id: h.hparams_dict for h in store.fetch_hyperparams(experiment_id, run_ids=compared)}
    matching = filtered_index(index, mode, search, hparams)
    shown = page_slice(matching, number, COMPARE_PAGE_SIZE)
    hparam_keys = frozenset(entry["key"] for entry in shown.items if entry["kind"] == RowKind.HPARAM.value)
    metric_keys = frozenset(entry["key"] for entry in shown.items if entry["kind"] == RowKind.METRIC.value)
    metrics: dict[int, dict[str, float]] = (
        store.fetch_metrics(
            experiment_id, keys=metric_keys, run_ids=compared, reduction=MetricReduction.LATEST
        ).latest_per_run()
        if metric_keys
        else {}
    )
    rows = comparison_rows([run.id for run in runs], _only(hparams, hparam_keys), _only(metrics, metric_keys))
    return ComparePage(runs=runs, rows=rows, matching=len(matching), number=shown.number, total=shown.total)


def _count_text(page: ComparePage) -> str:
    if not page.matching:
        return "No keys to show"
    first = (page.number - 1) * COMPARE_PAGE_SIZE + 1
    return f"Showing {first}-{min(first + COMPARE_PAGE_SIZE - 1, page.matching)} of {page.matching} keys"


def page_controls(page: ComparePage) -> list[Component]:
    """The count and pager under the grid. Always both, so the pager's callback input exists on every page."""
    return [
        dmc.Text(_count_text(page), size="sm", c="dimmed"),
        dmc.Pagination(id=COMPARE_PAGER_ID, total=page.total, value=page.number, withEdges=True, size="sm"),
    ]


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
    The modal and the store its keys live in: closed -- or, for a link carrying compare state, already open on it.

    A link's table is rendered right here, in the page's first response, rather than by a callback
    after it: `page_load_budget_test.py` caps the callbacks a page load may cost. Otherwise the
    modal starts empty and the button's callbacks fill it in.
    """
    _, rows = load_comparison(store, experiment_id, query.runs)
    index = comparison_index(rows)
    page = comparison_page(store, experiment_id, query.runs, index, query.mode, query.search, query.page)
    options: list[dict[str, str]] = (
        _run_options(list(store.get_runs(experiment_id, limit=None, run_ids=None))) if query.runs else []
    )
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
                            placeholder="Search keys and hyperparams…",
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
                    columnDefs=comparison_columns(page.runs),
                    rowData=page.rows,
                    # `cellClassRules` are JS expression strings -- see `_build_hparam_datatable`.
                    dangerously_allow_code=True,
                    columnSize="responsiveSizeToFit",
                    dashGridOptions={"rowHeight": 34, "headerHeight": 34, "domLayout": "autoHeight"},
                    className=f"{grid['className']} dl-compare-grid",
                    style=grid["style"],
                ),
                dmc.Group(
                    page_controls(page),
                    id=COMPARE_PAGE_SLOT_ID,
                    justify="space-between",
                    align="center",
                ),
            ]
        ),
    )
    return [modal, Store(id=COMPARE_KEYS_STORE_ID, data=index)]


def register_run_compare_callbacks(app: Dash) -> None:
    """
    Wire the compare modal: open from the button, load the picked runs' keys, page and filter them, mirror state into the URL.

    Every server callback here is `prevent_initial_call` and listens only to the modal's own
    controls (all in the page's static layout), so none of them costs a request on page load; the
    button, which is mounted later with the navbar, opens the modal clientside for the same reason.
    """
    app.clientside_callback(
        _OPEN_JS.source,
        Output(COMPARE_MODAL_ID, "opened"),
        Input(COMPARE_OPEN_ID, "n_clicks", allow_optional=True),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(COMPARE_RUNS_ID, "data"),
        Output(COMPARE_RUNS_ID, "value"),
        Input(COMPARE_MODAL_ID, "opened"),
        State(COMPARE_RUNS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
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
        store = get_data_store()
        runs = list(store.get_runs(experiment_id, limit=None, run_ids=None))
        # Start from the runs the charts are showing, unless a link or an earlier open already chose.
        page = page_model.BasicExperimentPage.model_validate_json(page_json)
        shown = shown_run_ids(store, experiment_id, page.document)
        charted = [run for run in runs if run.id in shown] or runs
        default = [str(run.id) for run in charted[:MAX_COMPARED_RUNS]]
        return _run_options(runs), picked or default

    @app.callback(
        Output(COMPARE_KEYS_STORE_ID, "data"),
        Input(COMPARE_RUNS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def load_compared_keys(picked: list[str], experiment_id: int) -> list[dict[str, Any]]:
        _, rows = load_comparison(get_data_store(), experiment_id, [int(run_id) for run_id in picked])
        return comparison_index(rows)

    @app.callback(
        Output(COMPARE_GRID_ID, "columnDefs"),
        Output(COMPARE_GRID_ID, "rowData"),
        Output(COMPARE_PAGE_SLOT_ID, "children"),
        Input(COMPARE_KEYS_STORE_ID, "data"),
        Input(COMPARE_MODE_ID, "value"),
        Input(COMPARE_SEARCH_ID, "value"),
        Input(COMPARE_PAGER_ID, "value", allow_optional=True),
        State(COMPARE_RUNS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def page_compared_keys(  # noqa: PLR0913 -- one input per control the page depends on
        index: list[dict[str, Any]] | None,
        mode: str,
        search: str | None,
        number: int | None,
        picked: list[str],
        experiment_id: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Component]]:
        # A new filter or key list starts again from the first page; only the pager itself keeps its place.
        page_number = (number or 1) if ctx.triggered_id == COMPARE_PAGER_ID else 1
        page = comparison_page(
            get_data_store(),
            experiment_id,
            [int(run_id) for run_id in picked],
            index or [],
            CompareMode(mode),
            search or "",
            page_number,
        )
        return comparison_columns(page.runs), page.rows, page_controls(page)

    # Side-effect only (it writes `history.replaceState`, never the prop): declared as both Input and
    # Output of `opened` for the same reason `sync_view_url.js` is -- Dash has no prop for the address bar.
    app.clientside_callback(
        _SYNC_URL_JS.source,
        Output(COMPARE_MODAL_ID, "opened", allow_duplicate=True),
        Input(COMPARE_MODAL_ID, "opened"),
        Input(COMPARE_RUNS_ID, "value"),
        Input(COMPARE_MODE_ID, "value"),
        Input(COMPARE_SEARCH_ID, "value"),
        Input(COMPARE_PAGER_ID, "value", allow_optional=True),
        prevent_initial_call=True,
    )
