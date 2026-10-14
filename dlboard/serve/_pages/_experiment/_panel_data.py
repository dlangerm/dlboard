"""
A panel's data: what the page fetches for the charts on screen, and the fingerprint that says when it changed.

No Dash. The views are handed a `PanelFrames` to fetch through rather than a store, and the live poll compares
`chart_data_fingerprint`s to decide which charts to redraw.
"""

from __future__ import annotations

import hashlib
import typing
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from structlog.stdlib import get_logger

from dlboard import models
from dlboard.models import (
    ARTIFACT_COLUMN_PREFIX,
    HPARAM_COLUMN_PREFIX,
    RUN_NAME_COLUMN,
    Artifact,
    HyperParams,
    artifact_column,
    artifact_fname_column,
    artifact_tags_column,
)
from dlboard.serve._backend._artifact_download import artifact_url
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment._page_model import (
    EXCLUDED_RUNS_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

_log = get_logger(__name__)

type PanelFrames = Callable[[models.PanelInstance[Any, Any]], pd.DataFrame]
"""How a view gets the data for the charts of a panel it shows: handed the panel with just those charts in it."""


def panel_frames(store: DataStore[...], page: BasicExperimentPage) -> PanelFrames:
    """Fetch for `page`'s experiment through `store`, leaving out the runs `page` excludes."""
    experiment_id = cast("int", page.experiment_id)
    return lambda panel: fetch_panel_dataframe(store, experiment_id, panel, page.page_settings)


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
    page_settings: dict[str, Any],
) -> pd.DataFrame:
    """Fetch just the data one panel needs -- not the whole experiment."""
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
    excluded = frozenset[int](page_settings.get(EXCLUDED_RUNS_KEY) or [])
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
    merged = merge_hyperparams(merge_metrics_and_artifacts(metrics_df, artifacts_df), hparams_df)
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
    wanted_hparams = None if hparams is None else {f"{HPARAM_COLUMN_PREFIX}{key}" for key in hparams}
    artifacts = tuple(
        artifact_column(name)
        for name in (
            *(chart.hint_required_artifact_keys() or ()),
            *chart.hint_required_artifact_key_prefixes(),
        )
    )

    def renders_from(column: str) -> bool:
        if column in models.IDENTITY_COLUMNS:
            return True
        if column.startswith(HPARAM_COLUMN_PREFIX):
            return wanted_hparams is None or column in wanted_hparams
        if column.startswith(ARTIFACT_COLUMN_PREFIX):
            return column.startswith(artifacts)
        return metrics is None or column in metrics

    return [column for column in df.columns if renders_from(column)]


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
        if c not in models.NO_DATA_COLUMNS and not c.startswith(HPARAM_COLUMN_PREFIX)
    ]
    if data_cols:
        scoped = scoped.dropna(how="all", subset=data_cols)
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
        rows.append({"run_id": h.run_id, **{f"{HPARAM_COLUMN_PREFIX}{k}": v for k, v in values.items()}})
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


def merge_metrics_and_artifacts(metrics_df: pd.DataFrame, artifacts_df: pd.DataFrame) -> pd.DataFrame:
    if not metrics_df.empty and not artifacts_df.empty:
        return metrics_df.merge(artifacts_df, on=["run_id", "step"], how="outer")
    return metrics_df if not metrics_df.empty else artifacts_df
