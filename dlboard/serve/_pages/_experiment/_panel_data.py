"""
A panel's data: what the page fetches for the charts on screen, and the fingerprint that says when it changed.

No Dash. The callbacks fetch the `PanelFrames` of the panels on screen here and hand them to the views, which
never see a store, and the live poll compares `chart_data_fingerprint`s to decide which charts to redraw.
"""

from __future__ import annotations

import functools
import hashlib
import typing
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard import models
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
from dlboard.serve._backend._artifact_download import artifact_url
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment._paging import visible_chart_indexes

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

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


type PanelFrames = Mapping[str, pd.DataFrame]
"""The data of the charts `Paging` shows in each open panel, by panel name; a panel with no chart showing has none."""


def panel_frames(
    store: DataStore[...], page: BasicExperimentPage, paging: Paging, names: Iterable[str]
) -> PanelFrames:
    """
    Fetch the charts `paging` shows in the panels called `names`, leaving out the runs `page` excludes.

    Only the page of charts shown is fetched for -- a panel of a thousand charts costs what one page of them does.
    """
    experiment_id, excluded = cast("int", page.experiment_id), page.document.excluded_runs
    wanted = frozenset(names)
    frames: dict[str, pd.DataFrame] = {}
    for panel in page.panels:
        indexes = visible_chart_indexes(panel, paging).items
        if panel.name in wanted and indexes:
            shown = panel.model_copy(update={"charts": [panel.charts[i] for i in indexes]})
            frames[panel.name] = fetch_panel_dataframe(store, experiment_id, shown, excluded)
    return frames


def _splits_metric_and_artifact_charts(panel: models.PanelInstance[Any, Any]) -> bool:
    """
    Whether a chart of the panel needs only metrics and another only artifacts.

    That is what makes fetching both for one panel wasteful. A chart that wants both (a table of runs with
    an artifact column) asks for nothing the panel would not fetch for it anyway.
    """
    wants = [
        (
            (cols := chart.hint_required_columns()) is None or bool(cols),
            bool(chart.hint_required_artifact_keys() or chart.hint_required_artifact_key_prefixes()),
        )
        for chart in panel.charts
    ]
    return any(metrics and not artifacts for metrics, artifacts in wants) and any(
        artifacts and not metrics for metrics, artifacts in wants
    )


def fetch_panel_dataframe(
    store: DataStore[...],
    experiment_id: int,
    panel: models.PanelInstance[Any, Any],
    excluded_runs: Iterable[int] = (),
) -> pd.DataFrame:
    """Fetch just the data one panel needs -- not the whole experiment -- leaving out `excluded_runs`."""
    metric_cols = panel.hint_required_columns()
    artifact_keys = panel.hint_required_artifact_keys()
    artifact_key_prefixes = frozenset(panel.hint_required_artifact_key_prefixes())
    has_artifacts = bool(artifact_keys or artifact_key_prefixes)

    if _splits_metric_and_artifact_charts(panel):
        _log.warning(
            "Panel %r mixes metric and artifact charts, fetching both is "
            "less efficient than a panel of one kind; consider splitting it.",
            panel.name,
        )

    # Each hint is `None` for "everything" (e.g. a table chart with no `metrics` filter -- its normal
    # default, flagged to the user by `_unscoped_fetch_badge`) or a set of keys, where empty means
    # "none of this kind at all".
    hparam_keys = panel.hint_required_hparams()
    excluded = frozenset(excluded_runs)
    metrics_df = (
        store.fetch_metrics(
            experiment_id,
            keys=None if metric_cols is None else frozenset(metric_cols),
            exclude_run_ids=excluded,
        ).to_frame()
        if metric_cols is None or metric_cols
        else pd.DataFrame()
    )
    artifacts_df = (
        build_artifacts_dataframe(
            store.fetch_artifacts(
                experiment_id,
                keys=frozenset(k for k in artifact_keys if k is not None),
                key_prefixes=artifact_key_prefixes,
                exclude_run_ids=excluded,
            )
        )
        if has_artifacts
        else pd.DataFrame()
    )
    # fetch_hyperparams has no server-side key filter -- it's one small row per run either way.
    hparams_df = (
        build_hyperparams_dataframe(
            store.fetch_hyperparams(experiment_id, exclude_run_ids=excluded), keys=hparam_keys
        )
        if hparam_keys is None or hparam_keys
        else pd.DataFrame()
    )
    histogram_keys = panel.hint_required_histogram_keys()
    histograms_df = (
        build_histograms_dataframe(
            store.fetch_histograms(
                experiment_id,
                keys=frozenset(histogram_keys),
                max_steps=HistogramFetchSettings().max_histogram_steps,
                exclude_run_ids=excluded,
            )
        )
        if histogram_keys
        else pd.DataFrame()
    )
    merged = merge_hyperparams(merge_by_step(metrics_df, artifacts_df, histograms_df), hparams_df)
    if merged.empty:
        return merged
    # Carried in alongside the metric/artifact/hparam data (not fetched separately by each chart
    # that wants it) so `LineChart`/`ImageChart` can label a run by its name instead of its bare id
    # -- `table_chart.py`'s default "runs" mode explicitly excludes this column, since it isn't a
    # real metric/hparam. `store.get_runs`'s own default page size caps this at 1000 runs, same as
    # every other `list_*` read on this page; a larger experiment just leaves the extra runs'
    # `RUN_NAME_COLUMN` cell blank after the merge below.
    run_names = pd.DataFrame.from_records(
        [{"run_id": run.id, RUN_NAME_COLUMN: run_display_name(run)} for run in store.get_runs(experiment_id)]
    )
    return merged if run_names.empty else merged.merge(run_names, on="run_id", how="left")


def is_lightning_experiment(data_store: DataStore[...], experiment_id: int) -> bool:
    """
    Whether `experiment_id`'s metrics were logged through `DLBoardLogger`.

    Lightning-specific chart-autogen conventions (see `_chart_autogen.lightning_granularity`) only
    apply to those.
    """
    experiment = data_store.get_experiment(experiment_id)
    return experiment is not None and experiment.source == models.ExperimentSource.PYTORCH_LIGHTNING


def _chart_columns(chart: models.ChartInstance[Any, Any], df: pd.DataFrame) -> list[str]:
    """
    The columns of a panel's shared dataframe one chart renders from, in the dataframe's own order.

    Each kind of column is scoped by its own hint, `None` meaning every column of that kind -- so a chart
    that shows every metric but only some hyperparameters is not redrawn by an unrelated hyperparameter.
    """
    metrics = chart.hint_required_columns()
    hparams = chart.hint_required_hparams()
    wanted_hparams = None if hparams is None else {hparam_column(key) for key in hparams}
    artifacts = tuple(
        artifact_column(name)
        for name in (
            *(chart.hint_required_artifact_keys() or ()),
            *chart.hint_required_artifact_key_prefixes(),
        )
    )
    histograms = {histogram_column(key) for key in chart.hint_required_histogram_keys()}

    def renders_from(column: str) -> bool:
        if column in models.IDENTITY_COLUMNS:
            return True
        match column_kind(column):
            case ColumnKind.HPARAM:
                return wanted_hparams is None or column in wanted_hparams
            case ColumnKind.ARTIFACT:
                return column.startswith(artifacts)
            case ColumnKind.HISTOGRAM:
                return column in histograms
            case ColumnKind.METRIC:
                return metrics is None or column in metrics
            case ColumnKind.GROUPING:
                msg = f"no dataframe column is of kind {ColumnKind.GROUPING}"
                raise ValueError(msg)

    return [column for column in df.columns if renders_from(column)]


def _histogram_contents(cell: object) -> dict[str, Any] | None:
    """What a histogram column's cell holds, as plain data (`None` where the key was not logged at the step)."""
    return cell.model_dump() if isinstance(cell, Histogram) else None


def chart_data_fingerprint(chart: models.ChartInstance[Any, Any], df: pd.DataFrame) -> str:
    """
    A cheap content hash of the slice of a panel's shared dataframe one chart actually renders from.

    Lets a live-update poll tell "this chart's underlying data hasn't actually changed" apart from
    "the experiment changed *something*, somewhere" -- `Experiment.revision` is bumped by any write
    anywhere in the experiment (a different run, a different metric, an epoch-level chart's data
    between epochs), so without this every open chart would re-render on every poll tick regardless
    of whether anything it actually shows moved. Scoped per *chart*, not per panel: a panel commonly
    holds charts with very different update cadences -- an epoch-level chart next to a per-step one,
    grouped there by "Auto-generate charts" -- and a panel-wide fingerprint would pop every chart in
    it whenever *any one* of them changed.

    The slice is the chart's metrics, the hyperparameters and artifact columns it asks for, and the
    run names, so editing a hyperparameter or renaming a run redraws the charts that show it.
    A hint of `None` (a table chart with no metrics filter, documented to mean "show every metric") means
    every column of that kind, since such a chart can't be scoped any tighter than the panel fetch already was.

    Hashes the `to_json(orient="split", ...)` serialization the rest of this page relies on,
    not `pandas.util.hash_pandas_object` -- that hashes cell values
    directly and raises `TypeError: unhashable type` the moment any column holds something like a
    list (nothing in this dataframe's own columns does today, but a hash used purely to detect "did
    this change" has no business being pickier about content than the JSON serialization the rest of
    this page already ships to the browser).

    Only rows where the chart has data of its own count: the dataframe is wide, so another metric
    logging a step the chart has nothing at adds a row to it -- which is not a change to the chart,
    and must not redraw it. (A hyperparameter is on every row of its run, so it never says a row is the
    chart's.) The index is dropped for the same reason: it numbers the rows of the *whole* dataframe, so
    another run's new row would renumber this chart's.
    """
    scoped = df[_chart_columns(chart, df)]
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
