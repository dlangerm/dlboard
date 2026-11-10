"""
The run-compare modal: an A/B view of up to `MAX_COMPARED_RUNS` runs' hyperparameters and latest metrics, a page of keys at a time.

Opened by the "Compare" button above the navbar's run table. Everything the modal shows -- which
runs, a diff or the full list, the search text, the page -- is mirrored into the page URL (`CompareQuery`),
so copying the address bar shares exactly what the viewer sees, and landing on such a link opens
the modal already filled in. The first picked run is the baseline: every other run's cell that
differs from it is highlighted.

Reading a comparison has two costs, and each is paid where it belongs. Which keys there are, and whether
each one's values differ across the runs (`CompareIndex`), needs every key's latest value: that is read
when the picked runs change, and the browser keeps only the keys and those flags. Each page then reads only
its own keys' values (`comparison_page`), so paging and filtering never read the rest.

Fully self-contained like `_runs_table.py`; the one hook into the page is `compare_components`, of an experiment's runs or a
comparison's (`compared_scope`: each run is read from its own experiment),
which the page's layout places beside its panels.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple, Self, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, ctx
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, BeforeValidator, Field, ValidationError, model_validator

from dlboard.models import MetricReduction, Run
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon, series_swatch_class
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId, ValueId
from dlboard.serve._grid import themed_grid_kwargs
from dlboard.serve._pages._dash_helpers import CallbackStates, notice, tooltipped_action_icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment._paging import page_slice
from dlboard.serve._pages._experiment._panel_data import experiment_names
from dlboard.serve._pages._experiment._run_picker import run_options
from dlboard.serve._pages._experiment._shown_runs import page_scope

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from dash.development.base_component import Component

    from dlboard.models import DataStore, FlatHparamDict
    from dlboard.serve._pages._experiment._shown_runs import RunScope

MAX_COMPARED_RUNS: typing.Final = 5

COMPARE_PAGE_SIZE: typing.Final = 50
"""How many keys the comparison lists at once. A page reads only its own keys' values."""

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


def _valued(
    by_run: Mapping[int, Mapping[str, object]], run_ids: list[int]
) -> Iterator[tuple[str, list[object]]]:
    """Each key any of `by_run` logged, in order, with its value for each of `run_ids` (`None` where it didn't)."""
    for key in sorted({key for logged in by_run.values() for key in logged}):
        yield key, [by_run.get(run_id, {}).get(key) for run_id in run_ids]


def comparison_rows(
    run_ids: list[int],
    hparams: Mapping[int, FlatHparamDict],
    metrics: Mapping[int, Mapping[str, float]],
) -> list[dict[str, Any]]:
    """
    One row per hyperparameter, then per metric, any of `run_ids` logged, valued per run (`None` where it didn't).

    Each row also records which runs' cells differ from the baseline (the first of `run_ids`), the
    single source of truth for the highlighting.
    """
    rows: list[dict[str, Any]] = []
    for kind, by_run in ((RowKind.HPARAM, hparams), (RowKind.METRIC, metrics)):
        for key, values in _valued(by_run, run_ids):
            changed = [
                run_field(run_id) for run_id, value in zip(run_ids, values, strict=True) if value != values[0]
            ]
            rows.append(
                {"key": key, "kind": kind.value, _CHANGED_FIELD: changed, _DIFFERS_FIELD: bool(changed)}
                | {run_field(run_id): value for run_id, value in zip(run_ids, values, strict=True)}
            )
    return rows


class CompareKey(NamedTuple):
    """One key of a comparison, and whether its values differ across the runs."""

    key: str
    kind: RowKind
    differs: bool


class CompareIndex(BaseModel, frozen=True):
    """
    The keys of a comparison, in order, and whether each one's values differ: all the browser holds to page through them.

    Columns rather than a record per key, as the browser sends it back with every page request and a record repeated
    its field names for each of thousands of keys. The flags are of the values when the runs were picked: the page's own
    values are read afresh, so a run that is still training can leave a key flagged that has since come to agree.
    """

    keys: list[str] = Field(default_factory=list[str])
    hparams: int = 0
    """How many of the keys, from the first, are hyperparameters; the rest are metrics."""
    differs: str = Field(default="", pattern=r"^[01]*$")
    """A `1` for each key whose values differ across the runs, and a `0` for each that agrees."""

    @model_validator(mode="after")
    def _one_flag_per_key(self) -> Self:
        if len(self.differs) != len(self.keys) or self.hparams > len(self.keys):
            msg = "a comparison index has a flag for each key, and no more hyperparameters than keys"
            raise ValueError(msg)
        return self

    @classmethod
    def of(
        cls,
        run_ids: list[int],
        hparams: Mapping[int, FlatHparamDict],
        metrics: Mapping[int, Mapping[str, float]],
    ) -> CompareIndex:
        """The index of a comparison: the hyperparameters' keys, then the metrics', with whether each differs."""
        listed = [
            (key, any(value != values[0] for value in values))
            for by_run in (hparams, metrics)
            for key, values in _valued(by_run, run_ids)
        ]
        return cls(
            keys=[key for key, _ in listed],
            hparams=len({key for logged in hparams.values() for key in logged}),
            differs="".join("1" if differs else "0" for _, differs in listed),
        )

    @property
    def entries(self) -> list[CompareKey]:
        return [
            CompareKey(key, RowKind.HPARAM if at < self.hparams else RowKind.METRIC, flag == "1")
            for at, (key, flag) in enumerate(zip(self.keys, self.differs, strict=True))
        ]


def filtered_index(
    index: CompareIndex, mode: CompareMode, search: str, hparams: Mapping[int, FlatHparamDict]
) -> list[CompareKey]:
    """
    The keys of `index` that `mode` and `search` leave, in order.

    A search matches a key's name, and a hyperparameter's values across the runs. A metric's values are not
    searched: that would need every metric's latest value on each keystroke, which the index does not hold.
    """
    needle = search.strip().lower()
    values: dict[str, list[object]] = {}
    for logged in hparams.values():
        for key, value in logged.items():
            values.setdefault(key, []).append(value)

    def matches(entry: CompareKey) -> bool:
        if needle in entry.key.lower():
            return True
        return entry.kind is RowKind.HPARAM and any(
            value is not None and needle in str(value).lower() for value in values.get(entry.key, [])
        )

    return [
        entry
        for entry in index.entries
        if (mode is CompareMode.ALL or entry.differs) and (not needle or matches(entry))
    ]


def comparison_columns(runs: list[Run], experiments: Mapping[int, str] | None = None) -> list[dict[str, Any]]:
    """
    Grid columns: the key and its kind, then one per run, highlighted where it departs from the baseline.

    Runs of several experiments are named as their charts name them, "experiment / run" (`experiments`, by id).
    """
    named = experiments or {}
    return [
        {"field": "key", "headerName": "Key", "pinned": "left", "minWidth": 180},
        {"field": "kind", "headerName": "Type", "maxWidth": 150},
        *(
            {
                "field": run_field(run.id),
                "headerName": f"{named[run.experiment_id]} / {run_display_name(run)}"
                if run.experiment_id in named
                else run_display_name(run),
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


def compared_scope(
    store: DataStore[...], page: page_model.BasicExperimentPage, run_ids: list[int]
) -> RunScope:
    """
    Which of `run_ids` the page may compare, by experiment: any of an experiment's runs, or a comparison's members.

    Anything else in `run_ids` (a stale or hand-edited link) is left out, so a link can't reach another experiment's runs.
    """
    picked = frozenset(run_ids)
    if page.experiment_id is not None:
        return {page.experiment_id: picked}
    scope: dict[int, set[int]] = {}
    for run in store.comparison_runs(page.id):
        if run.id in picked:
            scope.setdefault(run.experiment_id, set()).add(run.id)
    return {experiment_id: frozenset(ids) for experiment_id, ids in scope.items()}


def load_compared(
    store: DataStore[...], scope: RunScope, run_ids: list[int]
) -> tuple[list[Run], dict[int, FlatHparamDict]]:
    """
    The picked runs `scope` holds, in the order picked (the first is the baseline), and their hyperparameters.

    A read of each experiment in `scope`, of its picked runs alone: a run of another experiment is not among them.
    """
    by_id: dict[int, Run] = {}
    hparams: dict[int, FlatHparamDict] = {}
    for experiment_id, picked in scope.items():
        found = (
            {run.id: run for run in store.get_runs(experiment_id, limit=None, run_ids=picked)}
            if picked
            else dict[int, Run]()
        )
        by_id.update(found)
        if found:
            hparams.update(
                (h.run_id, h.hparams_dict)
                for h in store.fetch_hyperparams(experiment_id, run_ids=frozenset(found))
            )
    return [by_id[run_id] for run_id in dict.fromkeys(run_ids) if run_id in by_id], hparams


def run_experiment_names(store: DataStore[...], runs: list[Run]) -> Mapping[int, str]:
    """What each of `runs`' experiments is called in a run's name, once they are of more than one (`experiment_names`)."""
    return experiment_names(store, {run.experiment_id: frozenset[int]() for run in runs})


def _latest(
    store: DataStore[...], runs: list[Run], keys: frozenset[str] | None
) -> dict[int, dict[str, float]]:
    """The latest value of each of `keys` (every metric, if `None`) for each of `runs`: a read of each of their experiments."""
    by_experiment: dict[int, set[int]] = {}
    for run in runs:
        by_experiment.setdefault(run.experiment_id, set()).add(run.id)
    latest: dict[int, dict[str, float]] = {}
    for experiment_id, run_ids in by_experiment.items():
        latest.update(
            store.fetch_metrics(
                experiment_id, keys=keys, run_ids=frozenset(run_ids), reduction=MetricReduction.LATEST
            ).latest_per_run()
        )
    return latest


@dataclass(frozen=True)
class Comparison:
    """The runs a comparison is of, their hyperparameters, and the index of every key they logged."""

    runs: list[Run]
    hparams: dict[int, FlatHparamDict]
    index: CompareIndex


def load_run_compare(store: DataStore[...], scope: RunScope, run_ids: list[int]) -> Comparison:
    """
    The comparison of the picked runs: every key's latest value is read here, to say which keys differ, and not kept.

    Reads only through existing store calls, scoped to the picked runs (see `load_compared`).
    """
    runs, hparams = load_compared(store, scope, run_ids)
    if not runs:
        return Comparison(runs=[], hparams={}, index=CompareIndex())
    metrics = _latest(store, runs, None)
    return Comparison(
        runs=runs, hparams=hparams, index=CompareIndex.of([run.id for run in runs], hparams, metrics)
    )


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


def comparison_page(  # noqa: PLR0913 -- the store and the runs, then what the page is of
    store: DataStore[...],
    runs: list[Run],
    hparams: Mapping[int, FlatHparamDict],
    index: CompareIndex,
    mode: CompareMode,
    search: str,
    number: int,
) -> ComparePage:
    """
    Page `number` of the comparison `index` describes, for `runs`: those keys' values, and no other key's.

    The page's hyperparameters come from `hparams` (a few per run) and its metrics are read by key, one probe per run
    and key on the page. The index already says which keys are listed, so a page never reads the others.
    """
    if not runs:
        return ComparePage(runs=[], rows=[], matching=0, number=1, total=1)
    matching = filtered_index(index, mode, search, hparams)
    shown = page_slice(matching, number, COMPARE_PAGE_SIZE)
    hparam_keys = frozenset(entry.key for entry in shown.items if entry.kind is RowKind.HPARAM)
    metric_keys = frozenset(entry.key for entry in shown.items if entry.kind is RowKind.METRIC)
    metrics = _latest(store, runs, metric_keys) if metric_keys else dict[int, dict[str, float]]()
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


def compare_options(
    store: DataStore[...], page: page_model.BasicExperimentPage, *, search: str = "", keep: Iterable[str] = ()
) -> list[dict[str, str]]:
    """
    The picker's runs: an experiment's, as any run picker lists them, or a comparison's members, named as its charts are.

    A comparison's members are bounded (`DLBOARD_MAX_COMPARISON_RUNS`), so they are listed whole, and searched here.
    """
    if page.experiment_id is not None:
        return run_options(store, page.experiment_id, search=search, keep=keep)
    members = store.comparison_runs(page.id)
    scope: dict[int, set[int]] = {}
    for run in members:
        scope.setdefault(run.experiment_id, set()).add(run.id)
    names = experiment_names(store, {e: frozenset(ids) for e, ids in scope.items()})
    kept, wanted = frozenset(keep), search.strip().lower()
    labelled = [
        (run, f"{names[run.experiment_id]} / {run_display_name(run)}" if names else run_display_name(run))
        for run in members
    ]
    return [
        {"value": str(run.id), "label": label}
        for run, label in labelled
        if str(run.id) in kept or wanted in label.lower() or wanted == str(run.id)
    ]


def _newest_drawn(store: DataStore[...], page: page_model.BasicExperimentPage) -> list[str]:
    """The runs the compare picker starts from: the newest `MAX_COMPARED_RUNS` the charts draw, else an experiment's newest."""
    drawn = [
        run
        for experiment_id, run_ids in page_scope(store, page).items()
        for run in store.get_runs(experiment_id, limit=MAX_COMPARED_RUNS, run_ids=run_ids)
    ]
    if not drawn and page.experiment_id is not None:
        drawn = list(store.get_runs(page.experiment_id, limit=MAX_COMPARED_RUNS, run_ids=None))
    newest = sorted(drawn, key=lambda run: (run.created_at, run.id), reverse=True)[:MAX_COMPARED_RUNS]
    return [str(run.id) for run in newest]


def compare_components(
    store: DataStore[...], page: page_model.BasicExperimentPage, query: CompareQuery
) -> list[Component]:
    """
    The modal and the store its keys live in: closed -- or, for a link carrying compare state, already open on it.

    A link's table is rendered right here, in the page's first response, rather than by a callback
    after it: `page_load_budget_test.py` caps the callbacks a page load may cost. Otherwise the
    modal starts empty and the button's callbacks fill it in. Of an experiment's runs, or of a comparison's.
    """
    comparison = load_run_compare(store, compared_scope(store, page, query.runs), query.runs)
    shown = comparison_page(
        store,
        comparison.runs,
        comparison.hparams,
        comparison.index,
        query.mode,
        query.search,
        query.page,
    )
    options = (
        compare_options(store, page, keep=[str(run_id) for run_id in query.runs])
        if query.runs
        else list[dict[str, str]]()
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
                    columnDefs=comparison_columns(shown.runs, run_experiment_names(store, shown.runs)),
                    rowData=shown.rows,
                    # `cellClassRules` are JS expression strings -- see `_build_hparam_datatable`.
                    dangerously_allow_code=True,
                    columnSize="responsiveSizeToFit",
                    dashGridOptions={"rowHeight": 34, "headerHeight": 34, "domLayout": "autoHeight"},
                    className=f"{grid['className']} dl-compare-grid",
                    style=grid["style"],
                ),
                dmc.Group(
                    page_controls(shown),
                    id=COMPARE_PAGE_SLOT_ID,
                    justify="space-between",
                    align="center",
                ),
            ]
        ),
    )
    return [modal, Store(id=COMPARE_KEYS_STORE_ID, data=comparison.index.model_dump())]


class Diff(NamedTuple):
    """What a run list's Diff does with the runs selected in it: open the modal on them, or say why not."""

    runs: list[str] | None
    notice: list[dict[str, Any]] | None


def diff_of(run_ids: frozenset[int]) -> Diff:
    """Open the A/B diff on `run_ids`, if there are few enough to read side by side, newest first (the first is the baseline)."""
    if len(run_ids) > MAX_COMPARED_RUNS:
        return Diff(
            None, notice("Too many runs to diff", f"The diff compares up to {MAX_COMPARED_RUNS} runs.")
        )
    return Diff([str(run_id) for run_id in sorted(run_ids, reverse=True)], None)


class _ComparedPage(CallbackStates):
    """The page of the comparison to show: the keys the runs have, each control that narrows them, and the runs."""

    index: Annotated[CompareIndex | None, Input(COMPARE_KEYS_STORE_ID, "data")]
    mode: Annotated[CompareMode, Input(COMPARE_MODE_ID, "value")]
    search: Annotated[str | None, Input(COMPARE_SEARCH_ID, "value")]
    number: Annotated[int | None, Input(COMPARE_PAGER_ID, "value", allow_optional=True)]
    picked: Annotated[list[int], State(COMPARE_RUNS_ID, "value")]
    page_json: Annotated[str, State(ids.STATE_PAGE_STORAGE, "data")]


def register_run_compare_callbacks(app: Dash) -> None:
    """
    Wire the compare modal: load the picked runs' keys, page and filter them, mirror state into the URL.

    Every server callback here is `prevent_initial_call` and listens only to the modal's own
    controls (all in the page's static layout), so none of them costs a request on page load. The run list
    opens the modal itself, for the runs selected in it (`RunAction.DIFF`).
    """

    def drawn(page_json: str) -> page_model.BasicExperimentPage:
        return page_model.BasicExperimentPage.model_validate_json(page_json)

    @app.callback(
        Output(COMPARE_RUNS_ID, "data", allow_duplicate=True),
        Input(COMPARE_RUNS_ID, "searchValue"),
        State(COMPARE_RUNS_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def search_runs(search: str | None, picked: list[str] | None, page_json: str) -> list[dict[str, str]]:
        return compare_options(get_data_store(), drawn(page_json), search=search or "", keep=picked or ())

    @app.callback(
        Output(COMPARE_RUNS_ID, "data", allow_duplicate=True),
        Output(COMPARE_RUNS_ID, "value"),
        Input(COMPARE_MODAL_ID, "opened"),
        State(COMPARE_RUNS_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def prepare_compare(
        opened: bool,  # noqa: FBT001
        picked: list[str],
        page_json: str,
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not opened:
            raise PreventUpdate
        store = get_data_store()
        # Start from the runs the charts are showing, unless a link or an earlier open already chose.
        page = drawn(page_json)
        chosen = picked or _newest_drawn(store, page)
        return compare_options(store, page, keep=chosen), chosen

    @app.callback(
        Output(COMPARE_KEYS_STORE_ID, "data"),
        Input(COMPARE_RUNS_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        prevent_initial_call=True,
    )
    def load_compared_keys(picked: list[str], page_json: str) -> dict[str, Any]:
        store, run_ids = get_data_store(), [int(run_id) for run_id in picked]
        comparison = load_run_compare(store, compared_scope(store, drawn(page_json), run_ids), run_ids)
        return comparison.index.model_dump()

    @app.callback(
        Output(COMPARE_GRID_ID, "columnDefs"),
        Output(COMPARE_GRID_ID, "rowData"),
        Output(COMPARE_PAGE_SLOT_ID, "children"),
        _ComparedPage.states(),
        prevent_initial_call=True,
    )
    def page_compared_keys(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Component]]:
        # Its grouping is all it reads, so Dash hands it over as keyword arguments rather than one dict.
        shown = _ComparedPage.model_validate(states)
        # A new filter or key list starts again from the first page; only the pager itself keeps its place.
        page_number = (shown.number or 1) if ctx.triggered_id == COMPARE_PAGER_ID else 1
        store = get_data_store()
        runs, hparams = load_compared(
            store, compared_scope(store, drawn(shown.page_json), shown.picked), shown.picked
        )
        page = comparison_page(
            store,
            runs,
            hparams,
            shown.index or CompareIndex(),
            shown.mode,
            shown.search or "",
            page_number,
        )
        return (
            comparison_columns(page.runs, run_experiment_names(store, page.runs)),
            page.rows,
            page_controls(page),
        )

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
