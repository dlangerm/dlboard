"""
A panel's data: what the page fetches for the charts on screen, and the fingerprint that says when it changed.

No Dash. The callbacks fetch the `PanelFrames` of the panels on screen here and hand them to the views, which
never see a store, and the live poll compares `chart_data_fingerprint`s to decide which charts to redraw.
"""

from __future__ import annotations

import functools
import hashlib
import time
import typing
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard import models
from dlboard._telemetry import Attributes, attribute
from dlboard._telemetry.traces import SpanKind, TraceKey, span
from dlboard.models import (
    RUN_NAME_COLUMN,
    Artifact,
    ColumnKind,
    Histogram,
    HyperParams,
    MetricColumn,
    artifact_column,
    artifact_fname_column,
    artifact_tags_column,
    column_kind,
    histogram_column,
    hparam_column,
)
from dlboard.serve import telemetry
from dlboard.serve._backend._artifact_download import artifact_url
from dlboard.serve._backend._metric_frame import MetricFrame
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment._paging import visible_chart_indexes
from dlboard.serve._pages._experiment._shown_runs import shown_run_ids

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
    from dlboard.serve._pages._experiment._paging import Paging

_log = get_logger(__name__)

_ROW_KEY: typing.Final = [MetricColumn.RUN_ID, MetricColumn.STEP]
"""What identifies a row of a panel's dataframe: the run and the step."""


class HistogramFetchSettings(BaseSettings):
    """`DLBOARD_*` environment variables bounding what a panel reads of its histograms."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    max_histogram_steps: PositiveInt = 256
    """
    Steps per run and key a panel fetches of a histogram, however many were logged.

    What bounds a histogram chart's cost: it draws at most this many columns per run. A run that logged more is
    thinned in the database to every 2ⁿ-th step from its first (keeping its latest too), so the steps shown stay put
    as it trains. Raise it for a finer picture of a long run, at the cost of reading and drawing that much more;
    lower it to make a panel of many histograms cheaper.
    """


class HistoryLimitSettings(BaseSettings):
    """`DLBOARD_*` environment variables bounding how many metrics' whole history one chart draws."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    wide_history_metrics: PositiveInt = 200
    """A chart that draws every step of more metrics than this (a pattern over a family of them) says so, with how many."""
    max_history_metrics: PositiveInt = 5000
    """
    A chart that would draw every step of more metrics than this is not drawn, and their history not read: it says to
    show their last value or their spread instead. No line or table can usefully draw that many series.
    """


@dataclass(frozen=True)
class WideHistory:
    """A chart that draws every step of many metrics: how many, and whether that was too many to read at all."""

    metrics: int
    refused: bool


def _history_width(request: models.DataRequest, known_keys: Collection[str]) -> int:
    """How many metrics' every step `request` reads: none for one of every metric, which says so on its own."""
    columns = _metric_columns(request, known_keys) or set()
    return sum(
        column not in models.MetricColumn and request.reductions.of(column) is models.MetricReduction.HISTORY
        for column in columns
    )


class PanelRead(StrEnum):
    """What one of a panel's reads from the store is for: how its span and its `_READS` series are told apart."""

    KEY_NAMES = "key_names"
    METRICS = "metrics"
    ARTIFACTS = "artifacts"
    HYPERPARAMS = "hyperparams"
    HISTOGRAMS = "histograms"
    SERIES_BANDS = "series_bands"
    RUN_NAMES = "run_names"


@dataclass(frozen=True, slots=True)
class _Read(Attributes):
    kind: PanelRead = attribute(telemetry.AttributeKey.PANEL_READ)


@dataclass(frozen=True, slots=True)
class _MetricRead(_Read):
    reduction: models.MetricReduction = attribute(TraceKey.METRIC_REDUCTION)
    unscoped: bool = attribute(TraceKey.UNSCOPED)
    """Whether it read every metric (a chart that names none, `_unscoped_fetch_badge`): what grows with the experiment."""


_READS = telemetry.Histogram[_Read](
    "dlboard.panel.read.duration",
    telemetry.Unit.SECONDS,
    "How long one of a panel's reads from the store took, building its dataframe included.",
)


def _read[T](read: _Read, fetch: Callable[[], T]) -> T:
    """Run one of a panel's reads in a span of its own, and time it by its kind."""
    started = time.perf_counter()
    with span("dlboard.panel.read", SpanKind.INTERNAL, read):
        result = fetch()
    _READS.record(time.perf_counter() - started, _Read(read.kind))
    return result


class ChartFrames(typing.Protocol):
    """Where a panel's charts get the frames they are drawn from."""

    def for_chart(self, chart: models.ChartInstance[Any, Any]) -> pd.DataFrame:
        """The frame `chart` is drawn from."""
        ...

    def wide_history(self, chart: models.ChartInstance[Any, Any]) -> WideHistory | None:
        """How many metrics' every step `chart` draws, if more than the page lets pass without saying so."""
        ...


@dataclass(frozen=True)
class PanelData:
    """
    What a panel read for its charts, kept by kind, so that each chart is drawn from what it asked for and nothing else.

    Read once for the whole panel (`fetch_panel_data`), not once per chart: what several charts ask for alike is read
    once. A metric two charts read differently (one its mean, the other its series) is read both ways, so neither
    reads more than it draws.
    """

    metrics: Mapping[models.MetricReduction, MetricFrame]
    artifacts: pd.DataFrame
    hparams: pd.DataFrame
    histograms: pd.DataFrame
    series: Mapping[str, pd.DataFrame]
    """By `SeriesBandRequest.column`."""
    run_names: pd.DataFrame
    wide: Mapping[models.DataRequest, WideHistory] = field(default_factory=dict)
    """The charts that draw every step of many metrics, by what they ask for; a refused one's were not read."""

    def wide_history(self, chart: models.ChartInstance[Any, Any]) -> WideHistory | None:
        """How many metrics' every step `chart` draws, if more than `HistoryLimitSettings` lets pass unsaid."""
        return self.wide.get(chart.data_request())

    def for_chart(self, chart: models.ChartInstance[Any, Any]) -> pd.DataFrame:
        """
        `chart`'s own frame: the rows and columns of what its `data_request` asks for, and the names of the runs in it.

        Each metric is in it as the chart reads it, so no metric is in it twice. A hyperparameter pattern is read whole
        and narrowed by the chart, so a chart that asks for one is handed every hyperparameter read.
        """
        request = chart.data_request()
        metrics = [
            selected
            for reduction, frame in self.metrics.items()
            if (
                selected := frame.select(
                    key
                    for key in frame.keys
                    if request.reductions.of(key) is reduction
                    and (request.every_metric or request.draws_metric(key))
                )
            ).keys
        ]
        artifacts = tuple(artifact_column(name) for name in (*request.artifacts, *request.artifact_prefixes))
        histograms = {histogram_column(key) for key in request.histograms}
        hparams = {hparam_column(key) for key in request.hparams}
        merged = merge_hyperparams(
            merge_by_step(
                MetricFrame.merged(metrics).to_frame() if metrics else pd.DataFrame(),
                _with_data_in(self.artifacts, lambda column: column.startswith(artifacts)),
                _with_data_in(self.histograms, histograms.__contains__),
                *(self.series[series.column] for series in request.series if series.column in self.series),
            ),
            _with_data_in(
                self.hparams,
                lambda column: bool(request.hparam_patterns) or column in hparams,
                keep_rows=True,
            ),
        )
        if merged.empty or self.run_names.empty:
            return merged
        return merged.merge(self.run_names, on="run_id", how="left")


def _with_data_in(
    frame: pd.DataFrame, wanted: Callable[[str], bool], *, keep_rows: bool = False
) -> pd.DataFrame:
    """
    The columns of `frame` that are `wanted`, with where its rows are, over the rows where they hold something.

    Empty if none is wanted. `keep_rows` keeps every row, for a frame (a run's hyperparameters) whose rows are not
    steps a chart could have nothing at.
    """
    columns = [column for column in frame.columns if column not in models.NO_DATA_COLUMNS and wanted(column)]
    if not columns:
        return pd.DataFrame()
    rows = frame if keep_rows else frame.dropna(how="all", subset=columns)
    return rows[[column for column in frame.columns if column in models.NO_DATA_COLUMNS or column in columns]]


type PanelFrames = Mapping[str, ChartFrames]
"""The data of the charts `Paging` shows in each open panel, by panel name; a panel with no chart showing has none."""


def panel_frames(
    store: DataStore[...], page: BasicExperimentPage, paging: Paging, names: Iterable[str]
) -> PanelFrames:
    """
    Fetch the charts `paging` shows in the panels called `names`, of the runs `page` shows (the newest few, or its selection).

    Only the page of charts shown is fetched for -- a panel of a thousand charts costs what one page of them does.
    """
    experiment_id = cast("int", page.experiment_id)
    run_ids = shown_run_ids(store, experiment_id, page.document)
    wanted = frozenset(names)
    frames: dict[str, ChartFrames] = {}
    for panel in page.panels:
        indexes = visible_chart_indexes(panel, paging).items
        if panel.name in wanted and indexes:
            shown = panel.model_copy(update={"charts": [panel.charts[i] for i in indexes]})
            frames[panel.name] = fetch_panel_data(store, experiment_id, shown, run_ids)
    return frames


def _splits_metric_and_artifact_charts(requests: Sequence[models.DataRequest]) -> bool:
    """
    Whether a chart of the panel needs only metrics and another only artifacts.

    That is what makes fetching both for one panel wasteful. A chart that wants both (a table of runs with
    an artifact column) asks for nothing the panel would not fetch for it anyway.
    """
    wants = [
        (bool(r.metrics or r.metric_patterns), bool(r.artifacts or r.artifact_prefixes)) for r in requests
    ]
    return any(metrics and not artifacts for metrics, artifacts in wants) and any(
        artifacts and not metrics for metrics, artifacts in wants
    )


def _metric_columns(request: models.DataRequest, known_keys: Collection[str]) -> set[str] | None:
    """The metrics a chart draws: its named ones, and those of `known_keys` its patterns match. `None` for every."""
    if request.every_metric:
        return None
    return set(request.metrics) | {key for key in known_keys if request.draws_metric(key)}


def metrics_by_reduction(
    requests: Sequence[models.DataRequest], known_keys: Collection[str] = ()
) -> Mapping[models.MetricReduction, set[str] | None]:
    """
    The metrics a panel fetches under each `MetricReduction`; `None` meaning every metric.

    Each chart's own metrics (by name, and those of `known_keys` its patterns match), each read as its `reductions` say
    for that column. A metric two charts read differently is read both ways, so neither reads more than it draws: a mean
    beside a chart of the series is the database's to compute, not a history to fetch. A chart of every metric reads each
    as its default says, except the ones it names another way.
    """
    wanted: dict[models.MetricReduction, set[str] | None] = {}

    def want(reduction: models.MetricReduction, columns: Collection[str] | None) -> None:
        if columns is None:
            wanted[reduction] = None
        elif columns and (got := wanted.setdefault(reduction, set())) is not None:
            got.update(columns)

    for request in requests:
        columns = _metric_columns(request, known_keys)
        if columns is None:
            want(request.reductions.default, None)
            columns = set(request.reductions.by_column)
        for column in columns:
            want(request.reductions.of(column), {column})
    return wanted


def fetch_panel_data(
    store: DataStore[...],
    experiment_id: int,
    panel: models.PanelInstance[Any, Any],
    run_ids: frozenset[int] | None,
) -> PanelData:
    """Fetch just the data one panel needs -- not the whole experiment -- of `run_ids` (every run, if `None`)."""
    requests = [chart.data_request() for chart in panel.charts]
    artifact_keys = frozenset[str]().union(*(r.artifacts for r in requests))
    artifact_key_prefixes = frozenset[str]().union(*(r.artifact_prefixes for r in requests))
    has_artifacts = bool(artifact_keys or artifact_key_prefixes)

    if _splits_metric_and_artifact_charts(requests):
        _log.warning(
            "Panel %r mixes metric and artifact charts, fetching both is "
            "less efficient than a panel of one kind; consider splitting it.",
            panel.name,
        )

    # `None` for every hyperparameter: a pattern is read whole, one small row per run, and narrowed by the chart.
    hparam_keys = (
        None if any(r.hparam_patterns for r in requests) else set[str]().union(*(r.hparams for r in requests))
    )
    # A metric pattern names the metrics it matches among the ones logged, so the names are listed for it alone.
    series_requests = sorted(
        frozenset[models.SeriesBandRequest]().union(*(r.series for r in requests)),
        key=lambda request: request.column,
    )
    needs_names = any(r.metric_patterns and not r.every_metric for r in requests) or bool(series_requests)
    known_keys = (
        _read(_Read(PanelRead.KEY_NAMES), lambda: store.summarize_metric_keys(experiment_id))
        if needs_names
        else list[str]()
    )
    # A chart over too many metrics' every step is said so of, or past the limit not read for at all (#417).
    limits = HistoryLimitSettings()
    wide = {
        request: WideHistory(width, refused=width > limits.max_history_metrics)
        for request in requests
        if (width := _history_width(request, known_keys)) > limits.wide_history_metrics
    }
    read = [request for request in requests if request not in wide or not wide[request].refused]
    metrics = {
        reduction: _read(
            _MetricRead(PanelRead.METRICS, reduction, unscoped=columns is None),
            functools.partial(
                store.fetch_metrics,
                experiment_id,
                keys=None if columns is None else frozenset(columns),
                run_ids=run_ids,
                reduction=reduction,
            ),
        )
        for reduction, columns in metrics_by_reduction(read, known_keys).items()
    }
    artifacts_df = (
        _read(
            _Read(PanelRead.ARTIFACTS),
            lambda: build_artifacts_dataframe(
                store.fetch_artifacts(
                    experiment_id,
                    keys=artifact_keys,
                    key_prefixes=artifact_key_prefixes,
                    run_ids=run_ids,
                )
            ),
        )
        if has_artifacts
        else pd.DataFrame()
    )
    # fetch_hyperparams has no server-side key filter -- it's one small row per run either way.
    hparams_df = (
        _read(
            _Read(PanelRead.HYPERPARAMS),
            lambda: build_hyperparams_dataframe(
                store.fetch_hyperparams(experiment_id, run_ids=run_ids), keys=hparam_keys
            ),
        )
        if hparam_keys is None or hparam_keys
        else pd.DataFrame()
    )
    histogram_keys = frozenset[str]().union(*(r.histograms for r in requests))
    histograms_df = (
        _read(
            _Read(PanelRead.HISTOGRAMS),
            lambda: build_histograms_dataframe(
                store.fetch_histograms(
                    experiment_id,
                    keys=histogram_keys,
                    max_steps=HistogramFetchSettings().max_histogram_steps,
                    run_ids=run_ids,
                )
            ),
        )
        if histogram_keys
        else pd.DataFrame()
    )
    series = {
        request.column: _read(
            _Read(PanelRead.SERIES_BANDS),
            functools.partial(_series_band_frame, store, experiment_id, request, known_keys, run_ids),
        )
        for request in series_requests
    }
    read_anything = (
        any(not frame.run_id.empty for frame in metrics.values())
        or not (artifacts_df.empty and hparams_df.empty and histograms_df.empty)
        or any(not frame.empty for frame in series.values())
    )
    # Read once, not by each chart that wants it, so `LineChart`/`ImageChart` can label a run by its name instead of
    # its bare id -- `table_chart.py`'s default "runs" mode explicitly excludes this column, since it isn't a real
    # metric/hparam. Only the runs read need a name.
    run_names = (
        _read(
            _Read(PanelRead.RUN_NAMES),
            lambda: pd.DataFrame.from_records(
                [
                    {"run_id": run.id, RUN_NAME_COLUMN: run_display_name(run)}
                    for run in store.get_runs(experiment_id, limit=None, run_ids=run_ids)
                ]
            ),
        )
        if read_anything
        else pd.DataFrame()
    )
    return PanelData(metrics, artifacts_df, hparams_df, histograms_df, series, run_names, wide)


def build_series_band_dataframe(
    request: models.SeriesBandRequest, bands: typing.Iterable[models.SeriesBand]
) -> pd.DataFrame:
    """One row per (run_id, step), with `request.column` holding that step's `SeriesBand`s, one per group."""
    by_row: dict[tuple[int, int], list[models.SeriesBand]] = {}
    for band in bands:
        by_row.setdefault((band.run_id, band.step), []).append(band)
    return pd.DataFrame.from_records(
        {MetricColumn.RUN_ID: run_id, MetricColumn.STEP: step, request.column: tuple(grouped)}
        for (run_id, step), grouped in by_row.items()
    )


def _series_band_frame(
    store: DataStore[...],
    experiment_id: int,
    request: models.SeriesBandRequest,
    known_keys: typing.Sequence[str],
    run_ids: frozenset[int] | None,
) -> pd.DataFrame:
    """The spread of the metrics `request` asks for, over the newest `request.runs` of `run_ids` (every run if `None`)."""
    groups = request.groups(known_keys)
    newest = frozenset(run.id for run in store.get_runs(experiment_id, limit=request.runs, run_ids=run_ids))
    if not groups or not newest:
        return pd.DataFrame()
    grid = request.cells_for(len(groups), len(newest))
    bands = store.fetch_series_band(experiment_id, groups=groups, run_ids=newest, grid=grid)
    return build_series_band_dataframe(request, bands)


def is_lightning_experiment(data_store: DataStore[...], experiment_id: int) -> bool:
    """
    Whether `experiment_id`'s metrics were logged through `DLBoardLogger`.

    Lightning-specific chart-autogen conventions (see `_chart_autogen.lightning_granularity`) only
    apply to those.
    """
    experiment = data_store.get_experiment(experiment_id)
    return experiment is not None and experiment.source == models.ExperimentSource.PYTORCH_LIGHTNING


def _histogram_contents(cell: object) -> dict[str, Any] | None:
    """What a histogram column's cell holds, as plain data (`None` where the key was not logged at the step)."""
    return cell.model_dump() if isinstance(cell, Histogram) else None


def chart_data_fingerprint(df: pd.DataFrame) -> str:
    """
    A cheap content hash of the frame one chart is drawn from (`PanelData.for_chart`).

    Lets a live-update poll tell "this chart's underlying data hasn't actually changed" apart from
    "the experiment changed *something*, somewhere" -- `Experiment.revision` is bumped by any write
    anywhere in the experiment (a different run, a different metric, an epoch-level chart's data
    between epochs), so without this every open chart would re-render on every poll tick regardless
    of whether anything it actually shows moved. Scoped per *chart*, not per panel: a panel commonly
    holds charts with very different update cadences -- an epoch-level chart next to a per-step one,
    grouped there by "Auto-generate charts" -- and a panel-wide fingerprint would pop every chart in
    it whenever *any one* of them changed.

    The frame is the chart's metrics, the hyperparameters and artifact columns it asks for, and the
    run names, so editing a hyperparameter or renaming a run redraws the charts that show it.

    Hashes the `to_json(orient="split", ...)` serialization the rest of this page relies on,
    not `pandas.util.hash_pandas_object` -- that hashes cell values
    directly and raises `TypeError: unhashable type` the moment any column holds something like a
    list (nothing in this dataframe's own columns does today, but a hash used purely to detect "did
    this change" has no business being pickier about content than the JSON serialization the rest of
    this page already ships to the browser).

    Only rows where the chart has data of its own count: a metric read alike for another chart of the
    panel shares its rows, so another metric logging a step the chart has nothing at adds a row to it --
    which is not a change to the chart, and must not redraw it. (A hyperparameter is on every row of its
    run, so it never says a row is the chart's.) The index is dropped for the same reason: another run's
    new row would renumber this chart's.
    """
    scoped = df
    data_cols = [
        c
        for c in scoped.columns
        if c not in models.NO_DATA_COLUMNS and column_kind(c) is not ColumnKind.HPARAM
    ]
    if data_cols:
        scoped = scoped.dropna(how="all", subset=data_cols)
    # A histogram cell is a model, which `to_json` cannot be trusted to serialize: hash what it holds instead.
    scoped = scoped.assign(
        **{
            column: scoped[column].map(_histogram_contents)
            for column in scoped.columns
            if column_kind(column) is ColumnKind.HISTOGRAM
        }
    )
    return hashlib.blake2b(
        scoped.reset_index(drop=True).to_json(orient="split", date_format="iso").encode(), digest_size=16
    ).hexdigest()


def build_artifacts_dataframe(artifacts: typing.Iterable[Artifact]) -> pd.DataFrame:
    """Pivot artifact download URLs (and tags and file names) into columns per key, indexed by (run_id, step)."""
    # A URL, not the underlying `ref` -- charts (and anything else consuming this dataframe) fetch
    # an artifact by id through `/artifact/<id>`, never by talking to the ref's backend directly.
    # One pass: each artifact is dumped and given its URL as it streams into the frame.
    df = pd.DataFrame(a.model_dump(mode="json") | {"url": artifact_url(a)} for a in artifacts)
    if df.empty:
        return df

    pivots = [
        df.pivot_table(index=["run_id", "step"], columns="key", values=value, aggfunc="first").rename(
            columns=column_name
        )
        for value, column_name in (
            ("url", artifact_column),
            ("tags", artifact_tags_column),
            ("fname", artifact_fname_column),
        )
    ]
    return pd.concat(pivots, axis=1).reset_index()


def build_hyperparams_dataframe(
    hparams: typing.Iterable[HyperParams], keys: typing.Iterable[str] | None = None
) -> pd.DataFrame:
    """
    One row per run_id; hparam keys become `hparam__<key>`-prefixed columns.

    `keys`, if given, keeps only those hparam keys (client-side filter -- `fetch_hyperparams`
    doesn't support filtering server-side, unlike metrics).
    """
    wanted = set(keys) if keys is not None else None
    rows: list[dict[str, Any]] = []
    for h in hparams:
        values = h.hparams_dict
        if wanted is not None:
            values = {k: v for k, v in values.items() if k in wanted}
        rows.append({"run_id": h.run_id, **{hparam_column(k): v for k, v in values.items()}})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_records(rows)


def merge_hyperparams(df: pd.DataFrame, hparams_df: pd.DataFrame) -> pd.DataFrame:
    """Broadcast each run's (step-less) hparam values onto every row for that run_id."""
    if hparams_df.empty:
        return df
    if df.empty:
        return hparams_df
    return df.merge(hparams_df, on="run_id", how="left")


def build_histograms_dataframe(
    histograms: typing.Iterable[models.UnderlyingHistogramTableEntry],
) -> pd.DataFrame:
    """One row per (run_id, step), a `histogram_column(key)` per key holding its `Histogram` at that step."""
    df = pd.DataFrame.from_records(
        {MetricColumn.RUN_ID: h.run_id, MetricColumn.STEP: h.step, histogram_column(h.key): h.histogram}
        for h in histograms
    )
    return df if df.empty else df.groupby(_ROW_KEY, as_index=False).first()


def merge_by_step(*frames: pd.DataFrame) -> pd.DataFrame:
    """Outer-join the non-empty `frames` on (run_id, step): a row wherever any of them has one."""
    present = [frame for frame in frames if not frame.empty]
    return (
        functools.reduce(lambda left, right: left.merge(right, on=_ROW_KEY, how="outer"), present)
        if present
        else pd.DataFrame()
    )
